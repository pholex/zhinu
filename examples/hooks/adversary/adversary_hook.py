#!/usr/bin/env python3
"""PreToolUse 二审钩子：bash 类调用交给另一次 `xiaoyu -p` 做第二意见。

挂法见同目录 README.md 与 hooks.toml。协议（xiaoyu/hooks.py）：
- stdin 收一个 JSON：{"event": "PreToolUse", "workspace": ..., "tool": 工具名, "args": 参数}
- 退出码 2 = 拦截，stderr 是回灌给模型的理由；0 = 放行；其它 = 按放行处理并告警。

判定只看结构化输出的 `verdict` 字段（--output-schema 强约束 allow / block），
不在正文里找 "BLOCK" 字样——文本匹配会被模型的解释性句子误触。

这是**补充层，不替代护栏**：二审模型调不起来、超时、输出不合法，一律 fail-open
（退出码 0 放行）。硬红线、deny 规则、沙箱才是闸；本钩子只是在它们之外多一双眼睛。

规则正文放同目录 adversary.md，改规则不用动代码。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES = HERE / "adversary.md"

#  哪些工具算"命令类"。bash 之外，MCP 工具经 use_tool 转发时 tool 是转发器名——
#  hooks.toml 的 matcher 已经只挑 bash，这里再守一道，直接跑脚本时也不会误判
COMMAND_TOOLS = {"bash"}

#  二审用的模型：便宜、快就行，占位默认走出厂默认模型；按本机情况改环境变量
MODEL = os.environ.get("XIAOYU_ADVERSARY_MODEL", "deepseek-flash")
#  二审总时限（秒）。要小于 hooks.toml 里给本钩子的 timeout，否则先被 harness 判超时
TIMEOUT = float(os.environ.get("XIAOYU_ADVERSARY_TIMEOUT", "45"))

SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["allow", "block"]},
        "reason": {"type": "string"},
    },
    "required": ["verdict", "reason"],
})


def allow(note: str = "") -> int:
    if note:
        print(f"[adversary] 放行：{note}", file=sys.stderr)
    return 0


def second_opinion(command: str, workspace: str) -> dict | None:
    """起一次一次性执行拿裁决；任何失败返回 None（调用方 fail-open）。"""
    rules = RULES.read_text(encoding="utf-8", errors="replace")
    prompt = (
        f"{rules}\n\n=== 待审命令 ===\n工作区：{workspace}\n命令：\n```\n{command}\n```\n\n"
        "只做判断，不要调用任何工具。按 schema 给出 verdict 与一句话 reason。"
    )
    env = dict(os.environ)
    #  防递归：二审进程自己也会加载用户级 hooks.toml，再触发本钩子就套娃了
    env["XIAOYU_ENABLE_HOOKS"] = "0"
    #  二审不需要扩展面，关掉省启动时间。工作区是空的临时目录、不开 --yolo：
    #  无人值守下要确认的命令会被自动拒绝，它就算想跑命令也只能在空目录里打转。
    #  ⚠️ 别加 --mode plan：plan 档把 structured_output 也当写操作拦下，而无人值守里
    #  exit_plan_mode 又会被自动拒绝，结构化裁决永远交不出来（实测如此）
    for key in ("XIAOYU_ENABLE_SKILLS", "XIAOYU_ENABLE_PLUGINS", "XIAOYU_ENABLE_MCP",
                "XIAOYU_ENABLE_AGENTS", "XIAOYU_ENABLE_WEB_SEARCH", "XIAOYU_ENABLE_BROWSER",
                "XIAOYU_UPDATE_CHECK"):
        env[key] = "0"
    with tempfile.TemporaryDirectory(prefix="xiaoyu-adversary-") as scratch:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "xiaoyu", "-p", prompt,
                 "--model", MODEL,
                 "--workspace", scratch,
                 "--output-format", "json", "--output-schema", SCHEMA],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=TIMEOUT, env=env, stdin=subprocess.DEVNULL,
            )
        except (subprocess.TimeoutExpired, OSError):
            return None
    if proc.returncode != 0:
        return None
    lines = [line for line in proc.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    try:
        payload = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    output = payload.get("output")
    if not isinstance(output, dict) or output.get("verdict") not in ("allow", "block"):
        return None
    return output


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return allow("stdin 不是合法 JSON")
    if event.get("event") != "PreToolUse" or event.get("tool") not in COMMAND_TOOLS:
        return 0
    args = event.get("args") or {}
    command = str(args.get("command", "")).strip()
    if not command:
        return 0
    if not RULES.is_file():
        return allow(f"规则文件缺失 {RULES}")
    verdict = second_opinion(command, str(event.get("workspace", "")))
    if verdict is None:
        return allow("二审不可用（模型调用失败 / 超时 / 输出不合法），fail-open")
    if verdict["verdict"] == "block":
        reason = str(verdict.get("reason", "")).strip() or "二审判定为高风险命令"
        print(f"二审拦截：{reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
