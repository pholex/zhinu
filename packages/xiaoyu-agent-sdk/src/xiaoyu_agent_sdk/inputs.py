"""Typed host input translated to private kernel content blocks."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from xiaoyu.media import MAX_IMAGE_BYTES, sniff_mime

from .types import ConfigurationError


@dataclass(frozen=True)
class TextBlock:
    text: str


@dataclass(frozen=True)
class ImageBlock:
    """Encoded PNG, JPEG, GIF or WebP bytes supplied explicitly by the host."""

    data: bytes = field(repr=False)


Prompt: TypeAlias = str | list[TextBlock | ImageBlock] | tuple[TextBlock | ImageBlock, ...]


def normalize_prompt(prompt: Prompt) -> str | list[dict[str, Any]]:
    if isinstance(prompt, str):
        return prompt
    if not isinstance(prompt, (list, tuple)) or not prompt:
        raise ConfigurationError("prompt must be text or a nonempty list/tuple of input blocks")
    parts: list[dict[str, Any]] = []
    for block in tuple(prompt):
        if isinstance(block, TextBlock) and isinstance(block.text, str):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            if not isinstance(block.data, bytes) or not 0 < len(block.data) <= MAX_IMAGE_BYTES:
                raise ConfigurationError("ImageBlock requires encoded bytes, at most 7 MiB per image")
            mime = sniff_mime(block.data)
            if not mime:
                raise ConfigurationError("ImageBlock requires PNG, JPEG, GIF or WebP data")
            encoded = base64.b64encode(block.data).decode("ascii")
            # Self-contained history can move with the host's SessionStore. Do
            # not write SDK inputs to the CLI's user-global media cache.
            parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}})
        else:
            raise ConfigurationError("Input blocks must be TextBlock or ImageBlock")
    return parts
