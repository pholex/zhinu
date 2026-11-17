"""Idle control operations keep permissions, usage and lifecycle ownership aligned."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))

from xiaoyu_agent_sdk import (
    AsyncSession, BudgetOptions, ConfigurationError, ExecutionError, Hook, HookDecision,
    ModelOptions, Session, SessionBusyError, SessionClosedError, SessionOptions,
    SessionStorageError, SQLiteSessionStore, Subagent, TaskSpec, Tool,
)
from tests.test_agent_paths import FakeClient, call_fragment, chunk, usage_chunk


def options(workspace, script, **kwargs):
    return SessionOptions(ModelOptions("test-model", client=FakeClient(script)), workspace,
                          builtin_tools=kwargs.pop("builtin_tools", ()), **kwargs)


def call(name, arguments):
    return chunk(tool_calls=[call_fragment(0, name, name, json.dumps(arguments))])


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class ControlTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()

    def test_validation_does_not_change_history_or_options(self):
        with self.assertRaises(ConfigurationError):
            Session(options(self.workspace, [], mode="yolo"))
        with Session(options(self.workspace, [])) as session:
            before = session.snapshot()
            for value in ("", "unknown", None):
                with self.assertRaises(ConfigurationError):
                    session.set_mode(value)
            for value in ("", " model ", None, 5):
                with self.assertRaises(ConfigurationError):
                    session.switch_model(value)
            for value in (0, -1, True, 1.5, "100"):
                with self.assertRaises(ConfigurationError):
                    session.set_budget_tokens(value)
            self.assertEqual(session.snapshot(), before)
        for operation in (session.reset, lambda: session.set_mode("auto"),
                          lambda: session.switch_model("next"), lambda: session.set_budget_tokens(None)):
            with self.assertRaises(SessionClosedError):
                operation()

    def test_plan_blocks_tool_and_task_bypasses_then_exit_syncs_options(self):
        effects = []
        config = options(self.workspace, [[call("mutate", {})], [chunk("blocked")],
                         [call("exit_plan_mode", {"plan": "Ready for review"})], [chunk("approved")]],
                         tools=(Tool("mutate", "Mutate", {"type": "object"}, lambda: effects.append(True)),),
                         subagents=(Subagent("worker", "Work", "Work", ()),), approver=lambda *_: True)
        with Session(config) as session:
            session.set_mode("plan")
            self.assertEqual(session.options.mode, "plan")
            with self.assertRaises(ConfigurationError):
                session.tasks.submit((TaskSpec("job", "worker", "work"),))
            with self.assertRaises(ConfigurationError):
                session.tasks.retry("unknown", allow_uncertain=True)
            session.run("do work")
            self.assertEqual(effects, [])
            self.assertEqual(session.snapshot().mode, "plan")
            session.run("present plan")
            self.assertEqual(session.snapshot().mode, "default")
            self.assertEqual(session.options.mode, "default")

    def test_auto_does_not_remove_deny_or_custom_tool_approval(self):
        effects = []
        config = options(self.workspace, [[call("write_file", {"path": "blocked.txt", "content": "x"})],
                         [call("business", {})], [chunk("done")]], builtin_tools=("write_file",),
                         tools=(Tool("business", "Business action", {"type": "object"}, lambda: effects.append(True)),),
                         deny_rules=("deny write_file(*)",))
        with Session(config) as session:
            session.set_mode("auto")
            session.run("work")
        self.assertEqual(effects, [])
        self.assertFalse((self.workspace / "blocked.txt").exists())

    def test_model_switch_preserves_history_client_and_shared_accounting(self):
        client = FakeClient([[chunk("one"), usage_chunk(10, 2)], [chunk("two"), usage_chunk(20, 3)],
                             [chunk("child"), usage_chunk(5, 1)]])
        closed = []
        client.close = lambda: closed.append(True)
        config = replace(options(self.workspace, [], budget=BudgetOptions()),
                         model=ModelOptions("test-model", client=client))
        with Session(config) as session:
            session.run("first")
            session.switch_model("second-model")
            self.assertEqual(session.options.model.model, "second-model")
            self.assertIs(session.options.model.client, client)
            self.assertEqual(session.snapshot().last_assistant_text, "one")
            session.run("next")
            self.assertEqual(client.completions.calls[-1]["model"], "second-model")
            self.assertIn("first", str(client.completions.calls[-1]["messages"]))
            self.assertEqual(session.cost.requests, 2)
            with session.fork() as child:
                self.assertEqual(child.snapshot().usage.prompt_tokens, 30)
                child.run("child")
                self.assertEqual(child.snapshot().model, "second-model")
            self.assertEqual(session.snapshot().usage.prompt_tokens, 30)
        self.assertEqual(closed, [])

    def test_reset_keeps_identity_resources_usage_and_budget_but_clears_context(self):
        config = options(self.workspace, [[chunk("one"), usage_chunk(10, 2)], [chunk("two"), usage_chunk(20, 3)]],
                         budget=BudgetOptions(), budget_tokens=100000)
        with Session(config) as session:
            session.run("old conversation")
            identity = session.session_id
            session.notify("old notification", "key")
            observer = session.watch_notifications()
            next(observer)
            session.set_mode("plan")
            session.reset()
            self.assertEqual(next(observer), ())
            observer.close()
            self.assertEqual(session.snapshot().history, ())
            self.assertEqual(session.snapshot().mode, "default")
            self.assertEqual(session.options.mode, "default")
            self.assertEqual(session.snapshot().usage.prompt_tokens, 10)
            self.assertEqual(session.cost.requests, 1)
            self.assertEqual(session.session_id, identity)
            self.assertEqual(session.checkpoints(), ())
            self.assertEqual(session.snapshot().budget_tokens, 100000)
            session.run("fresh start")
            self.assertNotIn("old conversation", str(config.model.client.completions.calls[-1]["messages"]))
            session.notify("new notification", "key", wake=False)
            self.assertEqual(len(session.pending_notifications()), 1)

    def test_usage_and_reset_restore_in_both_stores_without_double_counting(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite):
                storage = {"session_store": SQLiteSessionStore(self.workspace / "sessions.sqlite")} if sqlite else {"session_dir": self.workspace / "logs"}
                config = options(self.workspace, [[chunk("one"), usage_chunk(6000, 2)],
                                 [chunk("budget wrapup"), usage_chunk(10, 1)],
                                 [chunk("fresh"), usage_chunk(20, 3)]], **storage)
                with Session(config) as session:
                    session.run("old")
                    session.set_budget_tokens(5000)
                    resume = {"resume_id": session.session_id} if sqlite else {"resume_from": session.session_path}
                    active_options = session.options
                with Session(active_options, **resume) as resumed:
                    self.assertEqual(resumed.snapshot().usage.prompt_tokens, 6000)
                    self.assertEqual(resumed.run("continue").stopped, "budget")
                    resumed.set_budget_tokens(None)
                    resumed.reset()
                    resumed.run("new")
                    active_options = resumed.options
                with Session(active_options, **resume) as again:
                    self.assertEqual(again.snapshot().usage.prompt_tokens, 6030)
                    self.assertEqual(again.snapshot().usage.completion_tokens, 6)
                    self.assertNotIn("old", str(again.snapshot().history))

    def test_host_options_override_logged_model_mode_and_budget(self):
        config = options(self.workspace, [], session_dir=self.workspace / "logs")
        with Session(config) as session:
            session.switch_model("previous-model")
            session.set_mode("plan")
            session.set_budget_tokens(10000)
            path = session.session_path
        with Session(config, resume_from=path) as resumed:
            view = resumed.snapshot()
            self.assertEqual(view.mode, "default")
            self.assertEqual(view.model, "test-model")
            self.assertIsNone(view.budget_tokens)
            self.assertIn("关闭", view.history[-1].text)

    def test_plan_files_are_session_owned_and_fork_relocates_instruction(self):
        config = options(self.workspace, [], mode="plan")
        with Session(config) as session:
            path = session._agent.plan_file
            with session.fork() as child:
                child_path = child._agent.plan_file
                self.assertNotEqual(path, child_path)
                self.assertTrue(child_path.is_file())
                self.assertIn(str(child_path), child.snapshot().history[-1].text)
            self.assertFalse(child_path.exists())
        self.assertFalse(path.exists())
        self.assertFalse((self.workspace / ".xiaoyu" / "plan.md").exists())

    def test_child_only_usage_is_checkpointed_and_task_archive_survives_reset(self):
        config = options(self.workspace, [[chunk("child"), usage_chunk(20, 3)]],
                         session_store=SQLiteSessionStore(self.workspace / "child.sqlite"),
                         subagents=(Subagent("worker", "Work", "Work", ()),))
        with Session(config) as session:
            handle = session.tasks.submit((TaskSpec("job", "worker", "work"),))[0]
            self.assertEqual(handle.wait(3).state, "succeeded")
            session.reset()
            self.assertEqual(handle.snapshot().state, "succeeded")
            ident = session.session_id
        with Session(config, resume_id=ident) as recovered:
            self.assertEqual(recovered.snapshot().usage.prompt_tokens, 20)
            self.assertEqual(recovered.tasks.list()[0].state, "succeeded")

    def test_background_ownership_blocks_reset(self):
        with Session(options(self.workspace, [])) as session:
            with patch.object(session._toolbox.tasks, "shutdown_pending", return_value=("active-process",)):
                with self.assertRaises(SessionBusyError):
                    session.reset()

    def test_control_write_failure_blocks_further_execution(self):
        with Session(options(self.workspace, [], session_dir=self.workspace / "logs")) as session:
            with patch.object(session._log, "event", side_effect=OSError("disk failure")):
                with self.assertRaises(ExecutionError):
                    session.set_budget_tokens(10000)
            self.assertEqual(session.status(), "broken")
            with self.assertRaises(ExecutionError):
                session.run("must not run")

    def test_invalid_usage_checkpoint_is_not_silently_treated_as_zero(self):
        store = SQLiteSessionStore(self.workspace / "invalid.sqlite")
        config = options(self.workspace, [], session_store=store)
        with Session(config) as session:
            ident = session.session_id
        writer = store.open(ident, metadata={}, resume=True)
        try:
            writer.append("bad-usage", {"event": "sdk.usage", "by_model": {
                "model": {"calls": 1, "prompt_tokens": -5, "completion_tokens": 1},
            }})
        finally:
            writer.close()
        with self.assertRaises(SessionStorageError):
            Session(config, resume_id=ident)


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class AsyncControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()

    async def test_busy_controls_and_successful_async_operations(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def submitted(_):
            entered.set()
            await release.wait()
            return HookDecision(False)
        config = options(self.workspace, [[chunk("done")]], hooks=(Hook("UserPromptSubmit", submitted),))
        async with AsyncSession(config) as session:
            active = asyncio.create_task(session.run("hello"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                for operation in (session.reset, lambda: session.set_mode("plan"),
                                  lambda: session.switch_model("new"), lambda: session.set_budget_tokens(5000)):
                    with self.assertRaises(SessionBusyError):
                        await operation()
            finally:
                release.set()
            await active
            await session.set_mode("auto")
            await session.switch_model("new")
            await session.set_budget_tokens(50000)
            await session.reset()
            view = await session.snapshot()
            self.assertEqual((view.mode, view.model, view.budget_tokens), ("auto", "new", 50000))
            self.assertEqual(view.history, ())

    async def test_cancellation_does_not_claim_a_control_was_rolled_back(self):
        entered, release = threading.Event(), threading.Event()
        async with AsyncSession(options(self.workspace, [])) as session:
            original = session._session._journal
            def journal(*args, **kwargs):
                entered.set()
                release.wait(5)
                return original(*args, **kwargs)
            with patch.object(session._session, "_journal", side_effect=journal):
                task = asyncio.create_task(session.set_budget_tokens(10000))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    task.cancel()
                    with self.assertRaises(SessionBusyError):
                        await session.switch_model("other")
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertEqual((await session.snapshot()).budget_tokens, 10000)
            self.assertEqual(session.status(), "idle")
