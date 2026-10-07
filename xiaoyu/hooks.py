"""hooks 最小版（按小羽体量收敛的生命周期钩子）。

用户在生命周期节点挂 shell 命令，harness 喂 JSON、看退出码定夺：

    #  <用户配置目录>/hooks.toml
    [[hooks]]
    event = "PreToolUse"        # PreToolUse | PostToolUse | ToolFailed | UserPromptSubmit
                                # | Stop | SessionStart | SessionEnd | SubagentStart
                                # | SubagentEnd | BeforeCompact | AfterCompact
    matcher = "bash"            # 正则匹配工具名（只对工具类事件有意义，可省）
    command = "python ~/bin/check.py"
    timeout = 10                # 秒，缺省 30，上限 600
    on_failure = "allow"        # allow | block：钩子自己坏了怎么办（只对 PreToolUse 生效）

约定（沿用业界通行的习惯，用户不用学新规矩）：
- stdin 收一个 JSON 对象（event / workspace 必有；tool / args / call_id / output /
  prompt / model / session 视事件而定）。PreToolUse 与 PostToolUse（以及 ToolFailed）
  对同一次调用带**同一个 call_id**，外部钩子靠它把"前"与"后"对上；
- **退出码 2 = block**，stderr 作为理由回灌模型或提示用户；
- 退出码 0 = 放行；其它退出码、超时、起不来 = 默认 **fail-open 放行**并打 warn——
  hook 是辅助护栏，不能因为自己坏了把 agent 卡死（deny 规则才是硬闸）。
  `on_failure = "block"` 反过来：钩子坏了就按拦截处理，理由里标明"钩子失败"——
  给"这道闸必须跑过才能动手"的场景（合规审计、生产环境）。只对 PreToolUse
  生效：其它事件的 block 本就只是反馈或顶回，拿"钩子坏了"去顶回模型没有意义；
- 同一事件多个 hook 顺序执行（个人工具挂不了几个，不为并行引入线程池），
  任一 block 即 block，理由拼接。

刻意只认**用户级** hooks.toml，不读工作区级：hook 是任意代码执行，
工作区级配置文件等于"clone 一个仓库就把命令种进你的 shell"——要开这个口，
得先有指纹/审批机制（同 MCP rug-pull 那套），最小版不背这个包袱。
`XIAOYU_ENABLE_HOOKS=0` 一键关闭。

事件的语义（与 SDK 进程内 hook 同名同义、payload 同形）：
- PreToolUse   block → 该次工具调用不执行，理由回灌模型（在审批之后、执行之前）
- PostToolUse  block → 工具已执行，理由作为附注拼进 tool result（模型看得到）
- ToolFailed   通知：工具结果判成失败（ERROR:）之后触发，带 call_id / args / output；
               发生在动作之后，block 没有意义，退出码只决定要不要打 warn
- UserPromptSubmit block → 本轮不发给模型，理由打给用户；放行时 stdout 的**首个非空行**
               作为本轮的附加上下文紧跟用户输入注入历史（与 SessionStart 同一通道：
               harness 放进来、内容不可信，受 OUTPUT_LINE_CAP）——给"按本轮输入
               查个工单号 / 附上当前 git 状态"这类每轮都变的补充用
- Stop         block → 模型想收尾时被顶回去，理由作为 user 消息续跑一步
               （每轮只顶一次，防 hook 永远不放行造成死循环）
- SessionStart 会话首轮之前触发一次（子 agent 不触发）：block → 拒绝启动，理由打给
               用户；放行时 stdout 的**首个非空行**作为一次性消息注入历史（给宿主
               注入环境说明用：当前分支、值班提示……），受长度上限
- SessionEnd   会话正常收尾时触发一次，结果不影响退出
- SubagentStart 主会话委托子 agent 之前（带 agent 名）：block → 这次委托不执行，
               模型收到"委托失败"的结果自行改道
- SubagentEnd  子 agent 收工之后的通知（带 agent 名与是否失败），block 只在结果里留一条附注
- BeforeCompact 要压缩上下文之前（带估算 token 数与是否强制）：block → 不压缩，
               本次压缩以异常中止——与 SDK 进程内 hook 同义；上下文已超窗时这一轮
               无法继续，所以只给"压缩前必须先归档"之类的硬需求用
- AfterCompact 压缩完成之后的通知（带是否真的改写了历史、用的是哪一层），退出码只决定要不要打 warn

放行钩子 stdout 的两种去处：
- 纯文本：首个非空行作为上下文进历史，只有 SessionStart / UserPromptSubmit 消费，
  其它事件的 stdout 没去处、照旧忽略；
- 一个 JSON 对象且含 `systemMessage` 键：该文本**只**作为提示显示给用户（Notice），
  不进历史、不进模型，所有事件都认；其余键忽略。JSON 形态优先于首行规则——
  stdout 整体是 JSON 对象时，它的首行不再当上下文（钩子想给用户看一句"已记录到
  审计日志"，不该同时把这句喂给模型）。

事件名表 EVENTS 必须覆盖内核实际触发的每一个名字（tests/test_hooks.py 对着源码
扫 `fire("…")` 字面量核对），少一个就是"文档承诺的事件 hooks.toml 挂不上"。
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

EVENTS = (
    "PreToolUse", "PostToolUse", "ToolFailed", "UserPromptSubmit", "Stop",
    "SessionStart", "SessionEnd",
    "SubagentStart", "SubagentEnd", "BeforeCompact", "AfterCompact",
)
#  带工具名、matcher 对其有意义的事件；也是子 agent 会带下去的那几个
TOOL_EVENTS = ("PreToolUse", "PostToolUse", "ToolFailed")
_ENTRY_KEYS = frozenset({"event", "matcher", "command", "timeout", "on_failure"})
ON_FAILURE_CHOICES = ("allow", "block")

_DEFAULT_TIMEOUT = 30.0
_MAX_TIMEOUT = 600.0
#  喂给 hook 的 output/prompt 字段上限：hook 不需要全文，超长纯属拖慢
_PAYLOAD_TEXT_CAP = 8_000
#  放行钩子 stdout 首行的上限：它会原样进历史（SessionStart 的环境说明），
#  一行就够说清"当前分支 / 值班提示"，更长的说明该写进 AGENTS.md
OUTPUT_LINE_CAP = 2_000


@dataclass(frozen=True)
class Hook:
    event: str
    command: str
    matcher: str = ""  # 正则（re.search 语义），空 = 全匹配
    timeout: float = _DEFAULT_TIMEOUT
    #  钩子自己坏了（超时 / 起不来 / 退出码既非 0 也非 2）时：allow = 放行并告警，
    #  block = 按拦截处理。只在 PreToolUse 上有意义，load_hooks 对别的事件会归零
    on_failure: str = "allow"

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
    #  放行钩子（退出码 0）stdout 的首个非空行，多个钩子按换行拼接。只有
    #  SessionStart / UserPromptSubmit 消费它（注入历史）；别的事件的 stdout
    #  没有去处，照旧忽略
    output: str = ""
    #  stdout 是 JSON 对象且带 systemMessage 时的那段文本：只给用户看（Notice），
    #  不进历史。多个钩子按换行拼接。所有事件都认
    notice: str = ""


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
        on_failure = str(entry.get("on_failure", "allow") or "allow").strip().lower()
        if on_failure not in ON_FAILURE_CHOICES:
            problems.append(
                f"第 {index} 条 on_failure={entry.get('on_failure')!r} 不认识"
                f"（可用：{' | '.join(ON_FAILURE_CHOICES)}），按 allow 算"
            )
            on_failure = "allow"
        elif on_failure == "block" and event != "PreToolUse":
            #  不静默吃掉：用户以为"钩子坏了会拦"，而这个事件上根本没有拦这回事
            problems.append(
                f'第 {index} 条 on_failure = "block" 只对 PreToolUse 生效，{event} 上已忽略'
            )
            on_failure = "allow"
        hooks.append(
            Hook(event=event, command=command, matcher=matcher, timeout=timeout, on_failure=on_failure)
        )
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
        UserPromptSubmit / Stop / SessionStart / SessionEnd 说的是用户这个会话的
        开头和收尾，子 agent 没有这些时刻，不带下去。没有工具类钩子时返回 None
        （触发点零开销）。
        """
        kept = [hook for hook in self.hooks if hook.event in TOOL_EVENTS]
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
        outputs: list[str] = []
        notices: list[str] = []
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
                self._failed(hook, f"超时（>{hook.timeout:.0f}s）", reasons)
                continue
            except OSError as exc:
                self._failed(hook, f"启动失败（{exc}）", reasons)
                continue
            if proc.returncode == 2:
                reason = proc.stderr.strip() or proc.stdout.strip() or "（hook 未给出理由）"
                reasons.append(clip_reason(reason))
            elif proc.returncode != 0:
                self._failed(hook, f"退出码 {proc.returncode}（既非 0 放行也非 2 拦截）", reasons)
            else:
                line, note = parse_stdout(proc.stdout)
                if line:
                    outputs.append(line)
                if note:
                    notices.append(note)
        return Decision(
            blocked=bool(reasons),
            reason="；".join(reasons),
            output="\n".join(outputs),
            notice="\n".join(notices),
        )

    def _failed(self, hook: Hook, what: str, reasons: list[str]) -> None:
        """钩子自己坏了：默认放行并告警；on_failure = block 的改记一条拦截理由。

        两种情况都要让用户看见——放行是静默失效（以为有闸其实没有），拦截则
        要知道是钩子坏了而不是真的违规，否则会对着模型的参数找半天问题。
        """
        if hook.on_failure == "block":
            reasons.append(f"钩子失败（{what}，on_failure = block）：{hook.command}")
            self._notify(f"[hook {what}，按 on_failure = block 拦截：{hook.command}]")
            return
        self._notify(f"[hook {what}，放行：{hook.command}]")


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


def first_line(text: str) -> str:
    """stdout 的首个非空行（截到 OUTPUT_LINE_CAP）；全空返回空串。"""
    for line in text.splitlines():
        if line.strip():
            return _cap_line(line.strip())
    return ""


def _cap_line(line: str) -> str:
    return line if len(line) <= OUTPUT_LINE_CAP else line[:OUTPUT_LINE_CAP] + "…（已截断）"


#  钩子 stdout 里"只给用户看"的那个键
SYSTEM_MESSAGE_KEY = "systemMessage"


def parse_stdout(text: str) -> tuple[str, str]:
    """放行钩子的 stdout → (进历史的首行, 只给用户看的提示)。

    整段 stdout 是一个 JSON 对象时走结构化形态：取 `systemMessage`（非字符串
    按 str 处理、空的当没有），**不再**取首行——否则一段 `{"systemMessage": …}`
    会原样进历史，钩子想说给用户的话反倒喂给了模型。不是 JSON 对象（纯文本、
    JSON 数组、坏 JSON）就按首行规则。
    """
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
        except ValueError:
            data = None
        if isinstance(data, dict):
            note = data.get(SYSTEM_MESSAGE_KEY)
            if note is None:
                return "", ""
            note = str(note).strip()
            return "", _cap_line(note) if note else ""
    return first_line(text), ""
