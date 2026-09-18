"""提示词文件的读入约定（--system-prompt-file / --append-system-prompt-file）。

文件内容原样使用，只有一条例外：**块级 HTML 注释不发给模型**——

    <!-- 写给维护者看的说明：这份提示词怎么改、占位符填什么 -->

选 `<!-- -->` 是因为它与提示词正文几乎不撞车（`#` 是 markdown 标题，`//` 撞代码
和 URL），天然多行，markdown 编辑器本来就认。边界刻意收得很窄，宁可少剥不错剥：

- 只认块级：`<!--` 在行首（前面只许空白），`-->` 之后到行尾只许空白。
  行内夹着的 `a <!-- b --> c` 不动；
- 代码围栏（``` / ~~~）里的不动——那是给模型看的示例正文；
- 没闭合的 `<!--` 不剥，把行号报给调用方去警告。否则漏写一个 `-->` 就会让
  后半份提示词悄悄消失。

剥掉一块后，它前后的空行并成一个；别处的空行不碰。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_OPEN, _CLOSE = "<!--", "-->"
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
#  模板占位符 `{{NAME}}`：只用于提醒"拿着没填完的模板直接上了"，不改内容
_PLACEHOLDER = re.compile(r"\{\{[^{}\n]+\}\}")


@dataclass
class PromptFile:
    text: str
    #  没闭合的 `<!--` 所在行号（1 起）
    unclosed: list[int] = field(default_factory=list)
    #  剥注释之后还留在正文里的 `{{...}}` 个数
    placeholders: int = 0

    def warnings(self, label: str) -> list[str]:
        notes = [
            f"{label}：第 {line} 行的 <!-- 没有闭合（或 --> 后面还跟着正文），"
            "这段按正文原样发给了模型"
            for line in self.unclosed
        ]
        if self.placeholders:
            notes.append(
                f"{label}：还有 {self.placeholders} 处 {{{{…}}}} 形态的占位符，"
                "若是没填完的模板请先填好（确属正文可忽略）"
            )
        return notes


def strip_comments(text: str) -> tuple[str, list[int]]:
    """剥掉块级 HTML 注释。返回 (剥后的文本, 没闭合的 <!-- 行号)。"""
    lines = text.splitlines()
    kept: list[str] = []
    unclosed: list[int] = []
    fence = ""  # 当前围栏的开栏标记（如 "```"）；空 = 不在围栏里
    index = 0
    while index < len(lines):
        line = lines[index]
        if fence:
            kept.append(line)
            closing = _FENCE.match(line)
            #  收栏：同一种字符、不短于开栏、后面不带别的
            if (
                closing
                and closing[1][0] == fence[0]
                and len(closing[1]) >= len(fence)
                and not line.strip().strip(fence[0])
            ):
                fence = ""
            index += 1
            continue
        if opening := _FENCE.match(line):
            fence = opening[1]
            kept.append(line)
            index += 1
            continue
        if not line.lstrip().startswith(_OPEN):
            kept.append(line)
            index += 1
            continue
        end = _comment_end(lines, index)
        if end is None:
            unclosed.append(index + 1)
            kept.append(line)
            index += 1
            continue
        index = end + 1
        #  注释两侧的空行并成一个（文件开头的注释后面不留空行）
        while (
            index < len(lines)
            and not lines[index].strip()
            and (not kept or not kept[-1].strip())
        ):
            index += 1
    return "\n".join(kept), unclosed


def _comment_end(lines: list[str], start: int) -> int | None:
    """从 start 行的 `<!--` 起找收尾行号；不是合规的块级注释回 None。"""
    offset = lines[start].index(_OPEN) + len(_OPEN)
    for number in range(start, len(lines)):
        body = lines[number][offset:] if number == start else lines[number]
        at = body.find(_CLOSE)
        if at < 0:
            continue
        return number if not body[at + len(_CLOSE) :].strip() else None
    return None


def parse(raw: str) -> PromptFile:
    text, unclosed = strip_comments(raw)
    text = text.strip()
    return PromptFile(text, unclosed, len(_PLACEHOLDER.findall(text)))


def load(path: Path) -> PromptFile:
    """读提示词文件。读不了抛 OSError / UnicodeDecodeError，交给调用方组织报错。"""
    #  utf-8-sig：Windows 记事本存的文件带 BOM，留着会成为 prompt 的第一个字符
    return parse(path.read_text(encoding="utf-8-sig"))
