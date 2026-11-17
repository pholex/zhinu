"""隐形 Unicode 字符剥离：外部文本进模型上下文前统一过这一道。

Unicode Tag 块（U+E0000–E007F）在终端、编辑器、diff 里一律不显示，模型却按
token 读得到——仓库里的指令文件、网页、MCP 工具描述里埋一段 Tag 字符拼出的
指令，人审阅时什么都看不见，模型照做。零宽空格、word joiner、不可见数学
运算符、ZWNBSP（BOM 的另一个身份）同理：全是"看不见但进上下文"的载体。

剥的是一个闭集，刻意不剥的两类：
- ZWJ / ZWNJ（U+200C / U+200D）：emoji 序列、阿拉伯文与印度语系的正常拼写都
  靠它们，剥掉会把正文写坏；
- 双向控制（U+200E/200F、U+202A–202E、U+2066–2069）：那是**显示层**的问题
  （审批框里的命令被反转），由 ui.strip_sequences 转成可见的 \\uXXXX，
  模型这一侧原样保留——它读的是码点序列，不受视觉反转影响。

所有入口共用这一个函数，别各自再写一份正则：集合要改只改这里。
"""

from __future__ import annotations

import re
import sys

_INVISIBLE = re.compile("[​⁠-⁤﻿\U000e0000-\U000e007f]")

#  只提示一次：命中多半是同一份文件/同一个 server 反复出现，每次都喊是刷屏
_warned = False


def strip_invisible(text: str, where: str = "") -> str:
    """剥掉隐形字符；首次命中时 stderr 留一行痕迹（整个进程只说一次）。"""
    stripped, removed = _INVISIBLE.subn("", text)
    if removed:
        _warn_once(where, removed)
    return stripped


def has_invisible(text: str) -> bool:
    return _INVISIBLE.search(text) is not None


def _warn_once(where: str, removed: int) -> None:
    global _warned
    if _warned:
        return
    _warned = True
    label = f"{where}里" if where else "外部文本里"
    print(
        f"[{label}剥掉了 {removed} 个隐形 Unicode 字符（Tag 块/零宽字符，"
        "常见于隐写式提示注入）。本提示每个进程只出现一次]",
        file=sys.stderr,
    )
