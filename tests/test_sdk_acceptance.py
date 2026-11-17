"""业务验收与执行成功分开，回调异常和重启不会伪报通过。"""
import asyncio
from dataclasses import replace
import threading
import unittest

from . import test_sdk_tasks
from .test_agent_paths import chunk
from xiaoyu_agent_sdk import (AsyncSession, Session, TaskSpec, TaskAcceptance,
                             ConfigurationError, SessionBusyError, CloseTimeoutError)


class AcceptanceTests(unittest.TestCase):
    setUp = test_sdk_tasks.TaskTests.setUp
    options = test_sdk_tasks.TaskTests.options

    def test_independent_verdict_and_resume_without_evaluator(self):
        opts = self.options([[chunk("代码已写完")]])
        with Session(opts) as session:
            task = session.tasks.submit((TaskSpec("build", "research", "build"),))[0].wait(5)
            result = session.tasks.evaluate(task.task_id, lambda t: TaskAcceptance("failed", "测试尚未通过"))
            self.assertEqual(result.state, "succeeded")
            self.assertEqual(result.acceptance, "failed")
            self.assertEqual(len(opts.model.client.completions.calls), 1)
            key = session.session_id
        with Session(self.options(), resume_id=key) as resumed:
            self.assertEqual(resumed.tasks.get(task.task_id), result)
            approved = resumed.tasks.evaluate(task.task_id, lambda t: TaskAcceptance("passed", "修复后的测试通过"))
            self.assertEqual(approved.acceptance, "passed")

    def test_invalid_and_raising_callbacks_are_unknown(self):
        with Session(self.options([[chunk("ok")]])) as session:
            task = session.tasks.submit((TaskSpec("a", "research", "do"),))[0].wait(5)
            def failing(t):
                raise RuntimeError("private detail")
            for evaluator in (lambda t: True, failing, lambda t: TaskAcceptance("passed", "")):
                result = session.tasks.evaluate(task.task_id, evaluator)
                self.assertEqual(result.acceptance, "unknown")
                self.assertNotIn("private detail", result.acceptance_evidence)
            for timeout in (0, -1, float("nan"), True):
                with self.assertRaises(ConfigurationError):
                    session.tasks.evaluate(task.task_id, failing, timeout=timeout)

    def test_callback_owns_resources_until_it_stops(self):
        entered, release = threading.Event(), threading.Event()
        session = Session(self.options([[chunk("ok")]], close_timeout=.02))
        task = session.tasks.submit((TaskSpec("a", "research", "do"),))[0].wait(5)
        results = []
        def evaluate(t):
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test barrier")
            return TaskAcceptance("passed", "checked")
        worker = threading.Thread(target=lambda: results.append(session.tasks.evaluate(task.task_id, evaluate)))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            with self.assertRaises(SessionBusyError):
                session.run("busy")
            with self.assertRaises(CloseTimeoutError):
                session.close()
        finally:
            release.set()
            worker.join(5)
            # The tiny timeout tests ownership, not the platform's cleanup speed.
            session.options = replace(session.options, close_timeout=5)
            session.close()
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(results), 1)

    def test_async_evaluator(self):
        async def run():
            async with AsyncSession(self.options([[chunk("ok")]])) as session:
                handle = (await session.submit_tasks((TaskSpec("a", "research", "do"),)))[0]
                task = await session.wait_task(handle.task_id, 5)
                async def check(t):
                    return TaskAcceptance("passed", "verified asynchronously")
                result = await session.evaluate_task(task.task_id, check)
                self.assertEqual(result.acceptance, "passed")
        asyncio.run(run())

    def test_pending_acceptance_restores_unknown(self):
        with Session(self.options([[chunk("ok")]])) as session:
            task = session.tasks.submit((TaskSpec("a", "research", "do"),))[0].wait(5)
            from dataclasses import asdict, replace
            session._journal("sdk.tasks", tasks=[asdict(replace(task, acceptance="pending"))])
            key = session.session_id
        with Session(self.options(), resume_id=key) as resumed:
            result = resumed.tasks.get(task.task_id)
            self.assertEqual(result.state, "succeeded")
            self.assertEqual(result.acceptance, "unknown")
