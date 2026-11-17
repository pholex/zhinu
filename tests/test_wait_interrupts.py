"""两处长等待要停得下来：task_output 等后台任务、chenshu_wait 等成员事件。

宿主的 interrupt() 只是置个标志，叫不醒阻塞着的 Event.wait / Queue.get——
这两处各自最长能等十分钟，serve 的 abort、ACP 的 cancel、预算硬闸都得等它到点。
"""

from __future__ import annotations

import tempfile
import threading
import time
import types
import unittest
from pathlib import Path

from xiaoyu.chenshu import ChenshuRuntime
from xiaoyu.config import Config
from xiaoyu.errors import Interrupted
from xiaoyu.render import PlainSink
from xiaoyu.tools import Toolbox


class Flag:
    """过一会儿变成 True 的"被打断了吗"。"""

    def __init__(self, after: float) -> None:
        self.at = time.monotonic() + after

    def __call__(self) -> bool:
        return time.monotonic() >= self.at


class WaitCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(
            base_url="x", model="m", workspace=Path(self.tmp.name).resolve(),
            enable_plugins=False, enable_mcp=False,
        )


class TaskOutputWaitTest(WaitCase):
    def task(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(done=threading.Event())

    def test_interrupt_breaks_the_wait(self) -> None:
        box = Toolbox(self.config)
        box.stop_requested = Flag(0.3)
        started = time.monotonic()
        with self.assertRaises(Interrupted):
            box._wait_task(self.task(), time.monotonic() + 30)  # noqa: SLF001
        self.assertLess(time.monotonic() - started, 3)

    def test_finishing_task_ends_the_wait_at_once(self) -> None:
        box = Toolbox(self.config)
        box.stop_requested = lambda: False
        task = self.task()
        threading.Timer(0.2, task.done.set).start()
        started = time.monotonic()
        box._wait_task(task, time.monotonic() + 30)  # noqa: SLF001
        self.assertLess(time.monotonic() - started, 3)
        self.assertTrue(task.done.is_set())

    def test_deadline_still_bounds_the_wait(self) -> None:
        for stop in (None, lambda: False):
            box = Toolbox(self.config)
            box.stop_requested = stop
            started = time.monotonic()
            box._wait_task(self.task(), time.monotonic() + 0.4)  # noqa: SLF001
            #  下限留余量：Windows 的定时器粒度会让等待早醒不到一毫秒
            self.assertGreaterEqual(time.monotonic() - started, 0.35)
            self.assertLess(time.monotonic() - started, 3)


class ChenshuWaitTest(WaitCase):
    def runtime(self, stop) -> ChenshuRuntime:
        runtime = ChenshuRuntime(
            self.config, None, None, PlainSink(), None, stop_requested=stop
        )
        runtime.active = True
        return runtime

    def test_interrupt_breaks_the_wait_and_keeps_later_events(self) -> None:
        runtime = self.runtime(Flag(0.3))
        started = time.monotonic()
        with self.assertRaises(Interrupted):
            runtime.wait(30)
        self.assertLess(time.monotonic() - started, 3)
        #  打断的是"等"，不是成员：之后到的事件下次照常取到
        runtime.stop_requested = lambda: False
        runtime.events.put("w1 完成")
        self.assertIn("w1 完成", runtime.wait(1))

    def test_event_arriving_during_the_wait_is_returned(self) -> None:
        runtime = self.runtime(lambda: False)
        threading.Timer(0.2, runtime.events.put, args=("w2 完成",)).start()
        started = time.monotonic()
        self.assertIn("w2 完成", runtime.wait(30))
        self.assertLess(time.monotonic() - started, 3)

    def test_quiet_wait_reports_no_events_after_the_timeout(self) -> None:
        for stop in (None, lambda: False):
            self.assertIn("没有新事件", self.runtime(stop).wait(1))


if __name__ == "__main__":
    unittest.main()
