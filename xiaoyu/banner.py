"""启动横幅：进 REPL 时的门面。零依赖纯 ANSI，窄终端自动降级。

宽终端是圆角框分栏：标题嵌在上边框，左栏欢迎语 +
吉祥物 + 模型/工作区，右栏上手提示与本版新特性。特别宽时左栏放 XIAOYU
块字，一般宽时放两片羽毛。配色沿用由浅入深的天蓝渐变（256 色，现代终端
包括 Windows Terminal 都认）——羽毛的意象；一次性执行模式不打横幅，
不干扰管道输出。

降级四档：宽 = 圆角框分栏；中 = 块字 + 羽毛；窄 = 只保块字；再窄 = 单行。
"""

from __future__ import annotations

import getpass
import os
import shutil
from pathlib import Path

from . import __version__, theme, ui

#  XIAOYU 块字，6 行 × 56 列（字母间隔 2 空格，挤在一起认不出字）；
#  行首不缩进，留给终端自己的边距
_ART = (
    "██╗  ██╗  ██╗   █████╗    ██████╗   ██╗   ██╗  ██╗   ██╗",
    "╚██╗██╔╝  ██║  ██╔══██╗  ██╔═══██╗  ╚██╗ ██╔╝  ██║   ██║",
    " ╚███╔╝   ██║  ███████║  ██║   ██║   ╚████╔╝   ██║   ██║",
    " ██╔██╗   ██║  ██╔══██║  ██║   ██║    ╚██╔╝    ██║   ██║",
    "██╔╝ ██╗  ██║  ██║  ██║  ╚██████╔╝     ██║     ╚██████╔╝",
    "╚═╝  ╚═╝  ╚═╝  ╚═╝  ╚═╝   ╚═════╝      ╚═╝      ╚═════╝ ",
)
#  逐行渐变（天空 → 深海）住在 theme 里：浅色终端上淡天蓝会糊在白底上，
#  得整体压深。取色在调用时进行，这样 set_mode() 之后打的横幅立刻跟上。

#  两片羽毛（小羽的"羽"）：中档终端跟在块字后面退居背景，
#  圆角框模式则升格为左栏吉祥物（配渐变色）
_FEATHERS = (
    "  ,\\      ,\\   ",
    " / )\\    / )\\  ",
    "/ / )\\  / / )\\ ",
    "/ / / ) / / / )",
    "\\ \\/ /  \\ \\/ / ",
    " `--´    `--´  ",
)
_GAP = "  "

_TAGLINE = "织码如羽 · a harness coding agent"

#  每个版本手工维护的亮点，进右栏「新特性」；保持短句，右栏太窄会截断
_WHATS_NEW = (
    "Shift-Tab 切 auto 档：沙箱兜得住的改动不再逐条问",
    "横幅门面上新：框架同色相蓝描边，层级更清晰",
    "等模型时显示「思考中 · Ns」，不再一片死寂",
)

#  块字 56 列 + 少量余量；再窄就放不下，退到单行
_MIN_WIDTH = 60
#  块字 + 间隔 + 羽毛的总宽；不够就只舍羽毛，块字保留
_FULL_WIDTH = len(_ART[0]) + len(_GAP) + max(len(row) for row in _FEATHERS)
#  圆角框 + 左右两栏放得下的最小终端宽；再窄退回块字
_BOX_MIN = 80
#  羽毛版左栏内宽：羽毛居中 + 模型/工作区一行放得下
_LEFT_INNER = 40


def _banner_color(token: str) -> int:
    """横幅专用取色：token 一定配的是 256 色号（见 theme 的 banner.* 各项）。"""
    code = theme.style(token).color
    assert code is not None, f"{token} 必须配 256 色号"
    return code


#  可见宽度：CJK 全角算 2 列（框对齐全靠它）。实现在 ui，按键帮助表也要用
_vis = ui.display_width


def _fit(text: str, width: int) -> str:
    """按可见宽度截断，截了补省略号。"""
    if _vis(text) <= width:
        return text
    out, used = [], 0
    for ch in text:
        w = _vis(ch)
        if used + w > width - 1:
            break
        out.append(ch)
        used += w
    return "".join(out) + "…"


def _tilde(path: str) -> str:
    """把 home 前缀缩成 ~，路径短一截更好看。"""
    home = str(Path.home())
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


def _box_banner(model: str, workspace: str, width: int) -> str:
    #  留 1 列余量：部分终端整行写满会立刻换行，框就断了
    box = width - 1

    #  左栏吉祥物：够宽放 XIAOYU 块字，不够放羽毛（都配天蓝渐变）
    art_inner = len(_ART[0]) + 2
    if box >= art_inner + 47:  # 右栏至少还剩 40 列，长句不用截
        left_w, mascot = art_inner, tuple(zip(theme.gradient(), _ART))
    else:
        left_w, mascot = _LEFT_INNER, tuple(zip(theme.gradient(), _FEATHERS))
    right_w = box - left_w - 7

    def cell(plain: str, w: int, style=None, center: bool = False) -> str:
        plain = _fit(plain, w)
        gap = w - _vis(plain)
        lead = gap // 2 if center else 0
        text = style(plain) if style and plain else plain
        return " " * lead + text + " " * (gap - lead)

    def heading(text: str) -> str:
        return ui.heading(ui.color256(_banner_color("banner.accent"), text))

    def frame(text: str) -> str:
        return ui.color256(_banner_color("banner.border"), text)

    def divider(text: str) -> str:
        return ui.color256(_banner_color("banner.divider"), text)

    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - 拿不到用户名就不带名字
        user = ""

    #  每行一个 (文本, 上色函数, 是否居中)
    left = [
        ("", None, False),
        (f"欢迎回来，{user}！" if user else "欢迎回来！", ui.heading, True),
        ("", None, False),
        *[(row, (lambda t, c=c: ui.color256(c, t)), True) for c, row in mascot],
        ("", None, False),
        (_TAGLINE, ui.secondary, True),
        (model, ui.secondary, True),
        (_tilde(workspace), ui.secondary, True),
    ]
    right = [
        ("上手提示", heading, False),
        ("直接输入任务，回车开干", None, False),
        ("/help 查看全部命令，/exit 退出", None, False),
        ("AGENTS.md 写项目规范，跟着仓库走", None, False),
        ("", None, False),
        ("─" * right_w, divider, False),
        ("新特性", heading, False),
        *[(item, None, False) for item in _WHATS_NEW],
        ("", None, False),
        ("README 有完整说明", ui.secondary, False),
    ]
    blank = ("", None, False)
    height = max(len(left), len(right))
    left += [blank] * (height - len(left))
    right += [blank] * (height - len(right))

    title = f" 小羽 · Xiaoyu v{__version__} "
    top = frame("╭─") + heading(title) + frame("─" * (box - 3 - _vis(title)) + "╮")
    edge, mid = frame("│"), divider("│")
    rows = [
        f"{edge} {cell(lp, left_w, ls, lc)} {mid} {cell(rp, right_w, rs, rc)} {edge}"
        for (lp, ls, lc), (rp, rs, rc) in zip(left, right)
    ]
    bottom = frame("╰" + "─" * (box - 2) + "╯")
    return "\n".join([top, *rows, bottom])


def build_banner(model: str, workspace: str, width: int | None = None) -> str:
    """返回完整横幅字符串；width 只在测试时显式传入。"""
    if width is None:
        width = shutil.get_terminal_size().columns
    if width >= _BOX_MIN:
        return f"\n{_box_banner(model, workspace, width)}\n"
    if width < _MIN_WIDTH:
        return f"{ui.heading('小羽 · Xiaoyu')} v{__version__}  {ui.secondary('/help 查看命令')}"

    with_feathers = width >= _FULL_WIDTH + 2
    rows = []
    for color, line, feather in zip(theme.gradient(), _ART, _FEATHERS):
        row = ui.color256(color, line)
        if with_feathers:
            row += _GAP + ui.color256(_banner_color("banner.feather"), feather)
        rows.append(row)
    art = "\n".join(rows)
    title = ui.heading("小羽 · Xiaoyu") + "  " + ui.secondary(f"{_TAGLINE} · v{__version__}")
    meta = ui.secondary(f"模型 {model} · 工作区 {workspace} · /help 查看命令")
    return f"\n{art}\n\n{title}\n{meta}\n"
