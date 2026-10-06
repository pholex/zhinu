"""AsyncSession.open：异步创建会话，不堵宿主的事件循环。

构造函数在调用线程上同步建会话（开存储、起 worker），在事件循环里直接调就把
循环停住这么久——Windows CI 慢盘上实测接近 1 秒。open 把这段搬到线程里，并且
在创建途中被取消时，不留下没人关的会话与存储锁。
"""

from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu_agent_sdk import AsyncSession, CloseTimeoutError, ConfigurationError, run_async
from xiaoyu_agent_sdk import session as session_module

from tests.test_agent_paths import chunk
from tests.test_sdk_controls import options

WAIT = 30


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class AsyncOpenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()

    async def test_opened_session_runs_like_a_constructed_one(self):
        async with await AsyncSession.open(options(self.workspace, [[chunk("hello")]])) as session:
            result = await session.run("Hi")
            self.assertEqual(result.text, "hello")
        self.assertTrue(session.closed)

    async def test_setup_runs_off_the_event_loop_thread(self):
        loop_thread = threading.get_ident()
        seen: list[int] = []
        original = session_module.Session.__init__

        def recording_init(self, *args, **kwargs):
            seen.append(threading.get_ident())
            original(self, *args, **kwargs)

        with mock.patch.object(session_module.Session, "__init__", recording_init):
            session = await AsyncSession.open(options(self.workspace, []))
        await session.close()
        self.assertEqual(len(seen), 1)
        self.assertNotEqual(seen[0], loop_thread)

    async def test_event_loop_keeps_ticking_during_slow_setup(self):
        release = threading.Event()
        original = session_module.Session.__init__

        def slow_init(self, *args, **kwargs):
            release.wait(WAIT)
            original(self, *args, **kwargs)

        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                if ticks == 5:
                    release.set()
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(ticker())
        try:
            with mock.patch.object(session_module.Session, "__init__", slow_init):
                session = await asyncio.wait_for(AsyncSession.open(options(self.workspace, [])), WAIT)
        finally:
            beat.cancel()
        #  setup 只在事件循环跑够 5 拍之后才放行：循环被堵住的话这里会等满 WAIT 后超时
        self.assertGreaterEqual(ticks, 5)
        await session.close()

    async def test_setup_errors_propagate_unchanged(self):
        with self.assertRaises(ConfigurationError):
            await AsyncSession.open(options(self.workspace, [], mode="yolo"))

    async def test_binds_to_the_opening_loop(self):
        session = await AsyncSession.open(options(self.workspace, []))
        self.addAsyncCleanup(session.close)
        self.assertIs(session._session._loop, asyncio.get_running_loop())

    async def test_cancel_during_setup_closes_what_setup_produced(self):
        started, release = threading.Event(), threading.Event()
        closed = threading.Event()
        original_init = session_module.Session.__init__
        original_close = session_module.Session.close

        def blocked_init(self, *args, **kwargs):
            started.set()
            release.wait(WAIT)
            original_init(self, *args, **kwargs)

        def recording_close(self, *args, **kwargs):
            try:
                return original_close(self, *args, **kwargs)
            finally:
                closed.set()

        with mock.patch.object(session_module.Session, "__init__", blocked_init), \
                mock.patch.object(session_module.Session, "close", recording_close):
            opening = asyncio.create_task(AsyncSession.open(options(self.workspace, [])))
            await asyncio.to_thread(started.wait, WAIT)
            opening.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await opening
            #  取消立即生效：此时 setup 还卡着，什么都还没建出来
            self.assertFalse(closed.is_set())
            release.set()
            self.assertTrue(await asyncio.to_thread(closed.wait, WAIT), "取消后建出来的会话没有被关闭")

    async def test_cancel_after_failed_setup_closes_nothing(self):
        started, release, settled = threading.Event(), threading.Event(), threading.Event()
        original_callback = session_module._close_abandoned_setup

        def failing_init(self, *args, **kwargs):
            started.set()
            release.wait(WAIT)
            raise ConfigurationError("boom")

        def recording_callback(setup):
            try:
                original_callback(setup)
            finally:
                settled.set()

        with mock.patch.object(session_module.Session, "__init__", failing_init), \
                mock.patch.object(session_module.Session, "close") as close, \
                mock.patch.object(session_module, "_close_abandoned_setup", recording_callback):
            opening = asyncio.create_task(AsyncSession.open(options(self.workspace, [])))
            await asyncio.to_thread(started.wait, WAIT)
            opening.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await opening
            release.set()
            #  等收尾回调确实跑过，而不是睡一个拍脑袋的时长再做否定断言
            self.assertTrue(await asyncio.to_thread(settled.wait, WAIT), "setup 失败后收尾回调没有被触发")
            close.assert_not_called()

    async def test_close_failure_after_cancel_is_logged_not_raised(self):
        started, release, closed = threading.Event(), threading.Event(), threading.Event()
        original_init = session_module.Session.__init__

        def blocked_init(self, *args, **kwargs):
            started.set()
            release.wait(WAIT)
            original_init(self, *args, **kwargs)

        def failing_close(self, *args, **kwargs):
            closed.set()
            raise CloseTimeoutError("still settling")

        with mock.patch.object(session_module.Session, "__init__", blocked_init), \
                mock.patch.object(session_module.Session, "close", failing_close), \
                self.assertLogs(session_module._log, "ERROR") as logs:
            opening = asyncio.create_task(AsyncSession.open(options(self.workspace, [])))
            await asyncio.to_thread(started.wait, WAIT)
            opening.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await opening
            release.set()
            self.assertTrue(await asyncio.to_thread(closed.wait, WAIT))
            for thread in threading.enumerate():
                if thread.name == "xiaoyu-sdk-abandoned-open":
                    await asyncio.to_thread(thread.join, WAIT)
        self.assertEqual(len(logs.records), 1)
        self.assertIsInstance(logs.records[0].exc_info[1], CloseTimeoutError)

    async def test_run_async_goes_through_open(self):
        with mock.patch.object(AsyncSession, "open", wraps=AsyncSession.open) as opened:
            result = await run_async("Hi", options(self.workspace, [[chunk("one-shot")]]))
        self.assertEqual(result.text, "one-shot")
        opened.assert_called_once()


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class OpenAcrossLoopShutdownTests(unittest.TestCase):
    """宿主取消 open 之后事件循环就收摊了（asyncio.run 退出会取消所有 Task）：
    setup 线程仍在建会话，建出来的那个照样要被关掉。"""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()

    def test_session_built_after_loop_shutdown_is_still_closed(self):
        started, release, closed = threading.Event(), threading.Event(), threading.Event()
        original_init = session_module.Session.__init__
        original_close = session_module.Session.close

        def blocked_init(self, *args, **kwargs):
            started.set()
            release.wait(WAIT)
            original_init(self, *args, **kwargs)

        def recording_close(self, *args, **kwargs):
            try:
                return original_close(self, *args, **kwargs)
            finally:
                closed.set()

        async def host():
            opening = asyncio.create_task(AsyncSession.open(options(self.workspace, [])))
            await asyncio.to_thread(started.wait, WAIT)
            opening.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await opening
            #  宿主就此返回，asyncio.run 收尾时 setup 还卡在 __init__ 里

        with mock.patch.object(session_module.Session, "__init__", blocked_init), \
                mock.patch.object(session_module.Session, "close", recording_close):
            asyncio.run(host())
            release.set()
            self.assertTrue(closed.wait(WAIT), "事件循环关闭后建出来的会话没有被关闭")
            for thread in threading.enumerate():
                if thread.name == "xiaoyu-sdk-abandoned-open":
                    thread.join(WAIT)


if __name__ == "__main__":
    unittest.main()
