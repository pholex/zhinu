"""终端输出的小工具（零依赖，直接用 ANSI）。

着色一律走 `theme.py` 的语义 token：这里只负责把 Style 翻译成转义序列，
"警告是什么颜色"由 theme 决定。想加一处新配色就去 theme 加 token，不要在
这里新增裸颜色函数——那正是当初一百多处散落颜色的来源。

宽度同理：不要再写死 `preview(x, 160)`。80 列终端上 160 字符的"一行摘要"
会折成两行，折叠就白折了。用 `fit()` 按终端实际宽度算预算。
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import unicodedata

from . import theme

if os.name == "nt":
    #  让 conhost（传统 cmd）开启 VT 转义处理；Windows Terminal 本来就支持
    os.system("")

#  三个标准流一律钉死 UTF-8。默认是 locale 编码：Windows 的管道/重定向是
#  GBK/cp1252，POSIX 上 LANG=en_US.ISO8859-1 之类的老 locale 同理——打印中文
#  直接 UnicodeEncodeError（整个 CLI 的自有文案都是中文，等于开不了机），
#  stream-json / wire 两个协议面又都以 UTF-8 为契约。交互控制台本来就是 UTF-8
#  的机器不受影响；老 locale 的终端上顶多花屏，好过崩掉。
for _stream in (sys.stdout, sys.stderr):
    with contextlib.suppress(Exception):
        _stream.reconfigure(encoding="utf-8")
#  stdin 是解码方向（外部字节进程序），按编码纪律得有 replace 兜底：
#  wire 请求或 `xiaoyu < prompt.txt` 里坏一个字节，不该让整个会话起不来。
with contextlib.suppress(Exception):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

_ENABLED = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None

#  窄到这个宽度以下就不再为终端宽度让步了：再减就没有可读的信息量，
#  不如让终端自己去折行
_MIN_BUDGET = 40
#  拿不到真实终端尺寸时的假定（管道、CI、非 tty）
_FALLBACK = (80, 24)


def styled(token: str, text: str) -> str:
    """按语义 token 着色。token 名见 theme.py。"""
    if not _ENABLED:
        return text
    code = theme.style(token).ansi()
    if not code:
        return text
    return f"\033[{code}m{text}\033[0m"


def secondary(text: str) -> str:
    """次要信息：提示、脚注、已折叠的内容。"""
    return styled("text.secondary", text)


def heading(text: str) -> str:
    return styled("text.heading", text)


def accent(text: str) -> str:
    """可交互/可点的东西：工具名、命令、路径。"""
    return styled("text.accent", text)


def success(text: str) -> str:
    return styled("status.success", text)


def warning(text: str) -> str:
    return styled("status.warning", text)


def error(text: str) -> str:
    return styled("status.error", text)


def prompt(text: str) -> str:
    """等待用户输入的提示符。"""
    return styled("prompt", text)


def color256(code: int, text: str) -> str:
    """256 色前景的直通口，只给横幅渐变用——它是纯装饰，逐行取色，
    套不进语义 token 的框子。"""
    if not _ENABLED:
        return text
    return f"\033[38;5;{code}m{text}\033[0m"


def display_width(text: str) -> int:
    """可见宽度：CJK 全角算 2 列。对齐用 len() 会让含中文的列全部错位。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int) -> str:
    """按可见宽度左对齐补空格。"""
    return text + " " * max(width - display_width(text), 0)


def term_size() -> tuple[int, int]:
    """终端宽高。拿不到（管道/CI）就用 80×24。"""
    size = shutil.get_terminal_size(fallback=_FALLBACK)
    return size.columns, size.lines


def term_width() -> int:
    return term_size()[0]


def budget(reserve: int = 0, width: int | None = None) -> int:
    """一行能放下的字符预算：终端宽度减去调用方已占用的前缀/缩进。

    `width` 显式传入时用它（rich 的 Console 有自己的宽度，测试里也会指定）。
    """
    return max((width if width is not None else term_width()) - reserve, _MIN_BUDGET)


def preview(value: object, limit: int = 100) -> str:
    """把工具参数压成一行短预览，**含省略号在内**不超过 limit 个字符。

    省略号必须算进预算里：limit 现在就是终端宽度，多吐一个字符这行就折行了，
    "压成一行"的意义随之落空。
    """
    text = str(value).replace("\n", "⏎")
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


def fit(value: object, reserve: int = 0, width: int | None = None) -> str:
    """按终端实际宽度压成一行（`preview` 的自适应版）。"""
    return preview(value, budget(reserve, width))
