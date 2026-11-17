"""确定性测试桩 provider（脚本驱动的 Echo DSL）。

解决的问题：CLI 层此前没有"不打网络的端到端回归"——unittest 只到模块层，
experiments/ 全靠真模型。这个桩把「LLM 会说什么」写成脚本文件，整条 CLI
链路（参数解析 → Agent 主循环 → 工具执行 → 事件流 → 输出格式）真实地跑，
唯一被替换的是网络调用本身。

用法：`XIAOYU_SCRIPTED_SCRIPTS=<脚本路径>` 即整个进程只注册 `_scripted`
一个 provider（见 providers._order），任何模型名都路由到它，绝无真实网络。

脚本 DSL（一行一条指令，`---` 单独一行分隔"轮"，每次 LLM 调用弹一轮）：

    # 注释与空行忽略
    text: 你好                                 ← 一条正文 delta（多行=多条 delta）
    tool_call: {"name": "bash", "arguments": {"command": "ls"}}
    tool_call_part: {"index": 0, "id": "c1", "name": "bash"}
    tool_call_part: {"index": 0, "arguments": "{\"comm"}
    usage: {"prompt_tokens": 100, "completion_tokens": 7}
    reasoning: [{"summary": "…"}]              ← 推理条目（Responses 传输的 chunk.reasoning）
    error: 上游超载 429                        ← 在此位置抛 RuntimeError(消息)

`tool_call` 是整只工具调用一个 chunk 的便捷写法（id 缺省自动编号，
arguments 可以是对象也可以是原始字符串）；`tool_call_part` 是分片写法，
专测流式参数拼接。`error:` 的消息文本会被 errors.classify 按关键词分类
（带 "429" 即限流可重试，其余默认 fatal），不构造 openai 异常对象——
分类器本来就按文本兜底识别。

脚本轮次用尽后再来 LLM 调用会抛 ScriptedExhausted：测试里"CLI 比预期多调
了一次模型"必须响亮地失败，而不是静默空转。

snapshot 套件（tests/test_e2e_snapshot.py）的两个配套开关：
- XIAOYU_SCRIPTED_CAPTURE=<目录>：每次 LLM 调用把请求头（model / system
  prompt / tool schemas）dump 成 request-<n>.json——回放模式下锁 pinned
  header 用（keyless 就能发现"改了 prompt 没 refresh 快照"）。
- XIAOYU_SCRIPTED_STRICT=1：进程退出时脚本还有剩余轮次 → stderr 打
  ScriptedUnconsumed 告警（双向消费检查：过度消费抛
  ScriptedExhausted，消费不足也必须可见，否则"场景静默少驱动了几次调用"
  就是假绿）。走 stderr 而非改退出码：atexit 里改不了退出码，测试断言
  stderr 干净即可。
"""

from __future__ import annotations

import atexit
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

#  providers._order / _make / client 认的保留 provider 名与激活开关
SCRIPTED_PROVIDER = "_scripted"
SCRIPTED_ENV = "XIAOYU_SCRIPTED_SCRIPTS"
CAPTURE_ENV = "XIAOYU_SCRIPTED_CAPTURE"
STRICT_ENV = "XIAOYU_SCRIPTED_STRICT"

#  一条指令 = (kind, payload)。payload：text/error 是 str，其余是 dict
Action = tuple[str, Any]

_KINDS = ("text", "tool_call", "tool_call_part", "usage", "reasoning", "error")


class ScriptError(ValueError):
    """脚本本身写错了（未知指令、JSON 不合法）。带行号，测试一眼定位。"""


class ScriptedExhausted(RuntimeError):
    """脚本轮次已用尽，又来了一次 LLM 调用。"""


def parse_scripts(text: str) -> list[list[Action]]:
    """DSL 全文 → 轮次列表。空轮（分隔符之间没有指令）不保留。"""
    turns: list[list[Action]] = []
    current: list[Action] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line == "---":
            if current:
                turns.append(current)
                current = []
            continue
        kind, sep, payload = line.partition(":")
        kind = kind.strip()
        if not sep or kind not in _KINDS:
            raise ScriptError(f"第 {lineno} 行：不认识的指令 {line!r}（合法：{'、'.join(_KINDS)}）")
        payload = payload.strip()
        if kind in ("text", "error"):
            #  正文要带换行等转义时写成 JSON 字符串：text: "第一行\n第二行"
            if payload.startswith('"'):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError as exc:
                    raise ScriptError(
                        f"第 {lineno} 行：{kind} 的引号形态不是合法 JSON 字符串：{exc}"
                    ) from exc
            current.append((kind, payload))
            continue
        try:
            data = json.loads(payload or ("[]" if kind == "reasoning" else "{}"))
        except json.JSONDecodeError as exc:
            raise ScriptError(f"第 {lineno} 行：{kind} 的 payload 不是合法 JSON：{exc}") from exc
        if kind == "reasoning":
            if not isinstance(data, list):
                raise ScriptError(f"第 {lineno} 行：reasoning 的 payload 必须是 JSON 数组")
            current.append((kind, data))
            continue
        if not isinstance(data, dict):
            raise ScriptError(f"第 {lineno} 行：{kind} 的 payload 必须是 JSON 对象")
        if kind == "tool_call" and not data.get("name"):
            raise ScriptError(f"第 {lineno} 行：tool_call 必须带 name")
        current.append((kind, data))
    if current:
        turns.append(current)
    return turns


def load_scripts(path: str) -> list[list[Action]]:
    with open(path, encoding="utf-8") as fh:
        return parse_scripts(fh.read())


#  —— chunk 构造：只造 agent._consume_stream 实际读的那些属性 ——


def _delta_chunk(**delta: Any) -> SimpleNamespace:
    merged = {"content": None, "tool_calls": None, **delta}
    return SimpleNamespace(
        usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(**merged))]
    )


def _usage_chunk(data: dict[str, Any]) -> SimpleNamespace:
    usage = SimpleNamespace(
        prompt_tokens=int(data.get("prompt_tokens", 0)),
        completion_tokens=int(data.get("completion_tokens", 0)),
    )
    return SimpleNamespace(usage=usage, choices=[])


def _fragment(data: dict[str, Any], auto_id: str) -> SimpleNamespace:
    """tool_call_part 的分片：与 openai SDK 的 delta.tool_calls 元素同形。"""
    function = SimpleNamespace(
        name=data.get("name") or None, arguments=data.get("arguments") or None
    )
    return SimpleNamespace(
        index=int(data.get("index", 0)),
        id=data.get("id") or (auto_id if data.get("name") else None),
        function=function,
    )


class ScriptedClient:
    """鸭子型 OpenAI client：只实现 chat.completions.create。

    队列进程内共享（Registry 按 provider 缓存 client）：主循环、摘要、
    explore 子 agent 的每一次 LLM 调用都从同一条队列弹——"总共调了几次模型"
    因此是可断言的。
    """

    def __init__(self, turns: list[list[Action]]) -> None:
        self._turns = list(turns)
        self._served = 0
        #  create(**kwargs) 的调用形态是 client.chat.completions.create
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create)
        )
        if os.environ.get(STRICT_ENV, "").strip():
            atexit.register(self._complain_unconsumed)

    def _complain_unconsumed(self) -> None:
        """strict 模式的退出检查：见模块 docstring。"""
        if self._turns:
            print(
                f"ScriptedUnconsumed: 脚本还剩 {len(self._turns)} 轮未被消费"
                f"（共消费 {self._served} 轮）",
                file=sys.stderr,
            )

    @classmethod
    def from_file(cls, path: str) -> "ScriptedClient":
        return cls(load_scripts(path))

    def _next_turn(self) -> list[Action]:
        if not self._turns:
            raise ScriptedExhausted(
                f"脚本已用尽（共 {self._served} 轮），又来了第 {self._served + 1} 次 LLM 调用"
            )
        self._served += 1
        return self._turns.pop(0)

    def _create(self, **request: Any) -> Any:
        capture = os.environ.get(CAPTURE_ENV, "").strip()
        if capture:
            #  惰性 import：capture 只在 snapshot 套件里开，正常 e2e 不碰 snapshot 模块
            from .snapshot import dump_request_header

            turn_index = self._served + 1  # _next_turn 之前算好：异常路径也有编号
            dump_request_header(Path(capture), turn_index, request)
        turn = self._next_turn()
        if request.get("stream"):
            return self._stream(turn)
        return self._response(turn)

    def _stream(self, turn: list[Action]) -> Iterator[SimpleNamespace]:
        call_no = 0
        for kind, payload in turn:
            if kind == "text":
                yield _delta_chunk(content=payload)
            elif kind == "error":
                raise RuntimeError(payload or "scripted error")
            elif kind == "usage":
                yield _usage_chunk(payload)
            elif kind == "reasoning":
                #  与 responses.py 转写后的 chunk 同形：reasoning 属性挂条目列表
                yield SimpleNamespace(usage=None, reasoning=payload, choices=[])
            elif kind == "tool_call":
                call_no += 1
                arguments = payload.get("arguments", {})
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments, ensure_ascii=False)
                whole = {
                    "index": payload.get("index", call_no - 1),
                    "id": payload.get("id") or f"scripted_call_{self._served}_{call_no}",
                    "name": payload["name"],
                    "arguments": arguments,
                }
                yield _delta_chunk(tool_calls=[_fragment(whole, whole["id"])])
            elif kind == "tool_call_part":
                call_no += 1
                auto = f"scripted_call_{self._served}_{call_no}"
                yield _delta_chunk(tool_calls=[_fragment(payload, auto)])

    def _response(self, turn: list[Action]) -> SimpleNamespace:
        """非流式（摘要路径）：text 拼正文，error 照抛，工具指令在此无意义。"""
        parts: list[str] = []
        usage = None
        for kind, payload in turn:
            if kind == "text":
                parts.append(payload)
            elif kind == "error":
                raise RuntimeError(payload or "scripted error")
            elif kind == "usage":
                usage = SimpleNamespace(
                    prompt_tokens=int(payload.get("prompt_tokens", 0)),
                    completion_tokens=int(payload.get("completion_tokens", 0)),
                )
        message = SimpleNamespace(content="\n".join(parts))
        return SimpleNamespace(usage=usage, choices=[SimpleNamespace(message=message)])
