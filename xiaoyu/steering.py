"""插话的接纳、撤回和修改共享同一把锁，消费后不能再声称撤回成功。"""
from __future__ import annotations

import threading
import uuid


class SteerQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, str] = {}

    def put(self, text: str) -> str:
        if not text.strip():
            return ""
        with self._lock:
            ident = uuid.uuid4().hex
            self._items[ident] = text.strip()
            return ident

    def pending(self) -> tuple[tuple[str, str], ...]:
        with self._lock:
            return tuple(self._items.items())

    def empty(self) -> bool:
        with self._lock:
            return not self._items

    def take_all(self) -> list[str]:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
            return items

    def retract(self, ident: str) -> str | None:
        with self._lock:
            return self._items.pop(ident, None)

    def replace(self, ident: str, text: str) -> bool:
        if not text.strip():
            raise ValueError("插话不能为空")
        with self._lock:
            if ident not in self._items:
                return False
            self._items[ident] = text.strip()
            return True
