"""hooks 最小版（按小羽体量收敛的生命周期钩子）。

用户在生命周期节点挂 shell 命令，harness 喂 JSON、看退出码定夺：

    #  <用户配置目录>/hooks.toml
    [[hooks]]
    event = "PreToolUse"        # PreToolUse | PostToolUse | UserPromptSubmit | Stop
    matcher = "bash"            # 正则匹配工具名（只对 *ToolUse 有意义，可省）
    command = "python ~/bin/check.py"
    timeout = 10                # 秒，缺省 30，上限 600

约定（沿用业界通行的习惯，用户不用学新规矩）：
- stdin 收一个 JSON 对象（event / tool / args / output / prompt 视事件而定）；
- **退出码 2 = block**，stderr 作为理由回灌模型或提示用户；
- 退出码 0 = 放行；其它退出码、超时、起不来 = **fail-open 放行**并打 warn——
  hook 是辅助护栏，不能因为自己坏了把 agent 卡死（deny 规则才是硬闸）；
- 同一事件多个 hook 顺序执行（个人工具挂不了几个，不为并行引入线程池），
  任一 block 即 block，理由拼接。

刻意只认**用户级** hooks.toml，不读工作区级：hook 是任意代码执行，
工作区级配置文件等于"clone 一个仓库就把命令种进你的 shell"——要开这个口，
得先有指纹/审批机制（同 MCP rug-pull 那套），最小版不背这个包袱。
`XIAOYU_ENABLE_HOOKS=0` 一键关闭。

四个事件的语义：
- PreToolUse   block → 该次工具调用不执行，理由回灌模型（在审批之前，省一次弹窗）
- PostToolUse  block → 工具已执行，理由作为附注拼进 tool result（模型看得到）
- UserPromptSubmit block → 本轮不发给模型，理由打给用户
- Stop         block → 模型想收尾时被顶回去，理由作为 user 消息续跑一步
               （每轮只顶一次，防 hook 永远不放行造成死循环）
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import user_config_dir

EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop")
_ENTRY_KEYS = frozenset({"event", "matcher", "command", "timeout"})

_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT = 600.0
#  喂给 hook 的 output/prompt 字段上限：hook 不需要全文，超长纯属拖慢
_PAYLOAD_TEXT_CAP = 8_000


@dataclass(frozen=True)
class Hook:
    event: str
    command: str
    matcher: str = ""  # 正则（re.search 语义），空 = 全匹配
    timeout: float = _DEFAULT_TIMEOUT

    def matches(self, tool_name: str) -> bool:
        if not self.matcher:
            return True
        try:
            return re.search(self.matcher, tool_name) is not None
        except re.error:
            return False


@dataclass(frozen=True)
class Decision:
    blocked: bool
    reason: str = ""


def hooks_path() -> Path:
    return user_config_dir() / "hooks.toml"


def load_hooks(path: Path | None = None) -> tuple[list[Hook], list[str]]:
    """解析 hooks.toml，返回 (hooks, 问题清单)。坏条目跳过不拦启动。"""
    path = path or hooks_path()
    if not path.is_file():
        return [], []
    problems: list[str] = []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return [], [f"hooks.toml 解析失败：{exc}"]
    #  不认识的键要说出来：[[hooks]] 写成 [[hook]]，整份配置一条都不加载，
    #  用户以为挂上的护栏其实不存在
    for key in data:
        if key != "hooks":
            problems.append(f"顶层键 {key!r} 不认识，已忽略——条目要写在 [[hooks]] 下")
    entries = data.get("hooks") or []
    if not isinstance(entries, list):
        problems.append("hooks 应当是表数组（[[hooks]]），整段已忽略")
        entries = []
    hooks: list[Hook] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            problems.append(f"第 {index} 条不是表")
            continue
        if unknown := sorted(set(entry) - _ENTRY_KEYS):
            problems.append(
                f"第 {index} 条有不认识的键 {'、'.join(unknown)}，已忽略"
                f"（可用：{'、'.join(sorted(_ENTRY_KEYS))}）"
            )
        event = str(entry.get("event", ""))
        command = str(entry.get("command", "")).strip()
        if event not in EVENTS:
            problems.append(f"第 {index} 条 event={event!r} 不认识（可用：{'、'.join(EVENTS)}）")
            continue
        if not command:
            problems.append(f"第 {index} 条缺 command")
            continue
        matcher = str(entry.get("matcher", "") or "")
        if matcher:
            try:
                re.compile(matcher)
            except re.error as exc:
                problems.append(f"第 {index} 条 matcher 正则不合法：{exc}")
                continue
        try:
            timeout = float(entry.get("timeout", _DEFAULT_TIMEOUT))
        except (TypeError, ValueError):
            problems.append(
                f"第 {index} 条 timeout={entry.get('timeout')!r} 不是数字，按 {_DEFAULT_TIMEOUT:g}s 算"
            )
            timeout = _DEFAULT_TIMEOUT
        timeout = min(max(timeout, 1.0), _MAX_TIMEOUT)
        hooks.append(Hook(event=event, command=command, matcher=matcher, timeout=timeout))
    return hooks, problems


def _hook_env() -> dict[str, str]:
    """hook 子进程的环境：原样继承 + 把管道编码钉成 UTF-8。

    payload（含中文提示词/工具输出）走 stdin、理由走 stderr，两个方向在 Windows
    上默认都是 locale 编码（cp1252/GBK）：写入端直接 UnicodeEncodeError 把 agent
    炸掉，读出端把中文理由变成 `\\uXXXX` 字面量——不报错、值悄悄错。

    这里强设而非 setdefault（mcp.py 的 `_safe_env` 是 setdefault）：管道的另一
    端是我们自己，已经按 UTF-8 收发，用户环境里一个 `PYTHONIOENCODING=gbk`
    就能让协议两端对不上。要用别的编码，在 hook 脚本内部自己 reconfigure。
    只动流编码（PYTHONIOENCODING），不开 PYTHONUTF8——后者连带改文件系统与
    locale 默认编码，那是 hook 脚本自己的事，不该由我们替它决定。
    """
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


class HookEngine:
    """按事件分发执行。notify 回调用于 fail-open 时的 warn（由 Agent 接 sink）。"""

    def __init__(self, hooks: list[Hook], workspace: Path, notify: Any = None) -> None:
        self.hooks = hooks
        self.workspace = workspace
        self._notify = notify or (lambda text: None)

    def has(self, event: str) -> bool:
        return any(hook.event == event for hook in self.hooks)

    def for_tools(self, workspace: Path) -> "HookEngine | None":
        """给委托出去的子 agent 用的一份：只带工具类钩子，工作目录换成它的。

        用户挂在工具调用上的护栏不该因为"这一步是子 agent 做的"就不触发；
        UserPromptSubmit / Stop 说的是用户这一轮的开头和收尾，子 agent 没有
        这两个时刻，不带下去。没有工具类钩子时返回 None（触发点零开销）。
        """
        kept = [hook for hook in self.hooks if hook.event in ("PreToolUse", "PostToolUse")]
        return HookEngine(kept, workspace, self._notify) if kept else None

    def fire(
        self, event: str, payload: dict[str, Any], tool_name: str = "", also: str = ""
    ) -> Decision:
        """跑该事件的所有匹配 hook。任一 block（exit 2）即 block，理由拼接。

        also 是这次调用的另一个名字：MCP 工具经转发器调用时，tool_name 是转发器、
        also 是它点名的工具——matcher 写哪个都算匹配，挂在具体 MCP 工具上的钩子
        不因为调用走了转发器就不触发。
        """
        reasons: list[str] = []
        body = json.dumps(
            {"event": event, "workspace": str(self.workspace), **payload},
            ensure_ascii=False,
        )
        for hook in self.hooks:
            if hook.event != event or not (
                hook.matches(tool_name) or (also and hook.matches(also))
            ):
                continue
            try:
                proc = subprocess.run(
                    hook.command,
                    shell=True,
                    input=body,
                    capture_output=True,
                    text=True,
                    #  两个方向都钉死 UTF-8（见 _hook_env）；replace 兜底，
                    #  hook 输出里一个坏字节不该让整条 fire 抛异常
                    encoding="utf-8",
                    errors="replace",
                    timeout=hook.timeout,
                    cwd=self.workspace,
                    env=_hook_env(),
                )
            except subprocess.TimeoutExpired:
                self._notify(f"[hook 超时（>{hook.timeout:.0f}s），放行：{hook.command}]")
                continue
            except OSError as exc:
                self._notify(f"[hook 启动失败（{exc}），放行：{hook.command}]")
                continue
            if proc.returncode == 2:
                reason = proc.stderr.strip() or proc.stdout.strip() or "（hook 未给出理由）"
                reasons.append(clip_reason(reason))
            elif proc.returncode != 0:
                self._notify(
                    f"[hook 退出码 {proc.returncode}（既非 0 放行也非 2 拦截），"
                    f"按放行处理：{hook.command}]"
                )
        return Decision(blocked=bool(reasons), reason="；".join(reasons))


#  一条 hook 理由进上下文的字符上限。理由是给模型的反馈，不是日志：测试闸把
#  整份失败输出打到 stderr 是常事，几十万字符原样进历史，之后每一轮都要为它付费
_REASON_CAP = 4_000
#  开头留这么多（第一条报错通常在最前），其余额度给结尾（汇总通常在最后）
_REASON_HEAD = 1_200


def clip_reason(text: str) -> str:
    """hook 理由超限时留头留尾，中间明说省了多少。"""
    if len(text) <= _REASON_CAP:
        return text
    tail = _REASON_CAP - _REASON_HEAD
    dropped = len(text) - _REASON_CAP
    return (
        f"{text[:_REASON_HEAD]}\n…（hook 输出过长，中间省略 {dropped} 字符）…\n{text[-tail:]}"
    )


def clip(text: str) -> str:
    """payload 里的长文本字段统一截到上限。"""
    if len(text) <= _PAYLOAD_TEXT_CAP:
        return text
    return text[:_PAYLOAD_TEXT_CAP] + "…（已截断）"
