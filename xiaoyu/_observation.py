"""Bounded signals for host state observers."""
from __future__ import annotations
import asyncio
import queue
import threading


class _Subscription:
    def __init__(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self.queue: queue.Queue[bool] = queue.Queue(maxsize=1)
        self.loop = loop
        self.ready = asyncio.Event() if loop is not None else None

    def signal(self, *, closed: bool = False) -> None:
        # Called under the hub lock. A slow observer retains one wakeup only;
        # the actual pending notifications remain owned by the kernel.
        if closed:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.queue.put_nowait(not closed)
        except queue.Full:
            return
        if self.loop is not None and self.ready is not None:
            try:
                self.loop.call_soon_threadsafe(self.ready.set)
            except RuntimeError:
                pass  # The host has already shut down this event loop.

    async def wait(self) -> bool:
        assert self.ready is not None
        while True:
            await self.ready.wait()
            self.ready.clear()
            try:
                return self.queue.get_nowait()
            except queue.Empty:
                pass  # A previously scheduled wakeup arrived after consumption.


class _Notifications:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._subscriptions: set[_Subscription] = set()

    def subscribe(self, loop: asyncio.AbstractEventLoop | None = None) -> _Subscription:
        subscription = _Subscription(loop)
        with self._lock:
            if not self._closed:
                self._subscriptions.add(subscription)
            subscription.signal(closed=self._closed)
        return subscription

    def unsubscribe(self, subscription: _Subscription) -> None:
        with self._lock:
            self._subscriptions.discard(subscription)

    def changed(self) -> None:
        with self._lock:
            for subscription in self._subscriptions:
                subscription.signal()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for subscription in self._subscriptions:
                subscription.signal(closed=True)
            self._subscriptions.clear()
