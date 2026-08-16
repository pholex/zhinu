"""snapshot 套件的归一化器（纯函数：把捕获面洗成跨机器、跨时间稳定的
文本，洗完才能与 golden 直接比对）。

三个面各一只归一化器：
- stream-json stdout：耗时归零、会话路径 token、临时目录 token、CRLF 归一；
- 会话 JSONL：时间戳/版本 token、tool_call id → 首见序号（重录制不 churn）；
- 请求头（system prompt / tool schemas）：机器相关段（工作区路径、平台串、
  shell 注记、环境画像）→ token。这些段在测试进程内等值重算——同机同 env
  下与子进程产出的字节相同，替换后剩下的就是 xiaoyu 自己控制的模板内容，
  即 pinned header 真正要锁的东西。

测试专用，不随包发布（tests/ 不进 wheel）。
"""

from __future__ import annotations

import json
import platform
import re
from pathlib import Path
from typing import Any

#  重试提示里的退避秒数带 ±25% jitter（agent._stream_with_recovery），逐次不同
_WAIT_RE = re.compile(r"\d+(?:\.\d+)?s 后重试")

#  路径 token 起始的连续路径段。golden 在 macOS 录制，token 后的分隔符必须
#  跨平台同形——Windows 上 `<tmp>\ws` 要洗成 `<tmp>/ws`，否则 golden 全平台
#  比对在 Windows 一定红（CI 首跑就是这么挂的）。
_TOKEN_SPAN = re.compile(r"(?:<tmp>|\{\{tmp\}\}|\{\{cwd\}\})[^\s\"'，。；：)\]]*")


def _token_tmp_paths(value: str, tmp: str, token: str) -> str:
    """tmp 前缀 → token，并把 token 起始路径段里的反斜杠压成正斜杠。"""
    value = value.replace(tmp, token)
    flipped = tmp.replace("\\", "/")
    if flipped != tmp:
        value = value.replace(flipped, token)
    return _TOKEN_SPAN.sub(lambda m: m.group(0).replace("\\", "/"), value)


def _scrub_machine_bits(text: str, tmp: str) -> str:
    """机器相关内容 → token（最长优先：路径可能出现在任何段里，先换）。"""
    from xiaoyu import envprobe
    from xiaoyu.tools import _platform_shell_note

    import xiaoyu

    replacements = [
        #  工作区路径用平台原生分隔符拼（Windows 上是 tmp\ws），posix 形态兜底
        (str(Path(tmp) / "ws"), "{{cwd}}"),
        (f"{tmp}/ws", "{{cwd}}"),
        (tmp, "{{tmp}}"),
        #  扩展指南的绝对路径随安装位置变（site-packages / 源码树），token 化
        (str(Path(xiaoyu.__file__).parent / "docs" / "extending.md"), "{{extending_doc}}"),
        (envprobe.block(), "\n{{envprobe}}\n"),
        (f"{platform.system()} {platform.release()} ({platform.machine()})", "{{platform}}"),
        (_platform_shell_note(), "{{shell_note}}"),
    ]
    for needle, token in replacements:
        if needle:
            text = text.replace(needle, token)
    return _TOKEN_SPAN.sub(lambda m: m.group(0).replace("\\", "/"), text)


def normalize_system_prompt(text: str, tmp: str) -> str:
    return _scrub_machine_bits(text.replace("\r\n", "\n"), tmp)


def normalize_tool_schemas(tools: Any, tmp: str) -> str:
    """schemas → 稳定 JSON 文本（工具描述里可能嵌平台注记，同一套替换）。"""
    text = json.dumps(tools, ensure_ascii=False, indent=2, sort_keys=True)
    return _scrub_machine_bits(text, tmp)


def normalize_event(event: dict[str, Any], tmp: str) -> dict[str, Any]:
    """stream-json 的一行 → 可比对形态（在 test_e2e_scripted 的雏形上扩）。"""
    out: dict[str, Any] = {}
    for key, value in event.items():
        if key == "seconds":
            out[key] = 0
        elif key == "session_log":
            out[key] = "<session_log>"
        elif isinstance(value, str):
            value = _token_tmp_paths(value.replace("\r\n", "\n"), tmp, "<tmp>")
            out[key] = _WAIT_RE.sub("<wait>s 后重试", value)
        elif isinstance(value, dict):
            out[key] = normalize_event(value, tmp)
        else:
            out[key] = value
    return out


class CallIdMap:
    """tool_call id → <call-N>（首见序号）。

    手写 fixture 的 id 本来就确定，但真录制拿到的是 provider 随机 id
    （call_AbC…）——不洗掉的话每次重录制整份 golden 全 churn。"""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    def token(self, raw: str) -> str:
        if raw not in self._seen:
            self._seen[raw] = f"<call-{len(self._seen) + 1}>"
        return self._seen[raw]


def normalize_session_record(
    record: dict[str, Any], tmp: str, ids: CallIdMap
) -> dict[str, Any]:
    """会话 JSONL 的一行 → 可比对形态。

    format 字段刻意保留字面值：SESSION_FORMAT 升版**应当** churn golden——
    格式变更就该在 diff 里被看见（guard 要证明会红）。
    """
    out: dict[str, Any] = {}
    for key, value in record.items():
        if key in ("ts", "started_at"):
            out[key] = "<ts>"
        elif key == "version":
            out[key] = "<version>"
        elif key == "tool_call_id" and isinstance(value, str):
            out[key] = ids.token(value)
        elif key == "tool_calls" and isinstance(value, list):
            calls = []
            for call in value:
                call = dict(call)
                if isinstance(call.get("id"), str):
                    call["id"] = ids.token(call["id"])
                calls.append(call)
            out[key] = calls
        elif isinstance(value, str):
            out[key] = _token_tmp_paths(value.replace("\r\n", "\n"), tmp, "<tmp>")
        elif isinstance(value, dict):
            out[key] = normalize_session_record(value, tmp, ids)
        else:
            out[key] = value
    return out


def normalize_session_lines(lines: list[dict[str, Any]], tmp: str) -> list[str]:
    ids = CallIdMap()
    return [
        json.dumps(normalize_session_record(record, tmp, ids), ensure_ascii=False)
        for record in lines
    ]
