"""语义色 token：全部配色的唯一事实来源。

问题：颜色以前直接以"红/黄/绿/dim"的形态散在四个模块一百多处。想换配色要
逐处改；想支持浅色终端更无从下手——**黄色配白底几乎不可读**，而 warning
恰恰全是黄的。

做法：调用方只说意图（`status.warning`），不说颜色。意图 → 颜色的映射集中
在本模块的两张表里（深色/浅色终端各一张）。两个消费端从同一份 Style 生成：

- 纯 ANSI（`ui.py`，给明文 REPL / 横幅 / 一次性执行）；
- rich 的 Theme（`tui.py`，`style="status.warning"` 直接可用）。

因为两边由同一个 Style 对象派生，不存在"改了 rich 忘了改 ANSI"的漂移。

深色表刻意用 ANSI 基础色名（`base="yellow"` → `\\033[33m`），与引入本模块之前
逐字节一致：基础色跟随用户自己配的终端调色板，是终端主题该有的样子。浅色表
才改用 256 色绝对色号——白底上必须压深，不能听凭调色板。

模式选择：`XIAOYU_THEME=dark|light|auto`，默认 auto。auto 当前解析为 dark；
探测终端真实背景色（OSC 11）之后调 `set_mode()` 即可让 auto 名副其实。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

Mode = Literal["dark", "light"]


@dataclass(frozen=True)
class Style:
    """一个 token 的样式。base 与 color 二选一：

    - `base`：ANSI 基础色名（跟随终端调色板，深色表用）；
    - `color`：256 色号（绝对色，浅色表用）。
    """

    base: str | None = None
    color: int | None = None
    on: int | None = None  # 背景 256 色号（diff 词级高亮用）
    dim: bool = False
    bold: bool = False

    _BASE_CODES = {  # noqa: RUF012 - 常量表，dataclass 字段之外
        "black": 30, "red": 31, "green": 32, "yellow": 33,
        "blue": 34, "magenta": 35, "cyan": 36, "white": 37,
    }

    def ansi(self) -> str:
        """SGR 参数串，如 "2" / "33" / "38;5;130;1"。空串 = 不着色。"""
        parts: list[str] = []
        if self.dim:
            parts.append("2")
        if self.bold:
            parts.append("1")
        if self.base is not None:
            parts.append(str(self._BASE_CODES[self.base]))
        elif self.color is not None:
            parts.append(f"38;5;{self.color}")
        if self.on is not None:
            parts.append(f"48;5;{self.on}")
        return ";".join(parts)

    def rich(self) -> str:
        """rich 样式串，如 "dim" / "yellow" / "color(130) bold"。"""
        parts: list[str] = []
        if self.dim:
            parts.append("dim")
        if self.bold:
            parts.append("bold")
        if self.base is not None:
            parts.append(self.base)
        elif self.color is not None:
            parts.append(f"color({self.color})")
        if self.on is not None:
            parts.append(f"on color({self.on})")
        return " ".join(parts) or "none"

    def ptk(self) -> str:
        """prompt_toolkit 样式串，如 "fg:ansiyellow" / "fg:#af5f00 bold"。

        prompt_toolkit 不认 256 色号，得换算成十六进制；它也没有 dim，
        用暗灰前景近似（确认菜单的提示行本来就是这个观感）。
        """
        parts: list[str] = []
        if self.base is not None:
            parts.append(f"fg:ansi{self.base}")
        elif self.color is not None:
            parts.append(f"fg:{to_hex(self.color)}")
        elif self.dim:
            parts.append("fg:ansibrightblack")
        if self.on is not None:
            parts.append(f"bg:{to_hex(self.on)}")
        if self.bold:
            parts.append("bold")
        return " ".join(parts)


#  xterm 256 色立方体的六级亮度
_CUBE_LEVELS = (0, 95, 135, 175, 215, 255)


def to_hex(code: int) -> str:
    """256 色号 → "#rrggbb"（prompt_toolkit 只吃十六进制）。"""
    if code >= 232:  # 232-255：24 级灰阶
        gray = 8 + (code - 232) * 10
        return f"#{gray:02x}{gray:02x}{gray:02x}"
    if code >= 16:  # 16-231：6×6×6 色立方
        index = code - 16
        rgb = (
            _CUBE_LEVELS[index // 36],
            _CUBE_LEVELS[(index % 36) // 6],
            _CUBE_LEVELS[index % 6],
        )
        return "#{:02x}{:02x}{:02x}".format(*rgb)
    #  0-15 是终端自己配的调色板，没有固定 RGB；本模块的表也不用它们
    raise ValueError(f"低 16 色随终端调色板而变，请改用 base=：{code}")


#  深色终端（默认）：沿用引入 token 层之前的配色，逐字节一致
_DARK: dict[str, Style] = {
    #  正文与层级
    "text.primary": Style(),
    "text.secondary": Style(dim=True),
    "text.heading": Style(bold=True),
    "text.accent": Style(base="cyan"),
    #  状态
    "status.success": Style(base="green"),
    "status.warning": Style(base="yellow"),
    "status.error": Style(base="red"),
    #  diff 预览
    "diff.hunk": Style(base="cyan"),
    "diff.added": Style(base="green"),
    "diff.removed": Style(base="red"),
    "diff.added.word": Style(base="white", on=22),
    "diff.removed.word": Style(base="white", on=52),
    #  交互件
    "prompt": Style(base="green"),
    "menu.title": Style(base="yellow"),
    "menu.selected": Style(base="cyan", bold=True),
    "menu.hint": Style(dim=True),
    #  横幅（纯装饰，浅色表另配一套压深的）。框架与 Logo 同色相、只降明度：
    #  亮青标题 → 外框 → 内分隔线，层级靠明度阶梯拉开，不引入第二色相
    "banner.accent": Style(color=39),
    "banner.feather": Style(color=153),
    "banner.border": Style(color=31),
    "banner.divider": Style(color=24),
}

#  横幅块字的逐行渐变（天空 → 深海）
_DARK_GRADIENT = (117, 117, 81, 45, 39, 33)

#  浅色终端：白底上基础色不可用——黄近乎隐形、亮青发灰。全部换成压深的 256 色。
_LIGHT: dict[str, Style] = {
    "text.primary": Style(),
    "text.secondary": Style(color=240),
    "text.heading": Style(bold=True),
    "text.accent": Style(color=25),
    "status.success": Style(color=28),
    "status.warning": Style(color=130),  # 黄→棕橙：白底上唯一能读的"警告色"
    "status.error": Style(color=124),
    "diff.hunk": Style(color=25),
    "diff.added": Style(color=28),
    "diff.removed": Style(color=124),
    "diff.added.word": Style(color=22, on=194),
    "diff.removed.word": Style(color=52, on=224),
    "prompt": Style(color=28),
    "menu.title": Style(color=130),
    "menu.selected": Style(color=25, bold=True),
    "menu.hint": Style(color=240),
    "banner.accent": Style(color=25),
    "banner.feather": Style(color=67),
    "banner.border": Style(color=31),
    "banner.divider": Style(color=67),
}

_LIGHT_GRADIENT = (33, 33, 26, 25, 24, 23)

_TABLES: dict[Mode, dict[str, Style]] = {"dark": _DARK, "light": _LIGHT}
_GRADIENTS: dict[Mode, tuple[int, ...]] = {"dark": _DARK_GRADIENT, "light": _LIGHT_GRADIENT}


def _initial_mode() -> Mode:
    #  auto 暂时落到 dark：探测背景色之前没有依据，而深色终端是压倒性多数
    requested = os.environ.get("XIAOYU_THEME", "auto").strip().lower()
    return "light" if requested == "light" else "dark"


_mode: Mode = _initial_mode()


def mode() -> Mode:
    return _mode


def set_mode(new_mode: Mode) -> None:
    """切换明暗（背景色探测出结果后调用）。

    已经打印出去的内容不受影响——终端的 scrollback 改不了，切换只对之后的
    输出生效。
    """
    global _mode
    if new_mode not in _TABLES:
        raise ValueError(f"未知主题：{new_mode}")
    _mode = new_mode


def style(token: str) -> Style:
    """取一个 token 的样式。未知 token 直接抛——拼错的样式名应该当场炸，
    而不是静默变成无色。"""
    try:
        return _TABLES[_mode][token]
    except KeyError:
        raise KeyError(f"未定义的样式 token：{token}") from None


def tokens() -> list[str]:
    """全部 token 名（两张表结构相同，取深色表即可）。"""
    return list(_DARK)


def gradient() -> tuple[int, ...]:
    """横幅块字的逐行渐变色号。"""
    return _GRADIENTS[_mode]


def rich_theme() -> dict[str, str]:
    """给 `rich.theme.Theme(...)` 的映射：token 名 → rich 样式串。"""
    return {name: item.rich() for name, item in _TABLES[_mode].items()}


def ptk(token: str) -> str:
    """单个 token 的 prompt_toolkit 样式串（提示符、行内追问用）。"""
    return style(token).ptk()


def ptk_style() -> dict[str, str]:
    """给 `prompt_toolkit.styles.Style.from_dict(...)` 的映射。

    ptk 的 class 名不能带点，统一把 token 里的 `.` 换成 `-`：
    `menu.title` → `menu-title`，用处是 `class:menu-title`。
    """
    return {name.replace(".", "-"): item.ptk() for name, item in _TABLES[_mode].items()}
