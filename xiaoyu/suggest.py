"""轮末的「接着问」建议：用辅助模型从本轮一问一答里提两三条下一步。

走旁路、不进主对话：轮结束后另起一个小请求给辅助模型（`summary_model`，
默认是便宜模型），输入只有本轮用户原话与模型回答的摘录，输出几条短句。
主模型的回答一个字不动，建议请求不进历史、不占上下文、不参与压缩。

刻意不让主模型在回答末尾夹带建议块：十几家 provider 加本机模型的格式
遵从度参差，流式还得边收边剥，剥不干净就是打到屏上的格式垃圾；常驻一条
格式指令也会污染回答风格。

本模块只管"问什么"与"答案怎么解析"，纯函数、不碰网络；发请求与线程在
`Agent` 里（它持有路由与账本）。
"""

from __future__ import annotations

import json
import re

#  最多几条；短句上限按字符算，中文一句 12 字左右，英文给到同等视觉长度
MAX_ITEMS = 3
MAX_CHARS = 48
#  喂给辅助模型的摘录上限：用户原话一般短，回答取头尾（结论多在尾部）
_USER_CAP = 1500
_ANSWER_HEAD = 1800
_ANSWER_TAIL = 1200

INSTRUCTION = (
    "你是一个编程助手的旁观者。下面是用户的一句话和助手的回答。"
    "请给出用户**接下来最可能想对助手说的话**，作为可以直接发送的下一条输入。\n"
    "要求：\n"
    "- 最多 3 条，每条是一句祈使句或问题，不超过 12 个字（英文不超过 8 个词）；\n"
    "- 必须紧扣这一轮的内容（继续、验证、修复、展开、收尾），不要泛泛的客套；\n"
    "- 用用户使用的语言；\n"
    "- 判断不出有价值的下一步就返回空数组。\n"
    "只输出一个 JSON 字符串数组，不要解释、不要代码块。"
)


def _clip(text: str, head: int, tail: int = 0) -> str:
    text = text.strip()
    if len(text) <= head + tail:
        return text
    if not tail:
        return text[:head] + "…"
    return text[:head] + "\n…（中略）…\n" + text[-tail:]


def build_messages(user_text: str, assistant_text: str) -> list[dict[str, str]]:
    """辅助模型的请求体：一条 user 消息，指令在前、材料在后。"""
    material = (
        f"【用户说】\n{_clip(user_text, _USER_CAP)}\n\n"
        f"【助手回答】\n{_clip(assistant_text, _ANSWER_HEAD, _ANSWER_TAIL)}"
    )
    return [{"role": "user", "content": f"{INSTRUCTION}\n\n---\n\n{material}"}]


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.、)]|[①②③④⑤⑥⑦⑧⑨])\s*")


def parse(content: str) -> list[str]:
    """把模型输出整理成建议列表；宽进严出，解析不出就返回空。

    主路径是 JSON 数组；模型没按格式来（套了代码块、写成项目符号列表）时
    退回按行取。每条去引号去编号、剔控制字符、超长截断、去重，最多 MAX_ITEMS。
    """
    text = _FENCE.sub("", content or "").strip()
    items: list[str] = []
    try:
        loaded = json.loads(text)
    except ValueError:
        #  不是 JSON：按行取
        items = [_BULLET.sub("", line) for line in text.splitlines()]
    else:
        if isinstance(loaded, list):
            items = [str(item) for item in loaded if isinstance(item, (str, int, float))]
        elif isinstance(loaded, dict):
            #  {"suggestions": [...]} 这类包了一层的也认
            for value in loaded.values():
                if isinstance(value, list):
                    items = [str(item) for item in value if isinstance(item, str)]
                    break
        #  合法 JSON 却不是数组/对象（null、数字、裸字符串）：没有建议
    cleaned: list[str] = []
    for item in items:
        item = "".join(ch for ch in item if ch.isprintable()).strip().strip("\"'“”‘’")
        if not item:
            continue
        if len(item) > MAX_CHARS:
            item = item[: MAX_CHARS - 1] + "…"
        if item not in cleaned:
            cleaned.append(item)
        if len(cleaned) >= MAX_ITEMS:
            break
    return cleaned


def render_line(items: list[str]) -> str:
    """各前端共用的那一行文案：接着问：① … ② … ③ …"""
    marks = "①②③④⑤"
    return "接着问：" + "  ".join(f"{marks[i]} {item}" for i, item in enumerate(items))
