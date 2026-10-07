"""终端礼仪：注意力通知、窗口标题、状态钩子。

三样都是"小羽转到**等人**的状态时，把人叫回来"的手段——模型跑一轮动辄几分钟，
用户早切去别的窗口了，轮次结束或审批挂起时没人知道，整段等待就白白浪费。
inline 架构没有常驻状态栏可以闪，只能借终端和系统自己的通道：

- **通知**（`XIAOYU_BELL`，默认关）：一轮结束 / 等审批时往终端写一段序列。
  `1` / `bel` 是最朴素的 BEL（`\\x07`），终端把它翻译成提示音、Dock 弹跳或标签页
  高亮，各家自定；`osc9` / `osc777` / `osc99` 是三种"桌面通知"转义序列（分别是
  iTerm2 / WezTerm / ghostty 一路、rxvt 一路、kitty 一路认的），能带一句文案弹
  系统通知；`auto` 按终端的环境变量挑一种（认不出就退回 BEL）。在 tmux 里会用
  DCS 透传包一层，否则序列到不了外层终端。默认关是因为很多人把铃声当噪音，开了
  才响；只在 stdout 是终端时写——管道里一个 0x07 只会污染输出；
- **标题**（`XIAOYU_TITLE=0` 关，默认开）：OSC 0 把窗口/标签页标题设成
  「<状态> · <会话名或目录名> · xiaoyu」，开了几个终端标签一眼认出哪个在跑哪个
  项目、哪个正等着你。状态只有四个词：就绪 / 运行中 / 等审批 / 等输入，随
  waiting() / running() 的调用点更新。具名会话（`--session-id`、终端集成的
  `term-…`）用名字，匿名会话退回目录名。名字先去掉控制字符——标题里塞转义序列
  等于让目录名改写终端状态。退出时恢复：进入时用 CSI 22 t 把旧标题 push 进
  "标题栈"（xterm 系、iTerm2、kitty、WezTerm 都认），之后的状态更新只改 OSC 0
  不再 push，退出 pop 一次精确还原；不认标题栈的终端退回"清空标题"（多数终端
  清空后显示 shell 自己的默认标题）；
- **状态钩子**（`XIAOYU_STATUS_HOOK=<命令>`，默认无）：状态变成"等人"时后台跑
  这条命令，状态字符串作最后一个参数（`waiting_input` / `waiting_approval`），
  同时放进环境变量 `XIAOYU_STATUS`。给系统通知用：macOS 一行 osascript、Linux
  notify-send、或自己写的脚本转发到 IM。命令按 shell 词法切分后直接 exec，
  不经 shell；超时（10s）和失败一律静默——通知本身出问题不能打扰正事。

**只在交互前端（TUI / 明文 REPL）接**：一次性 `-p`、wire、ACP、serve 都没有
"等人"这个状态，宿主自有通知渠道。无遥测、不出网：三样都只落在本机。
"""

from __future__ import annotations

import contextlib
import os
import shlex
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, TextIO

BELL_ENV = "XIAOYU_BELL"
TITLE_ENV = "XIAOYU_TITLE"
HOOK_ENV = "XIAOYU_STATUS_HOOK"

#  状态钩子的两个状态串（钩子命令的最后一个参数）
WAITING_INPUT = "waiting_input"
WAITING_APPROVAL = "waiting_approval"
#  标题里另外两个状态（不触发钩子与通知：它们不是"等人"）
READY = "ready"
RUNNING = "running"

#  状态 → 标题 / 通知里的中文词
STATE_LABELS = {
    READY: "就绪",
    RUNNING: "运行中",
    WAITING_APPROVAL: "等审批",
    WAITING_INPUT: "等输入",
}

#  钩子命令的时限：系统通知是毫秒级的事，10s 还没回来就是挂住了
HOOK_TIMEOUT = 10.0

_TRUTHY = ("1", "true", "yes", "on")
_FALSY = ("", "0", "false", "no", "off")
_BEL = "\x07"
_ST = "\x1b\\"
_TITLE_PUSH = "\x1b[22;0t"
_TITLE_POP = "\x1b[23;0t"

#  通知通道名（XIAOYU_BELL 的取值；auto 解析成其中之一）
BEL = "bel"
OSC9 = "osc9"
OSC777 = "osc777"
OSC99 = "osc99"
CHANNELS = (BEL, OSC9, OSC777, OSC99)

#  set_title 记下的"会话名或目录名"：状态更新只改状态词，名字不变；
#  空串 = 没设过标题（非交互前端），状态更新一律不写
_title_name = ""

#  最近一条钩子线程（测试等它收尾用；生产路径不 join）
_last_hook: threading.Thread | None = None
_hook_lock = threading.Lock()


def _stream(stream: TextIO | None) -> TextIO | None:
    """实际写入的流：显式传入的优先（测试注入），否则 stdout；非终端返回 None。"""
    target = stream if stream is not None else sys.stdout
    try:
        if not target.isatty():
            return None
    except (AttributeError, ValueError):  # 没有 isatty / 已关闭的流
        return None
    return target


def _write(text: str, stream: TextIO | None) -> None:
    target = _stream(stream)
    if target is None:
        return
    with contextlib.suppress(Exception):
        target.write(text)
        target.flush()


def _clean(text: str) -> str:
    """进标题 / 通知的文案：去掉控制字符与换行，压成一行。"""
    from . import ui

    return " ".join(ui.strip_sequences(str(text)).split())


# ---------- 通知 ----------


def bell_channel() -> str:
    """XIAOYU_BELL 解析成通道名；关着返回空串。

    `1` / `true` 与 `bel` 同义（老写法）；`auto` 按终端挑：kitty 认 OSC 99，
    iTerm2 / WezTerm / ghostty 认 OSC 9，其余退回 BEL——认错不会有害，最多
    是终端把一段不认识的 OSC 静默吞掉、连铃都不响，所以只给有把握的几家选
    专用通道。不认识的取值当 bel：用户显然是想开，不该因为拼错而无声。
    """
    raw = os.environ.get(BELL_ENV, "").strip().lower()
    if raw in _FALSY:
        return ""
    if raw in CHANNELS:
        return raw
    if raw == "auto":
        return _auto_channel(os.environ)
    return BEL


def _auto_channel(env: Any) -> str:
    if env.get("KITTY_WINDOW_ID") or env.get("TERM", "") == "xterm-kitty":
        return OSC99
    if env.get("TERM_PROGRAM", "") in ("iTerm.app", "WezTerm", "ghostty"):
        return OSC9
    return BEL


def bell_enabled() -> bool:
    return bool(bell_channel())


def tmux_passthrough(sequence: str) -> str:
    """在 tmux 里把序列包成 DCS 透传（`ESC P tmux; … ESC \\`），正文里的 ESC 加倍。

    不包的话 tmux 自己吃掉 OSC，外层终端什么都收不到。只按 TMUX 环境变量判断
    ——它是 tmux 给每个 pane 的进程注入的，比 TERM 可靠（TERM 常被用户改掉）。
    """
    if not os.environ.get("TMUX"):
        return sequence
    return "\x1bPtmux;" + sequence.replace("\x1b", "\x1b\x1b") + _ST


def notification_sequence(channel: str, state: str = "", name: str = "") -> str:
    """某个通道的通知字节序列（未包 tmux）。

    文案带状态与会话名/目录名：切去别的窗口的人看一眼通知就知道是哪个项目、
    在等什么。BEL 没有文案位，只响。
    """
    label = STATE_LABELS.get(state, "")
    title = " · ".join(part for part in ("小羽", _clean(name)) if part)
    body = label or "在等你"
    if channel == OSC9:
        return f"\x1b]9;{title} · {body}{_BEL}"
    if channel == OSC777:
        return f"\x1b]777;notify;{title};{body}{_BEL}"
    if channel == OSC99:
        #  kitty：同一个 i= 的两段，先标题（d=0 表示还有后续）再正文
        return f"\x1b]99;i=xiaoyu:d=0;{title}{_ST}\x1b]99;i=xiaoyu:d=1:p=body;{body}{_ST}"
    return _BEL


def ring(stream: TextIO | None = None, state: str = "") -> None:
    """发一次通知（opt-in，且只对终端）。state 进文案；名字取 set_title 记下的。"""
    channel = bell_channel()
    if channel == BEL:
        _write(_BEL, stream)  # 单字节控制符 tmux 本来就透传，不必包
    elif channel:
        _write(tmux_passthrough(notification_sequence(channel, state, _title_name)), stream)


# ---------- 标题 ----------


def title_enabled() -> bool:
    return os.environ.get(TITLE_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


def title_text(workspace: Path | str, state: str = READY, session: str = "") -> str:
    """标题文案：「<状态> · <会话名或目录名> · xiaoyu」，名字去掉控制字符与换行。"""
    name = _clean(session) or _clean(Path(str(workspace)).name or str(workspace))
    parts = [STATE_LABELS.get(state, ""), name, "xiaoyu"]
    return " · ".join(part for part in parts if part)


def session_label(session_log: Any) -> str:
    """具名会话的名字（`--session-id`、终端集成的 `term-…`）；匿名会话返回空串。

    参数是 SessionLog（或任何带 .path 的替身）；没有会话文件也返回空串，标题
    退回目录名。
    """
    path = getattr(session_log, "path", None)
    if path is None:
        return ""
    from .session_log import named_session

    return named_session(Path(str(path)))


def set_title(workspace: Path | str, stream: TextIO | None = None, session: str = "") -> None:
    """进交互前端：push 旧标题，再设成「就绪 · 名字 · xiaoyu」。

    之后的状态更新（running / waiting）只改 OSC 0，不再 push——标题栈要和
    clear_title 的那一次 pop 配平。
    """
    global _title_name
    _title_name = _clean(session) or _clean(Path(str(workspace)).name or str(workspace))
    if not title_enabled():
        return
    _write(f"{_TITLE_PUSH}\x1b]0;{title_text(workspace, READY, session)}{_BEL}", stream)


def update_title(state: str, stream: TextIO | None = None) -> None:
    """只改标题里的状态词。没设过标题（非交互前端）或关了就不写。"""
    if not _title_name or not title_enabled():
        return
    _write(f"\x1b]0;{title_text(_title_name, state)}{_BEL}", stream)


def clear_title(stream: TextIO | None = None) -> None:
    """退出时还原标题：先清空（给不认标题栈的终端），再 pop（认的终端据此还原）。"""
    global _title_name
    _title_name = ""
    if not title_enabled():
        return
    _write(f"\x1b]0;{_BEL}{_TITLE_POP}", stream)


# ---------- 状态钩子 ----------


def hook_command() -> list[str]:
    """钩子命令的 argv；没配或切不开返回空列表。"""
    raw = os.environ.get(HOOK_ENV, "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return []


def _run_hook(argv: list[str], state: str) -> None:
    env = {**os.environ, "XIAOYU_STATUS": state}
    with contextlib.suppress(Exception):
        subprocess.run(
            [*argv, state],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=HOOK_TIMEOUT,
            check=False,
        )


def status_hook(state: str) -> None:
    """后台跑状态钩子（配了才跑）。不阻塞调用方，不抛任何异常。"""
    argv = hook_command()
    if not argv:
        return
    global _last_hook
    thread = threading.Thread(
        target=_run_hook, args=(argv, state), daemon=True, name="xiaoyu-status-hook"
    )
    with _hook_lock:
        _last_hook = thread
    thread.start()


def wait_hooks(timeout: float = HOOK_TIMEOUT) -> None:
    """等最近一条钩子线程收尾（测试用；生产路径从不等）。"""
    with _hook_lock:
        thread = _last_hook
    if thread is not None:
        thread.join(timeout)


# ---------- 状态切换（前端的调用点） ----------


def waiting(state: str, stream: TextIO | None = None) -> None:
    """状态切到"等人"：标题 + 通知 + 钩子一起发。state 取 WAITING_INPUT / WAITING_APPROVAL。"""
    update_title(state, stream)
    ring(stream, state)
    status_hook(state)


def running(stream: TextIO | None = None) -> None:
    """状态切回"运行中"（一轮开始、审批或提问回答之后）：只改标题，不响不叫。"""
    update_title(RUNNING, stream)
