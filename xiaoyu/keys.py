"""按键与输入前缀的绑定表：交互语义的唯一事实来源。

问题：绑定以前散在 `Tui._key_bindings()` 的一串装饰器里，而首屏那行提示
（"! 直跑 shell · @ 补文件名 · …"）是另一处手写的字符串。加一个键忘了改文案，
用户就永远发现不了它；改了文案忘了改实现，帮助就在撒谎。两边都没有任何东西
会报错。

做法：绑定是数据。注册（tui.py 遍历本表 `bindings.add`）、首屏提示、`/keys`
帮助全部从这一张表渲染。防漂移这里规模小，用一条测试断言"表里每个带键的
条目都有处理函数、每个处理函数都在表里"就够了。

**`keys` 为空的条目是有意的**：那些行为由 prompt_toolkit 自带（↑↓ 历史回退、
Ctrl-R 反向搜索）或由 REPL 循环实现（Ctrl-C 两连退出），本表不注册它们，只
负责把它们记进帮助——否则帮助会漏掉一半用户真正能按的键。

本模块不依赖 prompt_toolkit：没装 tui 可选依赖时，`/keys` 仍要能打印。
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass

_WINDOWS = sys.platform == "win32"

#  分类既是 /keys 的分组标题，也决定渲染顺序
INPUT = "输入行"
PREFIX = "输入行前缀"
MENU = "确认菜单"
CATEGORIES = (INPUT, PREFIX, MENU)


@dataclass(frozen=True)
class Binding:
    """一条交互绑定。

    `command` 是稳定标识（点分命名，跟事件协议同一套风格），改按键不改它。
    `keys` 是 prompt_toolkit 的按键序列列表，一条命令可以有多个组合；
    留空表示"本表不注册，仅登记到帮助"（见模块文档）。
    """

    command: str
    label: str  # 给用户看的按法，如 "Ctrl-O"
    desc: str
    category: str
    keys: tuple[tuple[str, ...], ...] = ()
    hint: str = ""  # 非空则进速览行，内容是这里的简称（速览要短，不复用 desc）
    show: bool = True  # 是否单独占一行展示（成对的方向键合并显示，见 menu.down）
    #  是否进等待期轮播（见 tips()）。跟 hint 是两个维度：速览行寸土寸金，只放
    #  最高频的几个；轮播不占地方，标准是"不主动说就没人会发现"。菜单键几乎都
    #  不标——菜单底部提示行（menu_hint）在用户正对着菜单时就教了，时机更好。
    tip: bool = False


BINDINGS: tuple[Binding, ...] = (
    #  ---------- 输入行 ----------
    Binding(
        "input.submit", "Enter", "提交（补全菜单里选中的候选先落定成文本）",
        INPUT, keys=(("enter",),),
    ),
    Binding(
        "input.newline", "Alt-Enter", "插入换行，不提交",
        INPUT, keys=(("escape", "enter"),), hint="换行", tip=True,
    ),
    Binding(
        "input.complete", "Tab", "补全：先补到最长公共前缀，再按才轮换候选",
        INPUT, keys=(("tab",),),
    ),
    Binding(
        "input.backspace", "Backspace", "退格；光标紧邻粘贴 chip 时整颗删除",
        INPUT, keys=(("backspace",),),
    ),
    Binding(
        "input.paste", "粘贴", "大段粘贴折叠成 chip，提交时自动展开回原文；"
        "拖图片文件进来（可多个）收成图片 chip",
        INPUT, keys=(("<bracketed-paste>",),), tip=True,
    ),
    #  Windows 的终端宿主（Windows Terminal / VS Code / conhost）默认把 Ctrl-V
    #  截给自家"粘贴文本"：剪贴板是文本就变成一次 bracketed paste，是图片就
    #  毫无反应——按键根本到不了程序。所以补一个各家都不占的 Alt-V 作为并行
    #  组合，标签按平台只展示真按得动的那个。
    Binding(
        "input.pasteImage", "Alt-V" if _WINDOWS else "Ctrl-V",
        "把剪贴板里的图片收进这一条消息（截图直接贴；复制的多个文件一并收）"
        + ("；Windows 终端把 Ctrl-V 留给了文本粘贴，贴图用 Alt-V" if _WINDOWS else ""),
        INPUT, keys=(("c-v",), ("escape", "v")), hint="贴图", tip=True,
    ),
    Binding(
        "input.recall", "Esc Esc", "取回上一条发过的消息（改口重发）",
        INPUT, keys=(("escape", "escape"),), hint="取回上一条", tip=True,
    ),
    Binding(
        "app.toggleDetail", "Ctrl-O", "切换工具输出详略；开时先补出上一轮被折叠的输出",
        INPUT, keys=(("c-o",),), hint="工具详情", tip=True,
    ),
    Binding(
        "app.cycleMode", "Shift-Tab",
        "切模式：默认（逐条确认）→ auto（沙箱内自动放行）→ plan（只读规划）",
        INPUT, keys=(("s-tab",),), hint="切模式", tip=True,
    ),
    #  以下由 prompt_toolkit 或 REPL 循环实现，本表只登记，不注册
    Binding("history.previous", "↑ / ↓", "历史回退：视觉行 → 逻辑行 → 上一条", INPUT),
    Binding("history.search", "Ctrl-R", "反向搜索输入历史", INPUT, tip=True),
    Binding(
        "input.editor", "Ctrl-X Ctrl-E",
        "把当前输入拉进 $EDITOR 编辑（默认 vi）", INPUT, tip=True,
    ),
    Binding("app.exit", "Ctrl-C ×2", "退出（单次只提示，防误触）", INPUT, hint="退出"),
    Binding("app.eof", "Ctrl-D", "立即退出", INPUT),
    #  ---------- 输入行前缀 ----------
    Binding("prefix.slash", "/", "斜杠命令，带一行说明的补全", PREFIX),
    Binding(
        "prefix.shell", "!", "直接跑 shell，输出打屏并进入上下文",
        PREFIX, hint="直跑 shell", tip=True,
    ),
    Binding("prefix.file", "@", "模糊补全工作区文件路径", PREFIX, hint="补文件名", tip=True),
    Binding("prefix.memo", "#", "一句话记进项目指令文件", PREFIX, hint="记项目备忘", tip=True),
    #  ---------- 确认菜单 ----------
    Binding("menu.up", "↑↓", "选择", MENU, keys=(("up",),), hint="选择"),
    #  与 menu.up 合并显示成一个"↑↓"，但处理函数不同，命令必须分开
    Binding("menu.down", "↓", "选择", MENU, keys=(("down",),), show=False),
    Binding("menu.accept", "Enter", "确认", MENU, keys=(("enter",),), hint="确认"),
    Binding(
        "menu.amend", "Tab", "批准并附言（附言随 tool result 回灌模型）",
        MENU, keys=(("tab",),), hint="批准并附言",
    ),
    Binding(
        "menu.toggle", "Space", "勾选/取消勾选当前项（仅提问面板的多选题）",
        MENU, keys=(("space",),), hint="勾选",
    ),
    #  与输入行的 Ctrl-O 同一个心智动作"展开被折叠的内容"，用户只记一个键
    Binding(
        "menu.expand", "Ctrl-O", "补打被截断的预览全文进 scrollback（仅确认框预览超限时）",
        MENU, keys=(("c-o",),), hint="看全文",
    ),
    Binding(
        "menu.cancel", "Esc", "取消（等同拒绝）",
        MENU, keys=(("escape",), ("c-c",)), hint="取消",
    ),
    #  数字/首字母按选项动态注册（选项数量不固定），本表只登记。菜单键里唯一
    #  标 tip 的一个：menu_hint 里放不下它，除了轮播没有别的发现路径
    Binding("menu.digit", "1…9 / 首字母", "确认菜单里直接选中对应项", MENU, tip=True),
)

#  按命令查表，注册与测试都要用
BY_COMMAND: dict[str, Binding] = {item.command: item for item in BINDINGS}


#  ---------- 提交层输入路由 ----------
#
#  上表 PREFIX 分类的可执行形态（与绑定表同一模块，不另立文件）：
#  文档表加了前缀、这里没跟上（或反之），tests/test_input_router.py 的防漂移
#  断言会红。`@`（prefix.file）是补全层前缀，行提交时早已展开成路径，不进
#  本路由；模型作答期间的整行输入也不经过这里——那是 steer，斜杠不特判是
#  SteerPoller 的文档化决策。


@dataclass(frozen=True)
class InputAction:
    """一行已提交输入的去向。

    kind: "empty" | "slash" | "shell" | "memo" | "usage" | "send"
    args: slash=整行原文（handle_slash 自行 split）；shell=去 ! 去空白的命令；
          memo=去 # 去空白的备忘；send=原文
    hint: kind=="usage"（前缀对了但没内容）时给用户的提示
    """

    kind: str
    args: str = ""
    hint: str = ""


#  裸前缀的用法提示：只在这里出现，两个前端（TUI/明文 REPL）打的是同一句
_USAGE_HINTS = {
    "shell": "用法：!命令，如 !git status",
    "memo": "用法：# 备忘内容，如 # 测试统一跑 python -m pytest",
}


#  命令名的形状：/ + 字母开头的单 token（/help、/mcp、/skill:name）。
#  不含第二个 / 或点号——贴进输入行的绝对路径（/Users/…/简历.pdf）和
#  "/etc/hosts 是什么"这类开头像命令的话，都不该被当成命令吃掉。
_SLASH_COMMAND_SHAPE = re.compile(r"^/[A-Za-z][A-Za-z0-9_:-]*$")


def classify_input(line: str, *, literal: bool = False) -> InputAction:
    """所有前端的提交路由单点：一行输入 → 本地动作或发给模型。

    `literal=True`：行首不是用户敲的（粘贴 chip 展开回来的正文顶在第 0 位），
    ! / # / 前缀不算命令，整行原样发给模型——贴一段以 `!` 开头的日志不该
    跑成 shell，贴一段 `# 标题` 不该记成备忘。
    """
    line = line.strip()
    if not line:
        return InputAction("empty")
    if literal:
        return InputAction("send", line)
    if line.startswith("/"):
        head = line.split(None, 1)[0]
        #  路径形态（首 token 带第二个 / 或点号等）按普通输入发给模型，
        #  而不是报"未知命令"——ACP 侧 match_command 早有同款豁免，这里对齐。
        #  真敲错的命令（/halp）仍走 slash 报未知，保住可发现性。
        if head == "/" or _SLASH_COMMAND_SHAPE.match(head):
            return InputAction("slash", line)
        return InputAction("send", line)
    if line.startswith("!"):
        command = line[1:].strip()
        if not command:
            return InputAction("usage", hint=_USAGE_HINTS["shell"])
        return InputAction("shell", command)
    if line.startswith("#"):
        note = line.lstrip("#").strip()
        if not note:
            return InputAction("usage", hint=_USAGE_HINTS["memo"])
        return InputAction("memo", note)
    return InputAction("send", line)

#  需要 tui.py 提供处理函数的命令（即本表负责注册的那些）
REGISTERED: tuple[str, ...] = tuple(item.command for item in BINDINGS if item.keys)


def for_category(category: str) -> list[Binding]:
    """该分类下要展示的条目（合并显示的从属项已剔除）。"""
    return [item for item in BINDINGS if item.category == category and item.show]


def registered_in(*categories: str) -> set[str]:
    """这些分类下需要由代码提供处理函数的命令（含 show=False 的从属项）。"""
    return {item.command for item in BINDINGS if item.category in categories and item.keys}


def _hints(*categories: str) -> str:
    parts = [
        f"{item.label} {item.hint}"
        for category in categories
        for item in for_category(category)
        if item.hint
    ]
    return " · ".join(parts)


def hint_line() -> str:
    """首屏那行速览：前缀在前、按键在后（先告诉用户能打什么，再说能按什么）。"""
    return _hints(PREFIX, INPUT)


def menu_hint(exclude: tuple[str, ...] = ()) -> str:
    """确认菜单底部那行提示，同样从表来。

    exclude 按命令名剔除条目：同一套菜单组件也给会话选择这类"纯单选"用，
    那里 Tab=附言 不适用，提示行不能承诺一个没有效果的键。
    """
    parts = [
        f"{item.label} {item.hint}"
        for item in for_category(MENU)
        if item.hint and item.command not in exclude
    ]
    return " · ".join(parts)


def tips() -> list[str]:
    """等待期轮播的上手提示（spinner 用）。

    取自表里标了 tip 的条目——"不主动说就没人会发现"的功能（前缀符号和
    组合键都不会有人瞎按到）。曾经复用 hint 字段当筛选条件，结果轮播里
    混进"↑↓ 选择、Enter 确认"这种谁都知道的菜单键，真正隐蔽却没资格进
    速览行的键（Alt-Enter、Ctrl-X Ctrl-E、Ctrl-R）反而进不来——两个展示
    面的取舍标准不同，必须是两个字段。不另立一份提示文案，那又是一处会
    漂移的字符串。
    """
    return [f"{item.label} {item.desc}" for item in BINDINGS if item.tip]


def help_text() -> str:
    """`/keys` 的完整列表，按分类分组。

    对齐按可见宽度算：标签里有中文（"粘贴"、"首字母"），用 len() 会错位。
    """
    from . import ui

    lines: list[str] = []
    width = max(ui.display_width(item.label) for item in BINDINGS if item.show)
    for category in CATEGORIES:
        lines.append(f"  {ui.heading(category)}")
        for item in for_category(category):
            lines.append(f"    {ui.pad(item.label, width)}  {ui.secondary(item.desc)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
