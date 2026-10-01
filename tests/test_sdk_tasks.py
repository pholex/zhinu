"""Dependency scheduling through actual child kernels and durable journal replay."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import subprocess
import os
import json
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))
from xiaoyu_agent_sdk import (AsyncSession, CloseTimeoutError, ConfigurationError, ModelOptions,
    Session, SessionBusyError, SessionOptions, SQLiteSessionStore, Subagent, TaskSpec)
from tests.test_agent_paths import FakeClient, chunk
from tests.test_sdk import call


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.store = SQLiteSessionStore(self.root / "sessions.sqlite")

    def options(self, script=(), **kwargs):
        return SessionOptions(ModelOptions("test", client=FakeClient(list(script))), self.root,
            builtin_tools=(), subagents=(Subagent("research", "Research", "Do the assigned task", ()),),
            session_store=self.store, **kwargs)

    def test_dag_results_history_resume_and_no_repeat_success(self):
        opts = self.options([[chunk("source")], [chunk("synthesis")]])
        with Session(opts) as session:
            handles = session.tasks.submit((TaskSpec("a", "research", "find"),
                TaskSpec("b", "research", "combine", ("a",), parent="a")))
            done = [h.wait(5) for h in handles]
            self.assertEqual([t.state for t in done], ["succeeded", "succeeded"])
            self.assertTrue(any("source" in m.get("content", "") for m in opts.model.client.completions.calls[1]["messages"] if m["role"] == "user"))
            self.assertEqual(done[1].parent_task_id, done[0].task_id)
            self.assertTrue(all(t.child_run_id for t in done))
            key = session.session_id
        resumed_opts = self.options([])
        with Session(resumed_opts, resume_id=key) as resumed:
            self.assertEqual(resumed.tasks.list(), tuple(done))
            for task in done:
                self.assertTrue(resumed.tasks.runs.recall(task.child_run_id).messages)
                with self.assertRaises(ConfigurationError):
                    resumed.tasks.retry(task.task_id)
            self.assertEqual(resumed_opts.model.client.completions.calls, [])

    def test_cycle_and_unknown_agent_fail_before_writing_or_execution(self):
        with Session(self.options()) as session:
            for specs in ((TaskSpec("a", "missing", "x"),),
                (TaskSpec("a", "research", "x", ("b",)), TaskSpec("b", "research", "y", ("a",)))):
                with self.assertRaises(ConfigurationError):
                    session.tasks.submit(specs)
            self.assertEqual(session.tasks.list(), ())
            self.assertEqual(session.options.model.client.completions.calls, [])

    def test_parallel_cancel_and_busy_boundary(self):
        barrier = threading.Barrier(3)
        release = threading.Event()
        def stream():
            barrier.wait(5)
            release.wait(5)
            yield chunk("finished")
        opts = self.options([stream(), stream()], max_parallel_tasks=2)
        session = Session(opts)
        try:
            left, right = session.tasks.submit((TaskSpec("a", "research", "a"), TaskSpec("b", "research", "b")))
            barrier.wait(5)
            with self.assertRaises(SessionBusyError):
                session.run("no concurrent parent accounting")
            self.assertEqual(left.cancel().state, "cancelling")
            self.assertEqual(left.cancel().state, "cancelling")
            release.set()
            self.assertEqual(left.wait(5).state, "cancelled")
            self.assertEqual(right.wait(5).state, "succeeded")
            self.assertEqual(left.cancel().state, "cancelled")
        finally:
            release.set()
            session.close()

    def test_concurrency_bound_and_queued_cancellation(self):
        entered, release = threading.Event(), threading.Event()
        def stream():
            entered.set()
            release.wait(5)
            yield chunk("done")
        with Session(self.options([stream()], max_parallel_tasks=1)) as session:
            handles = session.tasks.submit(tuple(TaskSpec(str(n), "research", str(n)) for n in range(3)))
            self.assertTrue(entered.wait(5))
            self.assertEqual([h.snapshot().state for h in handles], ["running", "queued", "queued"])
            handles[1].cancel()
            handles[2].cancel()
            release.set()
            handles[0].wait(5)
            self.assertEqual(len(session.options.model.client.completions.calls), 1)

    def test_failure_blocks_dependents_explicit_retry_keeps_success(self):
        opts = self.options([[chunk("ok")], [chunk("repaired")], [chunk("joined")]], max_parallel_tasks=1)
        with Session(opts) as session:
            from xiaoyu.agents import DelegationResult
            from xiaoyu_agent_sdk.tasks import execute_delegation
            def delegate(spec, *args, **kwargs):
                if kwargs["task"] == "fail":
                    return DelegationResult(error="private-token")
                return execute_delegation(spec, *args, **kwargs)
            with patch("xiaoyu_agent_sdk.tasks.execute_delegation", side_effect=delegate):
                a, b, c = session.tasks.submit((TaskSpec("a", "research", "ok"), TaskSpec("b", "research", "fail"),
                    TaskSpec("c", "research", "join", ("a", "b"))))
                self.assertEqual(c.wait(5).state, "blocked")
            self.assertEqual(a.snapshot().state, "succeeded")
            self.assertNotIn("private-token", b.snapshot().error)
            with self.assertRaises(ConfigurationError):
                session.tasks.retry(b.task_id)
            session.tasks.retry(b.task_id, allow_uncertain=True).wait(5)
            self.assertEqual(session.tasks.retry(c.task_id).wait(5).state, "succeeded")
            self.assertEqual(a.snapshot().attempt, 1)
            self.assertEqual(len(opts.model.client.completions.calls), 3)

    def test_close_timeout_retains_parent_lock(self):
        entered, release = threading.Event(), threading.Event()
        def stream():
            entered.set()
            release.wait(5)
            yield chunk("done")
        session = Session(self.options([stream()], close_timeout=.02))
        try:
            session.tasks.submit((TaskSpec("a", "research", "wait"),))
            self.assertTrue(entered.wait(5))
            with self.assertRaises(CloseTimeoutError):
                session.close()
            self.assertFalse(session.closed)
            self.assertTrue(session._log.locked)
        finally:
            release.set()
            session.options = replace(session.options, close_timeout=5)
            session.close()

    def test_lost_recovery_does_not_start_and_needs_explicit_uncertain_retry(self):
        from dataclasses import asdict
        from xiaoyu_agent_sdk import TaskSnapshot
        with Session(self.options()) as session:
            key = session.session_id
            session._journal("sdk.tasks", tasks=[asdict(TaskSnapshot("task", key, "run", "a", "research", "wait", state="running"))])
        with Session(self.options([[chunk("retried")]]), resume_id=key) as session:
            self.assertEqual(session.tasks.get("task").state, "lost")
            self.assertEqual(session.options.model.client.completions.calls, [])
            with self.assertRaises(ConfigurationError):
                session.tasks.retry("task")
            self.assertEqual(session.tasks.retry("task", allow_uncertain=True).wait(5).state, "succeeded")

    def test_task_storage_failure_cannot_report_success(self):
        with Session(self.options()) as session:
            with patch.object(session._log.writer, "append", side_effect=OSError("disk full")):
                with self.assertRaises(Exception):
                    session.tasks.submit((TaskSpec("a", "research", "must not run"),))
            self.assertEqual(session.options.model.client.completions.calls, [])

    def test_real_host_exit_retains_success_and_marks_inflight_lost(self):
        code = '''
import os, sys, threading, json
from pathlib import Path
from xiaoyu_agent_sdk import *
from tests.test_agent_paths import FakeClient, chunk
root = Path(sys.argv[1])
entered = threading.Event()
def stream():
    (root / 'effect.txt').write_text('committed', encoding='utf-8')
    entered.set()
    threading.Event().wait(30)
    yield chunk('unreachable')
options = SessionOptions(ModelOptions('test', client=FakeClient([[chunk('saved')], stream()])), root,
    builtin_tools=(), subagents=(Subagent('research', 'Research', 'Do the assigned task', ()),),
    session_store=SQLiteSessionStore(root / 'sessions.sqlite'), max_parallel_tasks=1)
session = Session(options)
a, b = session.tasks.submit((TaskSpec('a', 'research', 'first'), TaskSpec('b', 'research', 'second', ('a',))))
assert a.wait(5).state == 'succeeded'
assert entered.wait(5)
print(json.dumps([session.session_id, a.task_id, b.task_id]), flush=True)
os._exit(23)
'''
        root = Path(__file__).resolve().parents[1]
        env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE_")}
        env["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "packages/xiaoyu-agent-sdk/src")))
        result = subprocess.run([sys.executable, "-c", code, str(self.root)], env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 23, result.stderr)
        key, a, b = json.loads(result.stdout)
        with Session(self.options(), resume_id=key) as session:
            self.assertEqual(session.tasks.get(a).state, "succeeded")
            self.assertEqual(session.tasks.get(a).answer, "saved")
            self.assertEqual(session.tasks.get(b).state, "lost")
            self.assertEqual(session.options.model.client.completions.calls, [])
            self.assertEqual((self.root / "effect.txt").read_text(encoding="utf-8"), "committed")

    def test_reverse_dependency_order_propagates_failure_to_all_descendants(self):
        from xiaoyu.agents import DelegationResult
        with Session(self.options()) as session:
            with patch("xiaoyu_agent_sdk.tasks.execute_delegation", return_value=DelegationResult(error="fail")):
                c, b, a = session.tasks.submit((TaskSpec("c", "research", "c", ("b",)),
                    TaskSpec("b", "research", "b", ("a",)), TaskSpec("a", "research", "a")))
                self.assertEqual([h.wait(5).state for h in (a, b, c)], ["failed", "blocked", "blocked"])

    def test_fork_copies_child_history_without_copying_task_execution(self):
        with Session(self.options([[chunk("saved")]])) as parent:
            task = parent.tasks.submit((TaskSpec("a", "research", "remember"),))[0].wait(5)
            with parent.fork(options=self.options([[chunk("continued")]])) as child:
                self.assertEqual(child.tasks.list(), ())
                handle = child.tasks.submit((TaskSpec("b", "research", "continue", resume_from=task.child_run_id),))[0]
                self.assertEqual(handle.wait(5).state, "succeeded")
                messages = child.options.model.client.completions.calls[-1]["messages"]
                self.assertTrue(any(m.get("content") == "remember" for m in messages))

    def test_model_invoked_child_history_persists(self):
        with Session(self.options([[call("research", '{"task":"find"}')], [chunk("child")], [chunk("parent")]])) as session:
            session.run("delegate")
            key = session.session_id
            runs = list(session.tasks.runs.archives)
            self.assertEqual(len(runs), 1)
        with Session(self.options(), resume_id=key) as session:
            self.assertIsNotNone(session.tasks.runs.recall(runs[0]))

    def test_async_host_can_schedule_and_wait_without_blocking_loop(self):
        async def main():
            async with AsyncSession(self.options([[chunk("async child")]])) as session:
                handles = await session.submit_tasks((TaskSpec("a", "research", "work"),))
                result = await session.wait_task(handles[0].task_id, 5)
                self.assertEqual(result.state, "succeeded")
        asyncio.run(main())

    def test_task_cancel_cancels_its_async_approval_and_waits_for_cleanup(self):
        async def main():
            entered, cleaned = asyncio.Event(), asyncio.Event()
            async def approve(*_):
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    await asyncio.sleep(.01)
                    cleaned.set()
            opts = replace(self.options([[call("write_file", '{"path":"must-not-write","content":"x"}')]]),
                approver=approve, subagents=(Subagent("research", "work", "work", ("write_file",)),))
            async with AsyncSession(opts) as session:
                handle = (await session.submit_tasks((TaskSpec("a", "research", "work"),)))[0]
                await asyncio.wait_for(entered.wait(), 5)
                await session.cancel_task(handle.task_id)
                state = await session.wait_task(handle.task_id, 5)
                self.assertEqual(state.state, "cancelled")
                self.assertTrue(cleaned.is_set())
                self.assertFalse((self.root / "must-not-write").exists())
        asyncio.run(main())
