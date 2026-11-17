"""同步流的请求级时限与资源收尾；原始事件和归一后的 chunk 共用一只钟。"""

from __future__ import annotations

import math
import time
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from typing import Any, Callable, Iterator

from .errors import Interrupted, StreamLifetimeExceeded

_limit: ContextVar[tuple[float, float] | None] = ContextVar("stream_limit", default=None)


@contextmanager
def stream_scope(seconds: float):
    value = (time.monotonic() + seconds, seconds) if seconds > 0 and math.isfinite(seconds) else None
    token = _limit.set(value)
    try:
        yield
    finally:
        _limit.reset(token)


def check_deadline() -> None:
    limit = _limit.get()
    if limit is not None and time.monotonic() >= limit[0]:
        raise StreamLifetimeExceeded(
            f"流式响应总时长达到 {limit[1]:g}s（XIAOYU_STREAM_MAX_LIFETIME）；"
            "已停止本轮，可调大该值或设为 0 关闭后重试"
        )


class _Stream(Iterator[Any]):
    """显式持有资源，尚未开始迭代时 close 也能关掉已经打开的 HTTP 流。"""

    def __init__(self, source: Any, cleanup: Callable[[], None] | None = None):
        self.source = source
        self.iterator: Iterator[Any] | None = None
        self.cleanup = cleanup
        self.closed = False

    def __next__(self) -> Any:
        if self.closed:
            raise StopIteration
        try:
            check_deadline()
            if self.iterator is None:
                self.iterator = iter(self.source)
            try:
                item = next(self.iterator)
            except StopIteration:
                check_deadline()
                raise
            check_deadline()
            return item
        except BaseException as exc:
            try:
                # 阻塞读直到 socket 超时才返回时，总时限仍应阻止自动重试。
                if isinstance(exc, Exception) and not isinstance(exc, (Interrupted, StopIteration, StreamLifetimeExceeded)):
                    check_deadline()
            finally:
                self.close()
            raise

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        close = getattr(self.source, "close", None)
        if callable(close):
            with suppress(Exception):
                close()
        if self.cleanup is not None:
            self.cleanup()


def iter_stream(source: Any) -> _Stream:
    """每个事件前后检查总时限；阻塞 next 仍由传输层的读超时打断。"""
    return _Stream(source)


def translate_stream(source: Any, convert: Any) -> _Stream:
    events = iter_stream(source)
    try:
        return _Stream(convert(events), events.close)
    except BaseException:
        events.close()
        raise
