"""token 估算。

为什么要自己估：走 Mantle / Responses API 的模型（gpt-5.x、grok）Bedrock 不回
usage，`response.usage` 拿不到值，压缩决策不能依赖它。

记账方式（锚点 + 增量，逻辑在 agent.py）：拿得到真实 usage 时以它为
锚点（权威覆盖当时的全部消息 + 工具 schema），本地只估算锚点之后新增的部分——
误差不随会话累积。拿不到 usage 就退化为纯本地估算。
"""

from __future__ import annotations

import json
from typing import Any

from . import media

#  中日韩、假名、谚文的常用区间：这些字符基本 1 字 ≈ 1 token
_CJK_RANGES = (
    (0x3000, 0x303F),
    (0x3040, 0x30FF),
    (0x3400, 0x4DBF),
    (0x4E00, 0x9FFF),
    (0xAC00, 0xD7AF),
    (0xF900, 0xFAFF),
    (0xFF00, 0xFFEF),
)

#  英文/代码大约 3.7 个字符 ≈ 1 token（代码比散文密）
_CHARS_PER_TOKEN = 3.7

#  每条消息的角色、分隔符等固定开销
_MESSAGE_OVERHEAD = 4


def _is_cjk(char: str) -> bool:
    code = ord(char)
    return any(low <= code <= high for low, high in _CJK_RANGES)


def estimate_text(text: str | None) -> int:
    if not text:
        return 0
    cjk = sum(1 for char in text if _is_cjk(char))
    return int(cjk + (len(text) - cjk) / _CHARS_PER_TOKEN) + 1


def estimate_content(content: Any) -> int:
    """content 的 token 估算。部件列表按「文本逐部件估 + 每张图固定 IMAGE_TOKENS」。

    ⚠️ 图片**绝不能**按字符估：内核历史里存的是 `xiaoyu-media://<sha256>` 引用
    （约 80 字符 ≈ 20 token），照字面估等于把一张真实要花上千 token 的图算成
    几乎免费，压缩永远不触发、最后撞上游的上下文上限——而那条错误路径上
    没有任何提示能指回这里。
    """
    if not media.is_parts(content):
        return estimate_text(content)
    total = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == media.IMAGE_PART:
            total += media.IMAGE_TOKENS
        else:
            total += estimate_text(part.get("text"))
    return total


def estimate_message(message: dict[str, Any]) -> int:
    total = _MESSAGE_OVERHEAD + estimate_content(message.get("content"))
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        total += estimate_text(function.get("name"))
        total += estimate_text(function.get("arguments"))
        total += _MESSAGE_OVERHEAD
    return total


def estimate_messages(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_message(message) for message in messages)


def estimate_tools(schemas: list[dict[str, Any]]) -> int:
    """工具 schema 每轮都要随请求发送，不能漏算。"""
    if not schemas:
        return 0
    return estimate_text(json.dumps(schemas, ensure_ascii=False))
