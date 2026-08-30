"""TUI 前端（第 2 步）：prompt_toolkit 输入层 + rich 渲染层。

可选依赖（pip install "xiaoyu-agent[tui]"）；缺包时 import 本模块会
ImportError，cli 捕获后自动退回明文 REPL。

设计约束（与全屏 TUI 的分界线）：
- **不进 alt-screen**：对话留在终端原生 scrollback 里，可滚动、可复制、可 grep。
  受控区域只有输入行（含补全/历史）、工具执行中的
  spinner 活区（transient，结束即消失）和确认菜单（erase_when_done）。
- **没有常驻状态栏**：bottom_toolbar 只在输入行挂起时存在，模型一跑就消失，
  时隐时现反而像 bug（试过，拆了）。模型/上下文/token 走 /model /context /usage 查。
- **改不了 scrollback，但可以补打**：折叠内容全部留底（RichSink.folded），
  Ctrl-O 开详情时回放上一轮被折叠的输出——"还有 N 行"是可兑现的承诺。
- **agent 保持全同步**：流式输出发生在没有 prompt 挂起的时刻，
  不需要 patch_stdout、不需要线程（rich Status 自带的刷新线程只画活区一行）。
- **流式正文按纯文本直出**：inline 模式做 markdown 重绘会污染 scrollback，
  已滚出屏幕的内容也无法和最终渲染保持一致——宁可朴素，不做花活。

核心交互三块：
1. 工具调用渐进式披露：pending 一行锚点 → running 活区 spinner →
   completed 折叠一行（出错自动展开、Ctrl-O 切换详略）→ denied 一行定格；
2. 权限确认三选项菜单：允许一次 / 本会话不再问 / 总是允许（写规则）/
   拒绝并给替代指令，str_replace 预览升级为 unified diff；
3. 输入行前缀语义：/ 命令补全带说明、! 直跑 shell 且输出进上下文、
   @ 模糊补全文件路径、# 记项目备忘、双击 Esc 取回上一条、Ctrl-C×2 退出。

工具与确认框的细节打磨：
- 连续只读工具（read_file/grep/list_files）折叠成一行汇总，结果统一 ⎿ 缩进，
  终态按 成功绿/失败红/拒绝黄 着色；detail 模式（Ctrl-O）成功输出不再截断；
- 确认框：问句带宾语（"要修改 foo.py 吗？"）、「总是允许」的规则写入前可编辑、
  Tab = 批准并附言（附言随 tool result 回灌模型）、diff 预览加配对行词级高亮
  （变化占比超 40% 回退整行红绿）；
- 输入行：大段粘贴折叠成 chip（旁路存储 + 占位符 + 退格原子删除 + 提交展开）、
  Tab 先补最长公共前缀再轮换（readline 直觉）。
  ↑↓ 的「视觉行 → 逻辑行 → 历史」三级回退经核实 prompt_toolkit 自带
  （buffer.auto_up/auto_down），不重复造。

窄终端与长会话的适配：
- 配色抽成语义 token：渲染处只说意图，颜色住在 theme.py，深浅两套表。
  附带解决了浅色终端上黄色警告近乎隐形的问题；
- 一切宽度按终端实际列数算：原先写死的 160 在 80 列终端上会把
  "折叠成一行"折成两行；
- 折叠提示在一轮收尾统一提一次：溢出是整轮的一个状态，不是每个折叠行
  各自的属性。行内只说藏了多少；
- 按键收进 keys.py 的绑定表，注册与帮助同源；
- spinner 三段式：安静 → 轮播上手提示 → 报超时预算；
- 等待期的抢跑输入主动接住再预填（只预填不自动提交）；
- 第三方 warn/log 收进事件流，但**不劫持 stdout**——流式正文就是裸 print，
  劫持会把自己的正文吃掉；
- 等模型期间的活区指示：以前从提交到首个 token 之间一个事件都没有，
  屏幕停在用户刚敲的那行不动。协议补了 request.started/ended 一对，
  前端才有东西可画。

**活区与裸 print 的次序**：rich 的 Live 启动时会接管 sys.stdout / sys.stderr，
停止时还原成启动那一刻的对象。所以首个 TextDelta 必须**先**收活区再 print，
否则两者抢同一片屏幕；测试里若把 redirect_stdout 套在 Live 内部，stop() 会
把那层 redirect 一并冲掉。

整屏 TUI 常见的那套（全量重绘、虚拟列表、鼠标捕获、vim 缓冲区）刻意不做：
那些都是"自己接管整屏渲染"的代价，与本文件开头的 inline 约束相反。
kitty 键盘协议也不开：prompt_toolkit 3.0.53 的 vt100 解析器不认 CSI-u，
开了会让整个输入层失灵；Shift+Enter 改由 editor_setup 从编辑器那侧解决。
"""

from __future__ import annotations

import contextlib
import difflib
import itertools
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from prompt_toolkit import PromptSession
from prompt_toolkit.application import Application, get_app_or_none, run_in_terminal
from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from rich.console import Console
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme as RichTheme

from . import command_check, keys, media, modes, theme, ui
from .agent import Agent
from .config import user_config_dir
from .events import (
    Notice,
    NoticeLevel,
    PlanUpdated,
    RequestEnded,
    RequestStarted,
    SteerAccepted,
    TextDelta,
    TextEnd,
    ToolCompleted,
    ToolDenied,
    ToolPending,
    ToolPurpose,
    ToolRunning,
    UIEvent,
)
from .permissions import Permissions, parse_rule, suggest_allow_rule
from . import render
from .render import args_preview

#  计划状态的显示符号与 render.PlainSink 保持一致；样式一律走 theme 的语义 token
_PLAN_STYLES = {
    "completed": ("✔", "text.secondary"),
    "in_progress": ("▶", "text.accent"),
    "pending": ("○", "text.primary"),
}
_NOTICE_STYLES: dict[str, str] = {
    "info": "text.secondary",
    "warn": "status.warning",
    "error": "status.error",
}

#  str_replace 确认预览的 diff 行数上限：看得出改了什么就够，不是代码审查。
#  真正的上限还要跟终端高度取小——预览比屏幕还高的话，最该看的开头会被顶走。
_DIFF_CAP = 40
#  确认框里 diff 之外的固定开销（标题、上下框线、选项菜单）
_CONFIRM_CHROME = 12


def _word_fragments(old: str, new: str) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """配对 -/+ 行的词级高亮：
    变化字符占比超过阈值就放弃词级、回退整行红绿——大改写时词级反而花。
    返回的两行片段都带 -/+ 前缀。"""
    _WHOLE_LINE = 0.4
    matcher = difflib.SequenceMatcher(None, old, new)
    opcodes = matcher.get_opcodes()
    changed = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in opcodes if tag != "equal")
    if changed / max(len(old), len(new), 1) > _WHOLE_LINE:
        return [("diff.removed", f"-{old}")], [("diff.added", f"+{new}")]
    removed: list[tuple[str, str]] = [("diff.removed", "-")]
    added: list[tuple[str, str]] = [("diff.added", "+")]
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            removed.append(("diff.removed", old[i1:i2]))
            added.append(("diff.added", new[j1:j2]))
            continue
        if i2 > i1:
            removed.append(("diff.removed.word", old[i1:i2]))
        if j2 > j1:
            added.append(("diff.added.word", new[j1:j2]))
    return removed, added


def _diff_rows(
    old: str, new: str, width: int, cap: int | None = _DIFF_CAP
) -> list[list[tuple[str, str]]]:
    """str_replace 确认预览：每行是 (样式, 文本) 片段序列的 unified diff
    （带 2 行上下文），相邻的 -/+ 行配对做词级高亮——大段替换时用户真正
    要看的是变了哪几个词，而不是两坨对不上位置的文本。

    `width` 是这一行留给 diff 文本的列数（调用方已扣掉 "  │ " 前缀）；
    带 -/+ 记号的行再自扣一列。
    """
    rows: list[list[tuple[str, str]]] = []
    inner = max(width - 1, 20)
    raw = [
        line
        for line in difflib.unified_diff(old.split("\n"), new.split("\n"), n=2, lineterm="")
        if not line.startswith(("---", "+++"))
    ]
    index = 0
    while index < len(raw):
        line = raw[index]
        if line.startswith("@@"):
            rows.append([("diff.hunk", ui.preview(line, width))])
            index += 1
            continue
        if line.startswith("-"):
            end_minus = index
            while end_minus < len(raw) and raw[end_minus].startswith("-"):
                end_minus += 1
            end_plus = end_minus
            while end_plus < len(raw) and raw[end_plus].startswith("+"):
                end_plus += 1
            minus_lines = raw[index:end_minus]
            plus_lines = raw[end_minus:end_plus]
            for offset, minus_line in enumerate(minus_lines):
                old_text = ui.preview(minus_line[1:], inner)
                if offset < len(plus_lines):
                    new_text = ui.preview(plus_lines[offset][1:], inner)
                    removed, added = _word_fragments(old_text, new_text)
                    rows.append(removed)
                    rows.append(added)
                else:
                    rows.append([("diff.removed", f"-{old_text}")])
            for plus_line in plus_lines[len(minus_lines) :]:
                rows.append([("diff.added", f"+{ui.preview(plus_line[1:], inner)}")])
            index = end_plus
            continue
        style = "diff.added" if line.startswith("+") else "text.secondary"
        rows.append([(style, ui.preview(line, width))])
        index += 1
    if cap is not None and len(rows) > cap:
        remainder = len(rows) - cap
        rows = rows[:cap] + [[("text.secondary", f"…还有 {remainder} 行")]]
    return rows


#  粘贴 chip 占位符：
#  任意位置匹配用于提交前展开；行尾匹配用于退格原子删除
_PASTE_REF = re.compile(r"\[粘贴 #(\d+) · \d+ 行\]")
_PASTE_REF_TAIL = re.compile(r"\[粘贴 #\d+ · \d+ 行\]$")

#  图片 chip：与文本 chip 同一套"旁路存储 + 占位符 + 退格原子删除"，但**提交时
#  不展开成文本**——它要变成 content 里的一个图片部件（见 Tui._content_of）。
#  文本 chip 那半是既有实现，这里只加媒体半边
_IMAGE_REF = re.compile(r"\[图片 #(\d+)[^\]]*\]")
_IMAGE_REF_TAIL = re.compile(r"\[图片 #\d+[^\]]*\]$")


#  非按键类的轮播补充：收录标准与 keys.tips() 相同（"不主动说就没人会发现"），
#  但主题不是键位、进不了按键绑定表，在这里独立维护。保持短句。
_EXTRA_TIPS = (
    "常用 auto 档？xiaoyu config --set XIAOYU_MODE=auto 设为个人默认",
)

#  轮播起点逐实例推进：单次运行的展示窗口（_QUIET→_LONG）只够放三四条，
#  恒从表头开始的话表尾的提示永远轮不到——12+ 条里只有前 4 条见过天日
_TIP_START = itertools.count()


class _RunningLine:
    """活区 spinner 的文案：rich 的刷新线程每帧重新渲染它，耗时随之跳动。

    三段式：

    - 前 `_QUIET` 秒只报名字和耗时。绝大多数工具一闪而过，在那半秒里插花活
      纯属打扰；
    - 之后开始轮播上手提示，把等待时间变成教学位——Ctrl-O、@、# 这些功能
      没人会主动去发现，而这正是用户闲着盯屏幕的时刻。按键类提示取自
      按键绑定表，非按键类（环境变量一类）在 _EXTRA_TIPS 补充；起点
      逐实例推进（_TIP_START），跨运行轮完整张表；
    - 超过 `_LONG` 秒改报"已跑多久 / 上限多久"。"长时间没有输出就另行提示"
      的思路在这里不成立（工具是同步执行、结果一次性返回的，执行期间本就
      没有输出），但它想解决的问题——用户不知道该不该继续等——是真的。
      对 bash 来说，剩余的超时预算才是能回答这个问题的信息。
    """

    _QUIET = 4.0  # 这之前不打扰
    _CYCLE = 8.0  # 每条提示停留多久
    _LONG = 30.0  # 这之后换成耗时/上限

    def __init__(self, name: str, timeout: int | None = None, verb: str = "运行中") -> None:
        self.name = name
        self.timeout = timeout
        self.verb = verb
        #  见 _TIP_START：跨运行把整张提示表轮完，而不是每次都从头四条开始
        self._tip_start = next(_TIP_START)
        self.started = time.monotonic()

    def __rich__(self) -> Text:
        elapsed = time.monotonic() - self.started
        return Text(
            f"{self.name} {self.verb} · {elapsed:.0f}s · {self._suffix(elapsed)}",
            style="text.secondary",
        )

    def _suffix(self, elapsed: float) -> str:
        if elapsed >= self._LONG:
            budget = f"上限 {self.timeout}s · " if self.timeout else ""
            return f"{budget}Ctrl-C 中断"
        if elapsed < self._QUIET:
            return "Ctrl-C 中断"
        rotation = [*keys.tips(), *_EXTRA_TIPS]
        if not rotation:
            return "Ctrl-C 中断"
        index = (self._tip_start + int((elapsed - self._QUIET) // self._CYCLE)) % len(rotation)
        return rotation[index]


#  连续的只读探索工具折叠成一行汇总：
#  coding agent 一半调用是这类，逐条打印会把真正的正文和写操作淹掉
_RO_TOOLS = {"read_file": "读文件", "grep": "搜索", "list_files": "列目录"}

#  标记 Console 已挂过语义主题，避免子 agent 共用 Console 时重复入栈
_THEMED = "_xiaoyu_themed"


class NoticeCapture:
    """把 warnings 与 logging 收进事件流。

    第三方库（httpx、urllib3…）随手 warn/log 是直接打到 stderr 的，而 stderr
    不经过 Console，正好插进 spinner 活区把它搅花，还可能落在半行正文中间。
    收口成 Notice 事件，交给 sink 统一在活区之上打印。

    **刻意不劫持 stdout**：小羽自己的流式正文就是裸 print（见
    RichSink._text_delta，那是为了绕开 rich 的按行渲染），全局劫持 stdout
    会把自己的正文吃掉——整屏渲染的 TUI 才劫持得起，因为它们的正文
    不走 stdout。

    logging 只在根 logger 没有任何 handler 时接管：那种情况下日志会经
    lastResort 打到 stderr（正是要拦的）；一旦宿主自己配过 logging，
    那是人家的输出编排，不该抢。
    """

    def __init__(self, sink: "RichSink") -> None:
        self.sink = sink
        self._saved_showwarning: Any = None
        self._handler: Any = None

    def __enter__(self) -> "NoticeCapture":
        import logging
        import warnings

        self._saved_showwarning = warnings.showwarning
        warnings.showwarning = self._on_warning

        root = logging.getLogger()
        if not root.handlers:
            self._handler = _NoticeHandler(self.sink)
            root.addHandler(self._handler)
        return self

    def __exit__(self, *exc: Any) -> None:
        import logging
        import warnings

        warnings.showwarning = self._saved_showwarning
        if self._handler is not None:
            logging.getLogger().removeHandler(self._handler)
            self._handler = None

    def _on_warning(self, message, category, filename, lineno, file=None, line=None) -> None:  # noqa: ANN001
        #  只留"哪个类别、说了什么"：调用栈位置对用户没有意义，
        #  真要排查的人会开 -W error
        self.sink.emit(Notice(f"  {category.__name__}: {message}", "warn"))


class _NoticeHandler:
    """把 LogRecord 转成 Notice 的 logging handler。

    不继承 logging.Handler 是为了让本模块在没装 tui 依赖时也别多拖一个
    import——这里只需要 handle/level 这几个鸭子接口。
    """

    level = 0

    def __init__(self, sink: "RichSink") -> None:
        self.sink = sink

    def handle(self, record: Any) -> None:
        import logging

        level: NoticeLevel = "info"
        if record.levelno >= logging.ERROR:
            level = "error"
        elif record.levelno >= logging.WARNING:
            level = "warn"
        self.sink.emit(Notice(f"  {record.name}: {record.getMessage()}", level))

    #  logging 在派发时会摸这几个属性
    def createLock(self) -> None:  # noqa: N802 - logging 的接口名
        pass

    def acquire(self) -> None:
        pass

    def release(self) -> None:
        pass


class DedupedHistory(FileHistory):
    """输入历史去重。

    只去**连续**重复：反复回车重跑同一条指令是常态，不去重的话 ↑ 要按十几次
    才能翻过上一批。间隔出现的相同输入保留——那里头有时序信息（"我又跑了一次
    它"本身是事实），全局去重会把它抹掉。

    读取时也去一遍：改这条之前写下的历史文件里已经堆了重复。
    """

    def load_history_strings(self) -> Iterable[str]:
        previous: str | None = None
        for item in super().load_history_strings():
            if item != previous:
                yield item
            previous = item

    def append_string(self, string: str) -> None:
        #  _loaded_strings 是 prompt_toolkit 的"最近在前"缓存，第 0 项即上一条。
        #  在这一层拦截而不是拦 store_string：否则文件去重了、本次会话的内存
        #  历史里还留着重复，↑ 照样要多按几下。
        recent = self._loaded_strings[0] if self._loaded_strings else None  # noqa: SLF001
        if string == recent:
            return
        super().append_string(string)


def _register(handlers: dict[str, Any], *categories: str) -> KeyBindings:
    """按绑定表注册一组按键。

    表与实现必须严丝合缝：表里带 keys 却没实现，帮助就在承诺一个按不动的键；
    实现了却没进表，用户永远发现不了它。两种情况都当场抛，不留给运行期。
    """
    wanted = keys.registered_in(*categories)
    if wanted != handlers.keys():
        missing = ", ".join(sorted(wanted - handlers.keys())) or "无"
        extra = ", ".join(sorted(handlers.keys() - wanted)) or "无"
        raise RuntimeError(f"按键表与实现不一致：表里缺实现 [{missing}]，实现未进表 [{extra}]")
    bindings = KeyBindings()
    for item in keys.BINDINGS:
        handler = handlers.get(item.command)
        if handler is None:
            continue
        for combo in item.keys:
            bindings.add(*combo)(handler)
    return bindings


def apply_theme(console: Console) -> None:
    """把语义 token 挂到 Console 上，之后 `style="status.error"` 才认得。

    对传进来的 Console 也要挂（测试和调用方都可能自带 Console），所以用标记
    位保证栈深恒为 1。`theme.set_mode()` 之后重新调用即可换肤——已经打出去的
    内容改不了，换肤只对之后的输出生效。
    """
    if getattr(console, _THEMED, False):
        console.pop_theme()
    console.push_theme(RichTheme(theme.rich_theme()))
    setattr(console, _THEMED, True)


def inline_select(
    title: str,
    options: list[tuple[Any, str, str]],
    amend: bool = True,
    expand: Callable[[], None] | None = None,
) -> Any:
    """不进 alt-screen 的行内单选菜单（确认框与会话选择共用的通用件）。

    按键由 keys.BINDINGS 的「确认菜单」分类驱动（底部提示也从表渲染）。
    options 每项 (返回值, 标签, 快捷键)；数字直选只绑前 9 项（"10" 会被
    prompt_toolkit 当成 1、0 两键序列，让 "1" 陷入歧义等待）。
    amend=True 时 Tab 返回 ("amend", 选中值)（确认框的"批准并附言"）；
    amend=False 时 Tab 等同 Enter，提示行也不再承诺附言。
    expand 非空时 Ctrl-O 调它补打全文（确认框预览被截断时传入；打进
    scrollback 定格，菜单原地重画——刻意不进 pager/alt-screen，
    保持与 Ctrl-O 详情回放同构的补打形态）；为 None 则该键 no-op
    且提示行不承诺它。
    取消返回 None。菜单退出即擦除（erase_when_done），决定由调用方回显
    一行定格进 scrollback。
    """
    selected = 0
    expanded = False

    def _up(event: Any) -> None:
        nonlocal selected
        selected = (selected - 1) % len(options)

    def _down(event: Any) -> None:
        nonlocal selected
        selected = (selected + 1) % len(options)

    def _accept(event: Any) -> None:
        event.app.exit(result=options[selected][0])

    def _amend(event: Any) -> None:
        if amend:
            event.app.exit(result=("amend", options[selected][0]))
        else:
            event.app.exit(result=options[selected][0])

    def _cancel(event: Any) -> None:
        event.app.exit(result=None)

    def _expand(event: Any) -> None:
        #  一次性：全文已定格进 scrollback，再按只会重复倾倒同一坨
        nonlocal expanded
        if expand is None or expanded:
            return
        expanded = True
        run_in_terminal(expand)

    bindings = _register(
        {
            "menu.up": _up,
            "menu.down": _down,
            "menu.accept": _accept,
            "menu.amend": _amend,
            #  Space 勾选只属于提问面板的多选题；确认菜单里按下不能有任何效果
            #  （若当确认键，误触空格=误批准）
            "menu.toggle": lambda event: None,
            "menu.expand": _expand,
            "menu.cancel": _cancel,
        },
        keys.MENU,
    )

    def bind_choice(value: Any):
        def handler(event: Any) -> None:
            event.app.exit(result=value)

        return handler

    for index, (value, _label, shortcut) in enumerate(options):
        if index < 9:
            bindings.add(str(index + 1))(bind_choice(value))
        if shortcut:
            bindings.add(shortcut)(bind_choice(value))

    #  提示行不承诺没有效果的键：无附言语义剔 Tab、无全文可看剔 Ctrl-O
    exclude = ["menu.toggle"] if amend else ["menu.amend", "menu.toggle"]
    if expand is None:
        exclude.append("menu.expand")
    hint = keys.menu_hint(exclude=tuple(exclude))

    def fragments() -> list[tuple[str, str]]:
        #  class 名是 theme 的 token 把点换成横杠（ptk 的 class 不能带点）
        rows: list[tuple[str, str]] = [("class:menu-title", f"  {title}\n")]
        for index, (_value, label, shortcut) in enumerate(options):
            marks = [str(index + 1)] if index < 9 else []
            if shortcut:
                marks.append(shortcut)
            tail = f"  ({'/'.join(marks)})" if marks else ""
            if index == selected:
                rows.append(("class:menu-selected", f"  ❯ {label}{tail}\n"))
            else:
                rows.append(("", f"    {label}{tail}\n"))
        rows.append(("class:menu-hint", f"    {hint}"))
        return rows

    window = Window(
        FormattedTextControl(fragments, focusable=True, show_cursor=False),
        height=len(options) + 2,
        always_hide_cursor=True,
    )
    application: Application = Application(
        layout=Layout(window),
        key_bindings=bindings,
        full_screen=False,
        erase_when_done=True,
        style=Style.from_dict(theme.ptk_style()),
    )
    return application.run()


#  提问面板自动附加的自由输入项（模型不许自己造「其他」，
#  系统统一加，保证它永远存在且行为一致）
OTHER_LABEL = "其他（自由输入）"


def question_select(
    title: str,
    options: list[tuple[str, str]],
    multi: bool = False,
    checked: set[int] | None = None,
    selected: int = 0,
) -> int | list[int] | None:
    """ask_user 的行内选择面板（inline 形态）。

    与 inline_select 的差异：options 是 (标签, 说明) 而非 (值, 标签, 快捷键)
    ——说明另起一行淡色显示（模型给的选项没有快捷键可言，数字直选就够）；
    multi=True 时 Space/数字是勾选开关、Enter 提交勾选集合。
    返回：单选 → 选中项下标；多选 → 勾选下标列表（显示顺序）；取消 → None。
    面板退出即擦除，答案由调用方回显一行定格进 scrollback（与确认框同一纪律）。
    """
    marked: set[int] = set(checked or ())
    cursor = min(max(selected, 0), len(options) - 1)

    def _up(event: Any) -> None:
        nonlocal cursor
        cursor = (cursor - 1) % len(options)

    def _down(event: Any) -> None:
        nonlocal cursor
        cursor = (cursor + 1) % len(options)

    def _accept(event: Any) -> None:
        if multi:
            event.app.exit(result=sorted(marked))
        else:
            event.app.exit(result=cursor)

    def _toggle(event: Any) -> None:
        if multi:
            marked.symmetric_difference_update({cursor})

    def _cancel(event: Any) -> None:
        event.app.exit(result=None)

    bindings = _register(
        {
            "menu.up": _up,
            "menu.down": _down,
            "menu.accept": _accept,
            #  这里没有"批准并附言"语义，Tab 等同 Enter（与纯单选菜单一致）
            "menu.amend": _accept,
            "menu.toggle": _toggle,
            #  提问面板没有被截断的预览，Ctrl-O 无事可做
            "menu.expand": lambda event: None,
            "menu.cancel": _cancel,
        },
        keys.MENU,
    )

    def bind_digit(index: int):
        def handler(event: Any) -> None:
            nonlocal cursor
            cursor = index
            if multi:
                marked.symmetric_difference_update({index})
            else:
                event.app.exit(result=index)

        return handler

    for index in range(min(len(options), 9)):
        bindings.add(str(index + 1))(bind_digit(index))

    exclude = (
        ("menu.amend", "menu.expand") if multi else ("menu.amend", "menu.toggle", "menu.expand")
    )
    hint = keys.menu_hint(exclude=exclude)

    def fragments() -> list[tuple[str, str]]:
        rows: list[tuple[str, str]] = [("class:menu-title", f"  {title}\n")]
        for index, (label, description) in enumerate(options):
            mark = f"[{'✓' if index in marked else ' '}] " if multi else ""
            tail = f"  ({index + 1})" if index < 9 else ""
            if index == cursor:
                rows.append(("class:menu-selected", f"  ❯ {mark}{label}{tail}\n"))
            else:
                rows.append(("", f"    {mark}{label}{tail}\n"))
            if description:
                rows.append(("class:menu-hint", f"      {'    ' if multi else ''}{description}\n"))
        rows.append(("class:menu-hint", f"    {hint}"))
        return rows

    height = 2 + len(options) + sum(1 for _label, description in options if description)
    window = Window(
        FormattedTextControl(fragments, focusable=True, show_cursor=False),
        height=height,
        always_hide_cursor=True,
    )
    application: Application = Application(
        layout=Layout(window),
        key_bindings=bindings,
        full_screen=False,
        erase_when_done=True,
        style=Style.from_dict(theme.ptk_style()),
    )
    return application.run()


def ask_questions(questions: list[dict[str, Any]], console: Console) -> dict[str, str]:
    """ask_user 工具的 TUI 侧交互：顺序逐题问，返回 {问题: 答案}。

    多题刻意**不做 Tab 分页**：行内面板 erase_when_done、一次一屏，
    顺序逐题 + 答完回显一行定格，才是"补打 scrollback"哲学下的自然形态
    （分页属于常驻面板的交互，与 inline 约束相反）。题号进标题（"（1/3）"）。

    「其他」项自动附加在每题末尾；选中后转行内自由输入，Esc 放弃输入退回
    面板，已敲的草稿保留、下次进入预填。
    多选题勾了「其他」时，提交前先问自由输入，与勾选项合并成一个答案。
    面板上 Esc = 不再回答剩余问题（已答的部分照常返回，交给工具层解释）。
    """
    answers: dict[str, str] = {}
    total = len(questions)
    session: PromptSession | None = None

    def prompt_other(draft: str) -> str | None:
        """自由输入一行；Esc/Ctrl-C/EOF → None（退回面板，草稿由调用方保留）。"""
        nonlocal session
        if session is None:
            session = PromptSession()
        try:
            return session.prompt(
                [(theme.ptk("menu.title"), "  自由回答：")], default=draft
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None

    for number, item in enumerate(questions, start=1):
        rows = [
            (str(option.get("label", "")), str(option.get("description", "")))
            for option in item["options"]
        ]
        rows.append((OTHER_LABEL, ""))
        other_index = len(rows) - 1
        multi = bool(item.get("multi_select"))
        title = item["question"] + (f"（{number}/{total}）" if total > 1 else "")
        draft = ""
        checked: set[int] = set()
        cursor = 0
        answer: str | None = None
        while True:
            try:
                result = question_select(title, rows, multi=multi, checked=checked, selected=cursor)
            except Exception:  # noqa: BLE001 - 非常规终端起不了面板：退回文本问答，提问永远可用
                from .cli import ask_one_text

                result = None
                answer = ask_one_text(item, position=f"{number}/{total}" if total > 1 else "")
                break
            if result is None:
                break  # Esc：剩余问题不问了
            if multi:
                checked = set(result)
                labels = [rows[i][0] for i in sorted(checked) if i != other_index]
                if other_index in checked:
                    text = prompt_other(draft)
                    if text is None:
                        cursor = other_index
                        continue
                    draft = text
                    if not text and not labels:
                        continue  # 只勾了「其他」却没写内容：留在面板
                    answer = ", ".join([*labels, text] if text else labels)
                    break
                if not labels:
                    continue  # 空提交无意义，留在面板
                answer = ", ".join(labels)
                break
            if result == other_index:
                text = prompt_other(draft)
                if not text:
                    draft = text if text is not None else draft
                    cursor = other_index
                    continue
                answer = text
                break
            answer = rows[result][0]
            break
        if answer is None:
            break
        answers[item["question"]] = answer
        console.print(Text(f"  ✔ {item['question']}：{answer}", style="text.secondary"))
    return answers


class RichSink:
    """rich 渲染的 UISink：四态工具生命周期的渐进式披露。

    - tool.pending   一行 ● 锚点（权限确认也发生在它下面）；
                     连续只读工具不逐条打，攒进折叠组
    - tool.running   活区 spinner（rich Status，transient：结束即消失，
                     scrollback 不留刷新残迹）
    - tool.completed 折叠成一行 ⎿ 摘要（绿）+ 耗时；出错 ✗（红）自动展开头几行；
                     detail 开关（输入行按 Ctrl-O）打开时成功输出完整展开不截断；
                     只读折叠组在非终结事件到来时汇成一行 ⎿ 读文件 ×2 · 搜索 ×1
    - tool.denied    一行 ⨯ 定格（黄；deny 规则拦截 / 用户拒绝）

    indent/verbose 语义与 PlainSink 的 quiet 子 agent 对齐。子 agent 必须共用
    同一个 Console（quiet_child）：rich 的 Live 只能把**同一个** Console 的
    输出抬到活区上方，子 agent 自建 Console 或裸 print 会把 spinner 搅花。

    全部走 Text 对象拼装，不吃 rich markup——工具参数里出现 `[` 不会炸样式。
    """

    _ERROR_LINES = 8  # 出错时自动展开的行数
    _REPLAY_LINES = 200  # 回放留底时单项上限（防一次 read_file 大文件把终端打爆）

    def __init__(self, console: Console | None = None, indent: str = "", verbose: bool = True) -> None:
        #  highlight=False：不要 rich 的自动语法猜测把路径/数字染得花花绿绿；
        #  soft_wrap=True：长行交给终端换行，不按面板宽度硬折（与 print 行为一致）
        self.console = console or Console(highlight=False, soft_wrap=True)
        apply_theme(self.console)
        self.indent = indent
        self.verbose = verbose
        #  Ctrl-O 切换。inline 模式改不了已滚出的 scrollback，开关只对之后的输出生效
        self.detail = False
        #  本轮是否有输出被折叠。提示不在每个折叠行后面重复，而是攒到这一轮
        #  收尾统一提一次（溢出是
        #  一个全局状态，不是每行的属性）
        self.truncated = False
        #  被折叠内容的留底：(锚点行, 正文行)。inline 模式改不了已打出的
        #  scrollback，但可以往后补打——Ctrl-O 开详情时回放这份留底，
        #  "还有 N 行"才不是一句永远兑现不了的空话
        self.folded: list[tuple[str, list[str]]] = []
        #  折叠组内只读调用的参数预览（顺序执行，pending→completed 严格配对，
        #  单格暂存即可）：回放时锚点行要能自报家门
        self._pending_ro = ""
        #  超时上限（Tui.run 拿到 agent 后回填）：spinner 跑久了要报预算，
        #  "还剩多少"才是用户判断该不该继续等的依据
        self.bash_timeout: int | None = None
        self.request_timeout: int | None = None
        self._status: Any | None = None
        self._ro_counts: dict[str, int] = {}
        #  当前正文块是否已打开 OSC 133 锚点（TextEnd 负责收束配对），
        #  语义与 PlainSink 对齐，见 render.OSC133_TEXT_START 的注释
        self._osc133_open = False
        self._handlers = {
            RequestStarted: self._request_started,
            RequestEnded: self._request_ended,
            TextDelta: self._text_delta,
            TextEnd: self._text_end,
            ToolPending: self._tool_pending,
            ToolPurpose: self._tool_purpose,
            ToolRunning: self._tool_running,
            ToolCompleted: self._tool_completed,
            ToolDenied: self._tool_denied,
            PlanUpdated: self._plan,
            SteerAccepted: self._steer,
            Notice: self._notice,
        }

    def emit(self, event: UIEvent) -> None:
        handler = self._handlers.get(type(event))
        if handler is not None:
            handler(event)

    def quiet_child(self, indent: str = "    ") -> "RichSink":
        """给子 agent 的静默 sink：共用 Console、带缩进、不刷正文、不开 spinner。"""
        return RichSink(self.console, indent=indent, verbose=False)

    def interrupt(self) -> None:
        """中断/异常路径的清扫：spinner 若还在转就停掉，折叠组落盘，避免悬空。"""
        self._stop_status()
        self._flush_ro_group()

    def begin_turn(self) -> None:
        """一轮开始：清掉上一轮的折叠标记与留底（收尾的 Ctrl-O 提示据此判断）。"""
        self.truncated = False
        self.folded = []

    def _budget(self, reserve: int) -> int:
        """本行留给正文的列数。以 Console 的宽度为准（它知道自己写到哪去了，
        管道/测试里也有确定值），不去问 shutil。"""
        return ui.budget(len(self.indent) + reserve, self.console.width)

    def _stop_status(self) -> None:
        if self._status is not None:
            with contextlib.suppress(Exception):
                self._status.stop()
            self._status = None

    def _flush_ro_group(self) -> None:
        """只读折叠组落盘：遇到任何非只读事件（或一轮结束）时汇成一行。

        折叠组里只收 ok 的只读调用；出错的不进组（错误必须展开可见）。
        """
        if not self._ro_counts:
            return
        parts = [f"{_RO_TOOLS[name]} ×{count}" for name, count in self._ro_counts.items()]
        self._ro_counts = {}
        if not self.detail:
            self.truncated = True
        self.console.print(
            Text(f"{self.indent}  ⎿ {' · '.join(parts)}", style="text.secondary")
        )

    def _request_started(self, event: RequestStarted) -> None:
        """等模型的活区指示。补上"请求已发出、还没有任何输出"这段空白——
        以前这里什么都不画，用户无从判断它在干活还是卡死了。"""
        self._flush_ro_group()
        if not self.verbose or not self.console.is_terminal:
            return
        self._stop_status()
        self._status = self.console.status(
            _RunningLine(event.model, self.request_timeout, verb="思考中"), spinner="dots"
        )
        with contextlib.suppress(Exception):
            self._status.start()

    def _request_ended(self, event: RequestEnded) -> None:
        #  兜底：正常路径上活区早在首个正文/工具行时就收掉了，这里管的是
        #  "一个字都没吐出来就结束"（空响应、异常、Ctrl-C）
        self._stop_status()

    def _text_delta(self, event: TextDelta) -> None:
        #  正文是裸 print，活区必须先收掉，否则 rich 的刷新线程会和它抢同一片
        #  屏幕（RichSink 文档注释里那条"裸 print 会把 spinner 搅花"）
        self._stop_status()
        self._flush_ro_group()
        if not self.verbose:
            return
        if not self._osc133_open and self.console.is_terminal:
            #  正文块起点的零宽锚点：终端把每段回复当可导航的"提示块"
            print(end=render.OSC133_TEXT_START)
            self._osc133_open = True
        #  流式分片直出，绕过 rich（它按行工作，逐分片 print 会插换行）
        print(event.text, end="", flush=True)

    def _text_end(self, event: TextEnd) -> None:
        self._flush_ro_group()
        if self.verbose:
            if self._osc133_open:
                print(end=render.OSC133_TEXT_END)
                self._osc133_open = False
            print()

    def _tool_pending(self, event: ToolPending) -> None:
        #  模型决定调工具了，"思考中"到此为止（接下来是工具自己的 spinner）
        self._stop_status()
        if event.name in _RO_TOOLS and not self.detail:
            #  进折叠组，等 completed 计数、汇总时一起出；spinner 照常给活信号。
            #  参数预览先记下：回放留底时锚点行要说清读的是哪个文件/搜的是什么
            self._pending_ro = args_preview(
                event.name, event.args, len(self.indent) + len(event.name) + 3, self.console.width
            )
            return
        self._flush_ro_group()
        #  "● 工具名 " 之后才是参数预览，预算要把这一截先扣掉
        preview = args_preview(
            event.name, event.args, len(self.indent) + len(event.name) + 3, self.console.width
        )
        self.console.print(
            Text.assemble(
                (f"{self.indent}● {event.name}", "text.accent"),
                (f" {preview}", "text.secondary"),
            )
        )

    def _tool_purpose(self, event: ToolPurpose) -> None:
        self._flush_ro_group()
        self.console.print(Text(f"{self.indent}  目的：{event.purpose}", style="text.secondary"))

    def _tool_running(self, event: ToolRunning) -> None:
        #  只有顶层 sink 开 spinner：同一 Console 同时只允许一个 Live；
        #  非终端（管道/测试）没有"活区"可言，静默跳过
        if not self.verbose or not self.console.is_terminal:
            return
        self._stop_status()
        line = _RunningLine(event.name, self._timeout_for(event))
        self._status = self.console.status(line, spinner="dots")
        with contextlib.suppress(Exception):
            self._status.start()

    def _timeout_for(self, event: ToolRunning) -> int | None:
        """这次调用的超时上限（只有 bash 有）：显式传的优先，否则用配置默认值。
        跑久了的时候，"还剩多少预算"才是用户判断要不要继续等的依据。"""
        if event.name != "bash":
            return None
        explicit = event.args.get("timeout")
        return int(explicit) if explicit else self.bash_timeout

    def _tool_completed(self, event: ToolCompleted) -> None:
        self._stop_status()
        if event.name in _RO_TOOLS and not self.detail and event.ok:
            self._ro_counts[event.name] = self._ro_counts.get(event.name, 0) + 1
            anchor = f"● {event.name} {self._pending_ro}".rstrip()
            self.folded.append((anchor, event.output.split("\n")))
            return
        self._flush_ro_group()
        lines = event.output.split("\n")
        duration = f" · {event.seconds:.1f}s" if event.seconds >= 0.05 else ""
        if event.ok:
            head = Text()
            head.append(f"{self.indent}  ")
            head.append("⎿ ", style="status.success")
            #  预算扣掉 "  ⎿ " 与耗时后缀，摘要才真的是一行
            summary = ui.preview(lines[0], self._budget(4 + len(duration)))
            head.append(f"{summary}{duration}", style="text.secondary")
            #  还有内容被折叠时说清藏了多少（…+N 行）；
            #  「怎么看」不在这里重复，攒到一轮收尾统一提一次
            if len(lines) > 1 and not self.detail:
                head.append(f" · 还有 {len(lines) - 1} 行", style="text.secondary")
                self.truncated = True
                self.folded.append((f"⎿ {summary}{duration}", lines[1:]))
            self.console.print(head)
            if self.detail and self.verbose:
                self._expand(lines[1:], None)
        else:
            #  出错自动展开：错误详情是用户必须看的，不该藏在折叠行后面
            first = ui.preview(lines[0], self._budget(4 + len(duration)))
            self.console.print(
                Text(f"{self.indent}  ✗ {first}{duration}", style="status.error")
            )
            if not self.detail and len(lines) - 1 > self._ERROR_LINES:
                self.folded.append((f"✗ {first}{duration}", lines[1:]))
            self._expand(lines[1:], None if self.detail else self._ERROR_LINES)

    def replay_folded(self) -> None:
        """回放本轮被折叠的留底（Ctrl-O 开详情时调用）。

        一次性动作：打完即清空，再开关不会重复刷屏。detail 已置 True，
        之后的输出本来就是展开的，留底只服务"已经打出去的那些"。
        """
        items, self.folded = self.folded, []
        for anchor, rows in items:
            self.console.print(Text(f"{self.indent}  {anchor}", style="text.secondary"))
            budget = self._budget(4)
            for row in rows[: self._REPLAY_LINES]:
                self.console.print(
                    Text(f"{self.indent}    {ui.preview(row, budget)}", style="text.secondary")
                )
            if len(rows) > self._REPLAY_LINES:
                hidden = len(rows) - self._REPLAY_LINES
                self.console.print(
                    Text(f"{self.indent}    …还有 {hidden} 行", style="text.secondary")
                )

    def _expand(self, rows: list[str], cap: int | None) -> None:
        shown = rows if cap is None else rows[:cap]
        budget = self._budget(4)
        for row in shown:
            self.console.print(
                Text(f"{self.indent}    {ui.preview(row, budget)}", style="text.secondary")
            )
        if len(rows) > len(shown):
            self.truncated = True
            self.console.print(
                Text(f"{self.indent}    …还有 {len(rows) - len(shown)} 行", style="text.secondary")
            )

    def _tool_denied(self, event: ToolDenied) -> None:
        self._stop_status()
        self._flush_ro_group()
        word = "命中 deny 规则被拦截" if event.by == "rule" else "被用户拒绝"
        self.console.print(Text(f"{self.indent}  ⨯ {event.name} {word}，未执行", style="status.warning"))

    def _plan(self, event: PlanUpdated) -> None:
        self._flush_ro_group()
        if not self.verbose:
            return
        if event.explanation:
            self.console.print(
                Text(f"  ✎ {ui.preview(event.explanation, self._budget(4))}", style="text.secondary")
            )
        for item in event.plan:
            mark, style = _PLAN_STYLES[item["status"]]
            self.console.print(Text(f"  {mark} {item['step']}", style=style))

    def _steer(self, event: SteerAccepted) -> None:
        #  插话被消费的确认（用户敲字时只有 tty 回显）：不打这行就无从分辨
        #  "生效了"还是"丢了"。从 agent 线程（主线程）发出，与 Live 无竞争。
        self._flush_ro_group()
        self.console.print(
            Text(f"{self.indent}  ↳ 插话已加入本轮：{event.text}", style="text.secondary")
        )

    def _notice(self, event: Notice) -> None:
        self._flush_ro_group()
        self.console.print(Text(event.text, style=_NOTICE_STYLES[event.level]))


class SlashCompleter(Completer):
    """输入行补全：/ 补命令（带一行说明），@ 模糊补工作区文件路径，
    /model 的第二个词补降级链里的模型名。"""

    #  @ 补全时最多展示的候选数：再多就该继续打字缩小范围了
    _FILE_CANDIDATES = 20

    def __init__(self, tui: "Tui") -> None:
        self.tui = tui

    def get_completions(
        self, document: Document, complete_event: Any
    ) -> Iterable[Completion]:
        text = document.text_before_cursor
        #  @ 文件引用：光标前最后一个以 @ 开头的词（可出现在消息任意位置）
        if match := re.search(r"(?:^|\s)(@\S*)$", text):
            yield from self._file_completions(match.group(1))
            return
        if not text.startswith("/"):
            return
        parts = text.split(" ")
        if len(parts) == 1:
            yield from self._command_completions(parts[0])
        elif parts[0] == "/model" and len(parts) == 2 and self.tui.agent is not None:
            word = parts[1]
            for model in self.tui.agent.switchable_models():
                if model.startswith(word):
                    yield Completion(model, start_position=-len(word))

    def _command_completions(self, word: str) -> Iterable[Completion]:
        from .cli import SLASH_COMMANDS

        #  前缀命中排前面，子串命中（/ctx 打错也能找到 /context）殿后
        substring_hits: list[tuple[str, str]] = []
        for command, description in SLASH_COMMANDS.items():
            if command.startswith(word):
                yield Completion(command, start_position=-len(word), display_meta=description)
            elif len(word) > 1 and word[1:] in command:
                substring_hits.append((command, description))
        for command, description in substring_hits:
            yield Completion(command, start_position=-len(word), display_meta=description)

    def _file_completions(self, token: str) -> Iterable[Completion]:
        word = token[1:].lower()
        starts: list[str] = []
        contains: list[str] = []
        for path in self.tui.workspace_files():
            base = path.rsplit("/", 1)[-1].lower()
            if base.startswith(word) or path.lower().startswith(word):
                starts.append(path)
            elif word in path.lower():
                contains.append(path)
        for path in (starts + contains)[: self._FILE_CANDIDATES]:
            #  保留 @ 前缀进文本：模型看到 @路径 知道这是文件引用
            yield Completion(f"@{path}", start_position=-len(token), display=path)


class SteerPoller:
    """模型作答期间的后台 stdin 行读取：整行（按过回车）→ `agent.steer()`。

    小羽全同步，`agent.send()` 期间主线程被占住，没有任何输入循环在跑——
    这个小线程补上"运行中还能说话"的通道（配 pause/resume 纪律）。要点：

    - 终端处于规范模式：select 只在**整行完成**（用户按了回车）时可读，
      半截输入留在内核行缓冲，既不会被这里偷走，也照常出现在下一轮 prompt；
    - **审批确认框期间必须 pause()**——确认菜单是 prompt_toolkit Application
      在主线程接管终端，这里不停手就会吃掉用户的选择按键；
    - 线程自己不打印任何东西（rich Live 归主线程独占）：确认反馈由
      steer.accepted 事件在消费点（agent 线程=主线程）打出，敲字时的即时
      回显本来就有 tty echo；
    - 斜杠命令不特判：运行中敲 `/...` 也会作为插话交给模型（提示语义上
      它就是"对模型说的话"；命令请等本轮结束）；
    - Windows 没有 select-on-stdin：整个 poller 禁用，保留旧的"收尾预填"路径。
    """

    _POLL = 0.1

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self._stop = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def supported() -> bool:
        if os.name != "posix" or not sys.stdin.isatty():
            return False
        try:
            import select  # noqa: F401

            return True
        except ImportError:  # pragma: no cover - posix 必有 select
            return False

    def start(self) -> None:
        if not self.supported() or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="steer-poller")
        self._thread.start()

    def stop(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._thread = None

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def _loop(self) -> None:
        import select

        fd = sys.stdin.fileno()
        while not self._stop.is_set():
            if self._paused.is_set():
                time.sleep(self._POLL)
                continue
            try:
                readable, _, _ = select.select([fd], [], [], self._POLL)
                if not readable or self._paused.is_set() or self._stop.is_set():
                    #  select 返回后再查一次 pause/stop：确认框刚接管终端时
                    #  宁可把这行留给它，也不能抢读
                    continue
                data = os.read(fd, 4096)
            except Exception:  # noqa: BLE001 - 读不动就收工，绝不能拖垮主循环
                return
            if not data:
                return
            text = data.decode("utf-8", errors="replace")
            for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
                if line.strip():
                    self.agent.steer(line)


class Tui:
    """交互前端：持有 sink、确认函数与 REPL 循环。

    构造顺序上先于 Agent 创建（Agent 需要 approver 与 sink），
    agent 引用在 run() 里回填；PromptSession 惰性创建（构造它需要真实终端，
    提前建会让测试环境炸在 import 之外的地方）。
    """

    #  两次 Ctrl-C 间隔在此秒数内才退出（单次 Ctrl-C 防误触）
    _EXIT_WINDOW = 2.0
    #  工作区文件清单缓存秒数（@ 补全用；逐键重扫是浪费）
    _FILES_TTL = 10.0
    _FILES_CAP = 5000
    #  粘贴折叠成 chip 的阈值：超 800 字符或超过约 3 行——
    #  大段粘贴撑爆输入区还把每次重绘拖慢，折叠后正文旁路存储、提交时展开
    _PASTE_CHAR_CAP = 800
    _PASTE_LINE_CAP = 2

    def __init__(self, permissions: Permissions, console: Console | None = None) -> None:
        self.permissions = permissions
        self.sink = RichSink(console)
        self.console = self.sink.console
        self.agent: Agent | None = None
        self._session: PromptSession | None = None
        self._confirm_session: PromptSession | None = None
        self._last_input = ""
        self._files_cache: list[str] | None = None
        self._files_cache_at = 0.0
        self._pastes: dict[int, str] = {}
        self._paste_id = 0
        #  本次提交行首来自粘贴 chip：展开后的 ! / # / 前缀不是用户敲的，路由
        #  按字面发给模型（由 _submit 置位、主循环取走即清）
        self._literal_submit = False
        #  图片 chip：编号 → 媒体引用（xiaoyu-media://…）。会话内累加不清理，
        #  用户可能把同一张图在后面几轮里反复引用（Esc-Esc 取回上一条即是）
        self._images: dict[int, str] = {}
        self._image_id = 0
        #  运行期插话线程（run() 里按轮起停）；confirm() 靠它 pause/resume
        self._poller: SteerPoller | None = None
        #  "切进 auto 但沙箱不可用"只提醒一次，别每次按键都刷一行
        self._warned_no_sandbox = False

    # ---------- 输入 ----------

    def _fold_paste(self, data: str) -> str:
        """大段粘贴折叠成 chip 占位符，正文旁路存储（输入区不被大段粘贴
        撑爆，每次击键的重绘也不被拖慢）。
        返回要插入输入框的文本；小粘贴原样返回。"""
        data = data.replace("\r\n", "\n").replace("\r", "\n")
        lines = data.count("\n") + 1
        if len(data) <= self._PASTE_CHAR_CAP and lines <= self._PASTE_LINE_CAP + 1:
            return data
        self._paste_id += 1
        self._pastes[self._paste_id] = data
        return f"[粘贴 #{self._paste_id} · {lines} 行]"

    def _starts_with_paste(self, text: str) -> bool:
        """行首（忽略空白）是否是一个真实存在的粘贴 chip。"""
        match = _PASTE_REF.match(text.lstrip())
        return bool(match) and int(match.group(1)) in self._pastes

    def _expand_pastes(self, text: str) -> str:
        """提交前把 chip 占位符展开回原文（被手改坏的占位符按字面保留）。"""
        return _PASTE_REF.sub(
            lambda match: self._pastes.get(int(match.group(1)), match.group(0)), text
        )

    # ---------- 图片 chip ----------

    def _fold_image(self, ref: str) -> str:
        """收下一张图，返回要插进输入行的 chip 占位符。

        标签带尺寸（`1920×1080 · 42 KB`）而不只是体积：贴完一眼能确认贴对了
        没有——尺寸比字节数更像"那张图"。解析不出尺寸时自动退回只报体积。
        """
        self._image_id += 1
        self._images[self._image_id] = ref
        return f"[图片 #{self._image_id} · {media.label(ref)}]"

    def _warn(self, text: str) -> None:
        """按键处理里打一行提示。

        `run_in_terminal` 是在输入行挂起时打字的正确姿势（不打花正在编辑的行），
        但它要求有正在跑的 prompt_toolkit app——测试与非交互路径下没有，
        那时直接 print 即可，不能因为提示打不出来就把整个粘贴动作弄崩。
        先问 `get_app_or_none()` 而不是 try/except：run_in_terminal 在没有事件
        循环时是**先造好协程再抛**，接住异常也已经留下一个 never awaited 警告。
        """
        def _print() -> None:
            self.console.print(Text(f"  {text}", style="status.warning"))

        if get_app_or_none() is None:
            _print()
        else:
            run_in_terminal(_print)

    def _take_image(self, data: bytes, source: str = "") -> str | None:
        """字节 → chip 文本；不合格时打一行原因并返回 None。

        原因一定要打出来：粘贴失败最烦人的形态是"按了没反应"，用户无从判断
        是键没生效、剪贴板没图、还是图太大。
        """
        ref, problem = media.accept(data, source)
        if problem:
            self._warn(problem)
            return None
        return self._fold_image(ref)

    def _paths_to_text(self, paths: Sequence[Path]) -> str:
        """一组文件路径 → 插进输入行的文本：图片折成 chip，其余原样给路径。"""
        pieces: list[str] = []
        for path in paths:
            if not media.is_image_path(path):
                pieces.append(str(path))
                continue
            ref, problem = media.accept_file(path)
            if ref:
                pieces.append(self._fold_image(ref))
            else:
                self._warn(problem)
        return " ".join(pieces)

    def _content_of(self, text: str) -> Any:
        """提交的一行 → 发给模型的 content。没有图片时返回原字符串。

        chip 占位符**原样留在文本里**（"[图片 #1 · 42 KB]"），图片部件追加在
        后面：模型既看得到图，也看得到用户把它插在句子的哪个位置——
        "这两张图哪个更好"如果只剩两张裸图，指代关系就丢了。

        当前模型看不了图时不静默丢弃，也不改文本：告诉用户换模型，图片留在
        chip 表里，切完 /model 重发即可（Esc-Esc 就能取回上一条）。配了代读模型
        （XIAOYU_VISION_FALLBACK）则先把图换成文字随本条一起发——**但提示照打**：
        代读是有损的，"能凑合看懂"和"真看见了"必须让用户分得清，而原图始终只有
        `/model` 换视觉模型这一条路。
        """
        refs = [
            self._images[int(match.group(1))]
            for match in _IMAGE_REF.finditer(text)
            if int(match.group(1)) in self._images
        ]
        if not refs:
            return text
        agent = self.agent
        model = agent.config.model if agent is not None else ""
        parts = [media.image_part(ref) for ref in refs]
        if agent is not None and not agent.registry.sees_images(model):
            #  代读要发一次网络请求，但这里已经在提交之后（全同步 REPL，
            #  紧接着就是 agent.send 阻塞整轮），不额外卡住任何输入循环
            block, notice = agent.caption_images(parts, guide=text)
            if block:
                self.console.print(Text(notice, style="status.warning"))
                return f"{text}\n\n{block}"
            self.console.print(
                Text(
                    f"  当前模型 {model} 不接受图片输入，这条只发文字。"
                    "/model 换一个支持视觉的再重发（Esc Esc 取回上一条）",
                    style="status.warning",
                )
            )
            return text
        return [media.text_part(text), *parts]

    def _tab_complete(self, buffer: Any) -> None:
        """Tab 先补到所有候选的最长公共前缀、再按才轮换（readline 直觉）。
        菜单已打开时维持轮换语义。"""
        if buffer.complete_state is not None:
            buffer.complete_next()
            return
        if buffer.completer is None:
            return
        document = buffer.document
        completions = list(
            buffer.completer.get_completions(document, CompleteEvent(completion_requested=True))
        )
        if not completions:
            return
        if len(completions) == 1:
            buffer.apply_completion(completions[0])
            return
        before = document.text_before_cursor
        candidates = [before[: item.start_position] + item.text for item in completions]
        prefix = os.path.commonprefix(candidates)
        if len(prefix) > len(before):
            #  只补到公共前缀就停手（不在此开菜单：start_completion 需要正在运行的
            #  应用事件循环，且 readline 语义本来就是"再按一次才出候选"）
            buffer.insert_text(prefix[len(before) :])
        else:
            buffer.complete_next()

    def _input_session(self) -> PromptSession:
        if self._session is None:
            history_dir = user_config_dir()
            history_dir.mkdir(parents=True, exist_ok=True)
            self._session = PromptSession(
                history=DedupedHistory(str(history_dir / "input_history")),
                completer=SlashCompleter(self),
                key_bindings=self._key_bindings(),
                multiline=True,
                #  续行不打标记、只按提示符实际宽度补空格对齐。
                #  宽度必须动态取：确认档之外提示符带模式标记，比 "› " 宽。
                prompt_continuation=lambda width, line_number, is_soft_wrap: " " * width,
                #  prompt_toolkit 自带的 Ctrl-X Ctrl-E：长指令拉到 $EDITOR 里改
                enable_open_in_editor=True,
            )
        return self._session

    def _key_bindings(self) -> KeyBindings:
        """按 keys.BINDINGS 那张表注册输入行的按键。

        每个键"按了会怎样"写在表里而不是这儿：首屏速览和 /keys 帮助都从同一张
        表渲染，加了键忘改文案（或反过来）不再可能。Enter 一律提交是 bash 直觉
        ——补全菜单只是展示，Tab 轮换时文本已进了行内，行里是什么就提交什么。
        """

        def _submit(event: Any) -> None:
            buffer = event.current_buffer
            state = buffer.complete_state
            if state and state.current_completion:
                #  菜单里有选中的候选：先把它落定成真实文本再提交。
                #  不能直接把 complete_state 置 None——那等于取消补全，
                #  Tab 轮换插入的文本会被回退成原输入。
                buffer.apply_completion(state.current_completion)
            #  chip 展开回原文再提交；输入历史里存的也是展开后的全文。
            #  展开前先看行首：顶在第 0 位的是 chip 就标记"字面提交"，
            #  否则贴进来的 `!rm -rf` 日志会在展开后被当成 shell 命令跑掉。
            self._literal_submit = self._starts_with_paste(buffer.text)
            expanded = self._expand_pastes(buffer.text)
            if expanded != buffer.text:
                buffer.text = expanded
                buffer.cursor_position = len(expanded)
            buffer.validate_and_handle()

        def _tab(event: Any) -> None:
            self._tab_complete(event.current_buffer)

        def _paste(event: Any) -> None:
            #  拖文件进终端 = 终端把路径当文本粘进来，拖多个就是空格分隔的多条。
            #  整段每一项都是真实文件时按文件收：图片折成 chip，其余留路径给模型
            #  自己 read_file（判据收得很紧，见 media.split_paths）。收不下时
            #  _paths_to_text 已经报过原因，这里照常按普通文本粘，至少留下路径。
            if paths := media.split_paths(event.data):
                if text := self._paths_to_text(paths):
                    event.current_buffer.insert_text(text)
                    return
            event.current_buffer.insert_text(self._fold_paste(event.data))

        def _paste_image(event: Any) -> None:
            clip = media.clipboard()
            if clip.problem:
                self._warn(clip.problem)
                return
            #  位图（截图）与文件清单可能同时有；文件里的非图片原样插成路径，
            #  模型拿到路径能自己 read_file，比我们替它判"这个格式没用"强
            parts = [
                chip
                for data in clip.images
                if (chip := self._take_image(data, "剪贴板里的图片"))
            ]
            if text := self._paths_to_text(clip.files):
                parts.append(text)
            if parts:
                event.current_buffer.insert_text(" ".join(parts))

        def _backspace(event: Any) -> None:
            buffer = event.current_buffer
            #  光标紧邻 chip 占位符时整颗删除（chip 是原子，不进内部逐字删）
            before = buffer.document.text_before_cursor
            match = _PASTE_REF_TAIL.search(before) or _IMAGE_REF_TAIL.search(before)
            if match:
                buffer.delete_before_cursor(len(match.group(0)))
            else:
                buffer.delete_before_cursor(1)

        def _newline(event: Any) -> None:
            event.current_buffer.insert_text("\n")

        def _toggle_detail(event: Any) -> None:
            self.sink.detail = not self.sink.detail

            def _report() -> None:
                if self.sink.detail:
                    #  开：先把上一轮折叠掉的补打出来——用户按 Ctrl-O 十有八九
                    #  就是想看刚才那条"还有 N 行"，不能只许诺以后
                    self.console.print(
                        Text("  工具输出详情：开（展开完整输出）", style="text.secondary")
                    )
                    self.sink.replay_folded()
                else:
                    self.console.print(
                        Text(
                            "  工具输出详情：关（每个工具折叠一行），对之后的输出生效",
                            style="text.secondary",
                        )
                    )

            run_in_terminal(_report)

        def _recall(event: Any) -> None:
            if self._last_input:
                buffer = event.current_buffer
                buffer.text = self._last_input
                buffer.cursor_position = len(buffer.text)

        def _cycle_mode(event: Any) -> None:
            if self.agent is None:
                return
            self.agent.set_mode(modes.next_mode(self.agent.mode))
            #  提示符是 callable，invalidate 就地重画成 "⏵⏵ auto ›"，不脏 scrollback
            event.app.invalidate()
            #  唯一例外：切进 auto 却没有沙箱兜底。这时 auto 只剩"改文件免确认"，
            #  bash 照旧逐条问——静默降级会让人以为按键没生效，必须打一次
            if (
                self.agent.mode == modes.AUTO
                and not self.agent.sandbox_ready()
                and not self._warned_no_sandbox
            ):
                self._warned_no_sandbox = True
                text = modes.describe(modes.AUTO, sandbox_ready=False)
                run_in_terminal(
                    lambda: self.console.print(Text(f"  {text}", style="status.warning"))
                )

        return _register(
            {
                "input.submit": _submit,
                "input.newline": _newline,
                "input.complete": _tab,
                "input.backspace": _backspace,
                "input.paste": _paste,
                "input.pasteImage": _paste_image,
                "input.recall": _recall,
                "app.toggleDetail": _toggle_detail,
                "app.cycleMode": _cycle_mode,
            },
            keys.INPUT,
        )

    def _prompt_fragments(self) -> list[tuple[str, str]]:
        """输入行提示符。确认档之外在 `›` 前面挂一个模式标记。

        做成 callable 而不是固定字符串：Shift+Tab 切档后 `app.invalidate()`
        就能就地重画。**不做常驻状态栏**——bottom_toolbar 只在输入行挂起时存在、
        模型一跑就消失，那条路做过又拆了（见模块开头）。提示符本来就只在
        "轮到你说话"时出现，模式标记长在它上面不会时隐时现。
        """
        mode = modes.get(self.agent.mode if self.agent is not None else modes.DEFAULT)
        fragments: list[tuple[str, str]] = []
        if mode.marker:
            fragments.append((theme.ptk(mode.style), f"{mode.marker} {mode.label} "))
        fragments.append((theme.ptk("prompt"), "› "))
        return fragments

    def _drain_typeahead(self) -> str:
        """取走用户在模型作答期间敲进终端缓冲的整行输入。

        小羽是全同步的：`agent.send()` 期间没有 prompt 挂着，这段时间的按键
        会一直躺在 tty 缓冲里，等下一轮 prompt 起来时被 prompt_toolkit 一次
        读掉——**连同其中的回车**，于是那句话在用户还没看到上一轮结果时就
        自动发出去了。这里抢先取走，改成预填进输入行，让人看一眼再决定。

        **只预填不自动提交**：异步架构里排队提交是自然语义；
        小羽同步，把用户"盲打"的内容直接发出去
        风险更大——他可能正是看到中途输出才要改口。

        终端处于规范模式，`os.read` 只会给出已经按过回车的完整行；没按回车的
        半截输入留在内核缓冲里，下一轮 prompt 会照常显示，不会误提交。
        Windows 没有 select on tty，直接返回空串。
        """
        if not sys.stdin.isatty():
            return ""
        try:
            import select

            fd = sys.stdin.fileno()
            chunks: list[bytes] = []
            while select.select([fd], [], [], 0)[0]:
                data = os.read(fd, 4096)
                if not data:
                    break
                chunks.append(data)
        except Exception:  # noqa: BLE001 - 抽不动就算了，绝不能影响主循环
            return ""
        text = b"".join(chunks).decode("utf-8", errors="replace")
        #  多行就用换行连起来（用户确实敲了多行，保留原样让他自己看）
        lines = [line for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
        return "\n".join(lines).strip()

    def _print_expand_hint(self) -> None:
        """折叠过输出就在一轮收尾提一次 Ctrl-O（溢出是整轮的一个状态，
        不是每个折叠行各自的属性）。

        原先挂在 bottom_toolbar 上，但那条栏只在输入行挂起时存在、模型一跑
        就消失，时隐时现像 bug；收尾打进 scrollback 则紧跟着被折叠的输出，
        且只在"当下还能展开"时出现。
        """
        if self.sink.truncated and not self.sink.detail:
            self.console.print(Text("  Ctrl-O 展开被折叠的输出", style="text.secondary"))

    def workspace_files(self) -> list[str]:
        """@ 补全的候选文件（工作区相对路径，带 TTL 缓存）。

        优先 git ls-files（快、天然吃 .gitignore、含未跟踪新文件）；
        不是 git 仓库再退回带剪枝的目录遍历。
        """
        now = time.monotonic()
        if self._files_cache is not None and now - self._files_cache_at < self._FILES_TTL:
            return self._files_cache
        root = self.permissions.workspace
        files: list[str] = []
        with contextlib.suppress(Exception):
            proc = subprocess.run(
                ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                cwd=root, capture_output=True, text=True,
                #  git 输出恒 UTF-8；Windows 的 locale 编码解非 ASCII 文件名会抛，
                #  抛了会被外层 suppress 吞掉、悄悄退化成 os.walk（更慢且少剪枝）
                encoding="utf-8", errors="replace", timeout=5,
            )
            if proc.returncode == 0:
                files = [line for line in proc.stdout.splitlines() if line]
        if not files:
            skip = {"node_modules", "__pycache__", "dist", "build", "target", "venv"}
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in skip and not d.startswith(".")]
                for filename in filenames:
                    if filename.startswith("."):
                        continue
                    relative = os.path.relpath(os.path.join(dirpath, filename), root)
                    files.append(relative.replace(os.sep, "/"))
                    if len(files) >= self._FILES_CAP:
                        break
                if len(files) >= self._FILES_CAP:
                    break
        self._files_cache, self._files_cache_at = files, now
        return files

    # ---------- 确认框 ----------

    def confirm(self, name: str, args: dict[str, Any]) -> bool | str | tuple[bool, str]:
        """交互确认（Agent 的 approver）：三选项菜单。

        允许一次 / 本会话不再问 / 总是允许（规则写入前可编辑）/
        拒绝并告诉小羽下一步怎么做。Tab = 批准并附言：附言随 tool result
        回灌模型（拒绝理由走同一条通道，拒绝只是附言的特例）。
        返回值：(True, 附言) 也是批准——附言由 Agent 拼进 tool result。
        """
        #  确认交互期间必须停掉插话线程：确认菜单在主线程接管终端，
        #  poller 不停手会把用户的选择按键当成插话吃掉
        if self._poller is not None:
            self._poller.pause()
        try:
            return self._confirm_inner(name, args)
        finally:
            if self._poller is not None:
                self._poller.resume()

    def ask(self, questions: list[dict[str, Any]]) -> dict[str, str]:
        """交互提问（Agent 的 asker，ask_user 工具在 handler 里调它）。

        与 confirm 同一纪律：插话线程 pause（否则面板按键被偷走）。
        另外要先收掉工具 spinner——ask_user 的 handler 跑在 tool.running
        之后，rich Status 的刷新线程还在画活区，不停掉会和面板抢屏幕。
        """
        if self._poller is not None:
            self._poller.pause()
        self.sink._stop_status()  # noqa: SLF001 - 同模块前端搭档
        try:
            return ask_questions(questions, self.console)
        finally:
            if self._poller is not None:
                self._poller.resume()

    def _confirm_inner(self, name: str, args: dict[str, Any]) -> bool | str | tuple[bool, str]:
        #  预览被截断时拿到"补打全文"闭包，挂到菜单的 Ctrl-O 上——
        #  "还有 N 行"在确认框里同样得是可兑现的承诺，不能逼人盲批
        expand: Callable[[], None] | None = None
        if name == "write_file":
            expand = self._preview_write(str(args.get("path", "")), str(args.get("content", "")))
        elif name == "str_replace":
            expand = self._preview_replace(
                str(args.get("path", "")),
                str(args.get("old_str", "")),
                str(args.get("new_str", "")),
            )
        elif name == "bash":
            if reason := command_check.command_risk(str(args.get("command", ""))):
                self.console.print(Text(f"  ⚠ 注意：{reason}", style="status.warning"))

        options: list[tuple[str, str, str]] = [("once", "允许一次", "y")]
        #  bash 的会话授权按命令头记（git status / rg…），推不出头的调用没有这个选项
        if (scope := self.permissions.session_grant_label(name, args)) is not None:
            options.append(("session", f"本会话内 {scope} 都允许，不再询问", "a"))
        rule = suggest_allow_rule(name, args, self.permissions.workspace)
        if rule is not None:
            options.append(("always", f"总是允许（规则写入前可编辑：{rule}）", "!"))
        options.append(("deny", "拒绝，并告诉小羽下一步怎么做", "n"))

        try:
            choice = self._inline_select(self._confirm_title(name, args), options, expand)
        except Exception:  # noqa: BLE001 - 非常规终端起不了菜单：退回文本问答，确认永远可用
            return self._confirm_text(name)

        amend = False
        if isinstance(choice, tuple):  # Tab 按下：批准并附言
            amend, choice = True, choice[1]

        if choice == "once":
            return self._accept("✔ 已允许（仅此一次）", amend)
        if choice == "session":
            scope = self.permissions.grant_session_call(name, args)
            return self._accept(f"✔ 本会话内 {scope} 不再逐次确认（/perm 可查看）", amend)
        if choice == "always":
            assert rule is not None  # 只有推导出规则才有这个选项
            edited = self._ask_rule(str(rule))
            if edited is None:  # Esc / Ctrl-C：规则不写了，本次也不执行
                self._echo_verdict("⨯ 已拒绝")
                return False
            edited = edited.strip()
            if not edited:
                return self._accept("✔ 未写入规则，仅本次允许", amend)
            parsed = parse_rule(edited)
            if parsed is None:
                self.console.print(
                    Text(f"  规则格式无法解析：{edited}（应为 allow bash(git *) 这类）", style="status.error")
                )
                return False
            try:
                path = self.permissions.add_persistent(parsed)
            except ValueError as exc:
                self.console.print(Text(f"  规则被拒绝：{exc}", style="status.error"))
                return False
            return self._accept(f"✔ 已写入 {path}：{parsed}", amend)
        if choice == "deny":
            reason = self._ask_reason()
            self._echo_verdict("⨯ 已拒绝" + (f"：{reason}" if reason else ""))
            return reason or False
        #  Esc / Ctrl-C：普通拒绝
        self._echo_verdict("⨯ 已拒绝")
        return False

    def _accept(self, verdict_text: str, amend: bool) -> bool | tuple[bool, str]:
        """批准路径的统一收尾：回显定格进 scrollback；amend 时追问附言。"""
        if amend:
            note = self._ask_note()
            if note:
                self._echo_verdict(f"{verdict_text}，附言：{note}")
                return (True, note)
        self._echo_verdict(verdict_text)
        return True

    @staticmethod
    def _confirm_title(name: str, args: dict[str, Any]) -> str:
        """确认框问句带具体宾语：「要修改 foo.py 吗？」
        比泛泛的「允许执行吗？」信息密度高。"""
        if name == "bash":
            return "要执行这条命令吗？"
        if name in ("write_file", "str_replace"):
            verb = "写入" if name == "write_file" else "修改"
            base = str(args.get("path", "")).rsplit("/", 1)[-1]
            return f"要{verb} {base} 吗？"
        return f"允许执行 {name} 吗？"

    def _ask_rule(self, rule: str) -> str | None:
        """「总是允许」的规则写入前可编辑（选项预填可改的命令前缀）。
        回车接受原文；置空 = 不写规则、仅本次允许；Esc/Ctrl-C = 取消。"""
        if self._confirm_session is None:
            self._confirm_session = PromptSession()
        try:
            return self._confirm_session.prompt(
                [(theme.ptk("menu.title"), "  规则（可编辑，置空 = 仅本次允许）：")], default=rule
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return None

    def _ask_note(self) -> str:
        """Tab 附言：批准的同时给模型一句
        指示，随这次 tool result 回灌。空输入 = 不附言。"""
        if self._confirm_session is None:
            self._confirm_session = PromptSession()
        try:
            return self._confirm_session.prompt(
                [(theme.ptk("menu.title"), "  附加指示（回车跳过）：")]
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""

    def _inline_select(
        self,
        title: str,
        options: list[tuple[str, str, str]],
        expand: Callable[[], None] | None = None,
    ) -> Any:
        """确认框的行内单选：通用件 inline_select 的带附言（Tab）形态。"""
        return inline_select(title, options, amend=True, expand=expand)

    def _ask_reason(self) -> str:
        """拒绝之后追问一句"下一步怎么做"：拒绝即改指令，不让模型干猜。"""
        if self._confirm_session is None:
            self._confirm_session = PromptSession()
        try:
            return self._confirm_session.prompt(
                [(theme.ptk("menu.title"), "  下一步该怎么做？（回车 = 仅拒绝）")]
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return ""

    def _echo_verdict(self, text: str) -> None:
        self.console.print(Text(f"  {text}", style="text.secondary"))

    def _confirm_text(self, name: str) -> bool | str:
        """菜单起不来时的回退：与 cli.make_confirm 相同的文本问答语义。"""
        from .cli import GRANT_SESSION, interpret_confirm_answer

        if self._confirm_session is None:
            self._confirm_session = PromptSession()
        try:
            answer = self._confirm_session.prompt(
                [
                    (
                        theme.ptk("menu.title"),
                        f"  允许执行 {name}? [y/N/a=本会话都允许，其它输入=拒绝理由] ",
                    )
                ]
            )
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        verdict = interpret_confirm_answer(answer)
        if verdict is GRANT_SESSION:
            scope = self.permissions.grant_session_call(name, args)
            note = (
                f"  （本次会话内 {scope} 不再逐次确认；/perm 可查看）"
                if scope is not None
                else "  （这条命令推不出会话授权范围，仅本次允许）"
            )
            self.console.print(Text(note, style="text.secondary"))
            return True
        return verdict

    #  write_file 预览行数与明文版一致
    _WRITE_HEAD = 12

    def _body_width(self) -> int:
        """确认框内正文的可用列数（"  │ " 前缀已扣）。"""
        return ui.budget(4, self.console.width)

    def _diff_cap(self) -> int:
        """diff 预览的行数上限：跟终端高度取小。预览比屏幕还高的话，最该先看到的
        开头会被自己的后半截顶出屏幕，那就白预览了。"""
        return max(min(_DIFF_CAP, ui.term_size()[1] - _CONFIRM_CHROME), 6)

    def _preview_write(self, path: str, content: str) -> Callable[[], None] | None:
        """打印头部预览；被截断时返回"补打全文"的闭包（确认菜单 Ctrl-O 调），
        没截断返回 None——菜单据此决定提示行里许不许诺"看全文"。"""
        head_lines = content.split("\n")[: self._WRITE_HEAD]
        self.console.print(Text(f"  ┌ 将写入：{path}", style="text.secondary"))
        self._print_code(path, "\n".join(head_lines))
        if content.count("\n") < self._WRITE_HEAD:
            return None
        remainder = content.count("\n") - self._WRITE_HEAD + 1
        self.console.print(Text(f"  └ …还有 {remainder} 行", style="text.secondary"))

        def show_full() -> None:
            #  与 Ctrl-O 详情回放同一契约：整体重打而非只补尾巴，读者不用
            #  跨着截断线自己拼接两截
            self.console.print(Text(f"  ┌ 全文：{path}", style="text.secondary"))
            self._print_code(path, content)
            self.console.print(Text("  └", style="text.secondary"))

        return show_full

    def _print_code(self, path: str, text: str) -> None:
        try:
            lexer = Syntax.guess_lexer(path, code=text)
            block = Syntax(text, lexer, background_color="default", word_wrap=True)
            self.console.print(block, no_wrap=False)
        except Exception:  # noqa: BLE001 - 高亮失败退回明文，预览不能因样式而炸
            for row in text.split("\n"):
                self.console.print(
                    Text(f"  │ {ui.preview(row, self._body_width())}", style="text.secondary")
                )

    def _preview_replace(self, path: str, old: str, new: str) -> Callable[[], None] | None:
        """打印 diff 预览；被截断时返回"补打完整 diff"的闭包，同 _preview_write。"""
        rows = _diff_rows(old, new, self._body_width(), cap=None)
        cap = self._diff_cap()
        shown = rows
        if len(rows) > cap:
            shown = rows[:cap] + [[("text.secondary", f"…还有 {len(rows) - cap} 行")]]
        self.console.print(Text(f"  ┌ 将修改：{path}", style="text.secondary"))
        self._print_diff_rows(shown)
        self.console.print(Text("  └", style="text.secondary"))
        if len(rows) <= cap:
            return None

        def show_full() -> None:
            self.console.print(Text(f"  ┌ 完整 diff：{path}", style="text.secondary"))
            self._print_diff_rows(rows)
            self.console.print(Text("  └", style="text.secondary"))

        return show_full

    def _print_diff_rows(self, rows: list[list[tuple[str, str]]]) -> None:
        for fragments in rows:
            #  _diff_rows 的片段是 (样式, 文本)，Text.assemble 吃 (文本, 样式)
            self.console.print(
                Text.assemble(
                    ("  │ ", "text.secondary"), *[(text, style) for style, text in fragments]
                )
            )

    # ---------- 输入行前缀动作 ----------

    def _run_shell(self, command: str) -> None:
        """! 前缀外壳：调核心（cli.user_shell）+ rich 打印。

        不过权限、不进沙箱的理由见核心 docstring；空命令由 classify_input 拦住。
        """
        from .cli import user_shell

        agent = self.agent
        assert agent is not None
        self.console.print(Text(f"$ {command}", style="text.accent"))
        try:
            result = user_shell(agent, command)
        except KeyboardInterrupt:
            self.console.print(Text("[命令被 Ctrl-C 中断，输出未能捕获]", style="status.warning"))
            return
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            self.console.print(Text(result.stderr.rstrip("\n"), style="status.error"))
        self.console.print(
            Text(f"  （退出码 {result.returncode}，输出已进入上下文）", style="text.secondary")
        )

    def _note_memory(self, note: str) -> None:
        """# 前缀外壳：调核心（cli.user_memo）+ rich 打印。

        指令文件选择与"文件管以后、灌话管这轮"的语义见核心 docstring。
        """
        from .cli import user_memo

        agent = self.agent
        assert agent is not None
        try:
            target = user_memo(agent, note)
        except OSError as exc:
            self.console.print(Text(f"  写入失败：{exc}", style="status.error"))
            return
        self.console.print(
            Text(f"  已记入 {target}（本会话即刻生效，之后的会话自动加载）", style="text.secondary")
        )

    # ---------- REPL ----------

    def run(self, agent: Agent) -> int:
        """交互循环。与明文 repl 的差异：Ctrl-C 需在 2 秒内按两次才退出
        （单次防误触），Ctrl-D 仍即刻退出；多出 @ 文件补全与 Ctrl-O 等按键
        （! / # 前缀两个前端已对齐，路由同走 keys.classify_input）。"""
        from .cli import handle_slash

        self.agent = agent
        self.sink.bash_timeout = agent.config.bash_timeout
        self.sink.request_timeout = int(agent.config.request_timeout)
        if agent.config.auto_approve:
            self.console.print(
                Text("--yolo 已开启：写文件和执行命令都不会再问你", style="status.error")
            )
        if agent.mode != modes.DEFAULT:
            #  --mode auto 起手：提示符上的标记不解释"这一档会不会问你"，开场说一次
            self.console.print(
                Text(
                    modes.describe(agent.mode, sandbox_ready=agent.sandbox_ready()),
                    style="status.warning",
                )
            )
            self._warned_no_sandbox = True
        #  速览行从绑定表渲染，全部按键见 /keys
        self.console.print(Text(f"  {keys.hint_line()} · /keys 看全部", style="text.secondary"))
        #  resume 回放若折叠过工具输出，进循环前提一次 Ctrl-O——留底还在，
        #  这是它被 begin_turn 清掉之前唯一的展开窗口
        self._print_expand_hint()

        session = self._input_session()
        last_interrupt = 0.0
        #  等待期敲的整行输入：预填进下一轮输入行，不自动提交
        queued = ""
        while True:
            try:
                line = session.prompt(
                    self._prompt_fragments,
                    default=queued,
                ).strip()
                queued = ""
            except EOFError:
                print()
                return 0
            except KeyboardInterrupt:
                now = time.monotonic()
                if now - last_interrupt < self._EXIT_WINDOW:
                    print()
                    return 0
                last_interrupt = now
                self.console.print(Text("  再按一次 Ctrl-C 退出", style="text.secondary"))
                continue

            #  提交路由单点在 keys.classify_input：TUI 与明文 REPL 同一张表
            literal, self._literal_submit = self._literal_submit, False
            action = keys.classify_input(line, literal=literal)
            if action.kind == "empty":
                continue
            if action.kind == "usage":
                self.console.print(Text(f"  {action.hint}", style="text.secondary"))
                continue
            if action.kind == "slash":
                #  /resume 这类需要列表选择的命令用行内菜单（纯单选，无附言）
                if handle_slash(agent, action.args, select=lambda t, o: inline_select(t, o, amend=False)):
                    return 0
                continue
            if action.kind == "shell":
                self._run_shell(action.args)
                continue
            if action.kind == "memo":
                self._note_memory(action.args)
                continue

            line = action.args
            self._last_input = line
            self.sink.begin_turn()
            #  运行期插话（steer）：整行回车即在 step 边界进入本轮。
            #  poller 起不来（Windows/非 tty）时自动退回旧的"收尾预填"体验
            self._poller = SteerPoller(agent)
            try:
                with NoticeCapture(self.sink):
                    self._poller.start()
                    agent.send(self._content_of(line))
                #  一轮结束只读折叠组可能还攒着（收尾回复没有正文时就没人触发落盘）
                self.sink._flush_ro_group()  # noqa: SLF001 - 同模块前端搭档
            except KeyboardInterrupt:
                self.sink.interrupt()
                agent.close_open_tool_calls("用户按 Ctrl-C 中断了本轮。")
                self.console.print(Text("\n[已中断，可以继续输入]", style="status.warning"))
            except Exception as exc:  # noqa: BLE001 - REPL 不该因为一次请求失败就退出
                self.sink.interrupt()
                if agent.session_log:
                    agent.session_log.event("error", error=f"{type(exc).__name__}: {exc}")
                self.console.print(
                    Text(f"\n请求失败：{type(exc).__name__}: {exc}", style="status.error")
                )
            finally:
                self._poller.stop()
                self._poller = None
            #  still-running 状态行：inline 架构没有常驻状态栏，
            #  轮次结束打一行即走，与明文 REPL 同一份文案（cli.background_status）
            from .cli import background_status

            if note := background_status(agent):
                self.console.print(Text(note, style="text.secondary"))
            self._print_expand_hint()
            #  没赶上本轮的插话（模型收尾后才按的回车）+ 等待期敲的其余内容：
            #  都转成下一轮输入行的预填——用户的话绝不凭空消失，也绝不自动提交
            leftover = agent.drain_steers()
            typed = self._drain_typeahead()
            queued = "\n".join(part for part in [*leftover, typed] if part).strip()
            if queued:
                self.console.print(
                    Text("  （已接住你刚才输入的内容，确认后回车发送）", style="text.secondary")
                )
            print()
