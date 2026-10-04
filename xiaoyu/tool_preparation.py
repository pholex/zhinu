"""工具参数生成时的有界预览；不参与参数校验或执行。"""

from __future__ import annotations

import json
import time

from .events import ToolPreparing


class Preparation:
    """逐字符扫描顶层字符串，只保留路径和目的，不复制文件正文。

    字符串闭合后才解码，跨片转义与 Unicode 代理对不会被截成错误预览。
    超过 2048 个原始字符的字段不展示；完整参数仍由内核照常收集。
    """

    _FIELDS = {"path", "__tool_use_purpose"}

    def __init__(self, index: int) -> None:
        self.index = index
        self.chars = 0
        self.fields: dict[str, str] = {}
        self._depth = 0
        self._string = False
        self._escape = False
        self._key = ""
        self._expect_key = True
        self._is_key = False
        self._buffer: list[str] | None = None
        self._closed = False
        self._last: tuple | None = None
        self._at = 0.0

    def feed(self, fragment: str) -> None:
        self.chars += len(fragment)
        for char in fragment:
            if self._closed:
                break
            if self._string:
                if self._buffer is not None:
                    self._buffer.append(char)
                    if len(self._buffer) > 2048:
                        self._buffer = None
                if self._escape:
                    self._escape = False
                elif char == "\\":
                    self._escape = True
                elif char == '"':
                    self._string = False
                    value = None
                    if self._buffer is not None:
                        try:
                            value = json.loads(''.join(self._buffer))
                        except ValueError:
                            pass
                    if self._is_key:
                        self._key = value if isinstance(value, str) else ""
                        self._expect_key = False
                    elif self._depth == 1 and self._key in self._FIELDS and isinstance(value, str):
                        # 孤立代理码点不能直接写进 UTF-8 终端或 JSONL。
                        self.fields[self._key] = value.encode("utf-8", "replace").decode("utf-8")
                    self._buffer = None
                continue
            if char == '"':
                self._string = True
                self._is_key = self._depth == 1 and self._expect_key
                capture = self._is_key or (self._depth == 1 and self._key in self._FIELDS)
                self._buffer = ['"'] if capture else None
            elif char in "{[":
                self._depth += 1
            elif char in "}]":
                self._depth -= 1
                if self._depth <= 0:
                    self._closed = True
            elif char == "," and self._depth == 1:
                self._expect_key = True
                self._key = ""

    def event(self, name: str, call_id: str, *, force: bool = False) -> ToolPreparing | None:
        if not name:
            return None
        path = self.fields.get("path", "")
        purpose = self.fields.get("__tool_use_purpose", "")
        state = (name, call_id, path, purpose, self.chars)
        if state == self._last:
            return None
        now = time.monotonic()
        # 元信息到达立即展示；只有字符数变化时最多每秒十次，收尾补最后一次。
        if not force and self._last is not None and state[:4] == self._last[:4] and now - self._at < 0.1:
            return None
        self._last, self._at = state, now
        return ToolPreparing(name, self.index, self.chars, path, purpose, tool_call_id=call_id)
