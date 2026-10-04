"""Public host inputs, delivery boundaries and self-contained recovery."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))

from xiaoyu_agent_sdk import (
    AsyncSession, ConfigurationError, Hook, HookDecision, ImageBlock, ModelOptions,
    Prompt, Session, SessionBusyError, SessionClosedError, SessionOptions,
    SQLiteSessionStore, SteerAccepted, TextBlock, Tool, run, run_async,
)
from tests.test_agent_paths import FakeClient, call_fragment, chunk


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=")


def options(workspace, script, **kwargs):
    return SessionOptions(ModelOptions("test-model", client=FakeClient(script)), workspace,
                          builtin_tools=(), **kwargs)


def user_parts(config):
    return [m["content"] for m in config.model.client.completions.calls[-1]["messages"]
            if m["role"] == "user" and isinstance(m["content"], list)]


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class InputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()

    def test_typed_blocks_are_copied_before_hook_and_do_not_use_cli_cache(self):
        prompt: Prompt = [TextBlock("Describe this"), ImageBlock(PNG)]
        seen = []
        def submitted(payload):
            seen.append(payload["prompt"])
            prompt.clear()
            return HookDecision(False)
        config = options(self.workspace, [[chunk("done")]], hooks=(Hook("UserPromptSubmit", submitted),))
        with patch("xiaoyu.media.store", side_effect=AssertionError("must not use global cache")):
            result = run(prompt, config)
        self.assertEqual(result.text, "done")
        parts = user_parts(config)[0]
        self.assertEqual(parts[0], {"type": "text", "text": "Describe this"})
        self.assertEqual(parts[1]["image_url"]["url"], "data:image/png;base64," + base64.b64encode(PNG).decode("ascii"))
        self.assertEqual(seen, ["Describe this[图片]"])
        self.assertEqual(prompt, [])

    def test_invalid_input_never_starts_session_hooks_or_model(self):
        seen = []
        config = options(self.workspace, [[chunk("done")]],
                         hooks=(Hook("SessionStart", lambda _: seen.append(True) or HookDecision(False)),))
        with Session(config) as session:
            invalid = (None, {}, [], (), ["text"], [{"type": "text", "text": "raw"}],
                       [TextBlock(5)], [ImageBlock(b"")], [ImageBlock(b"not an image")],
                       [ImageBlock(bytearray(PNG))], [ImageBlock(PNG + b"x" * (7 * 1024 * 1024))])
            for prompt in invalid:
                with self.subTest(prompt_type=type(prompt)), self.assertRaises(ConfigurationError):
                    session.run(prompt)
            self.assertEqual(seen, [])
            self.assertEqual(config.model.client.completions.calls, [])
            self.assertEqual(session.run("still usable").text, "done")

    def test_image_only_stream_and_text_only_tuple(self):
        config = options(self.workspace, [[chunk("image")], [chunk("text")]])
        with Session(config) as session:
            events = list(session.stream((ImageBlock(PNG),)))
            self.assertEqual(events[-1].result.text, "image")
            self.assertEqual(session.run((TextBlock("hello"),)).text, "text")

    def test_image_history_survives_fork_and_both_storage_backends(self):
        for sqlite in (False, True):
            with self.subTest(sqlite=sqlite):
                store = SQLiteSessionStore(self.workspace / "sessions.sqlite") if sqlite else None
                config = options(self.workspace, [[chunk("one")], [chunk("fork")], [chunk("resumed")]],
                                 session_store=store, session_dir=None if sqlite else self.workspace / "logs")
                with Session(config) as session:
                    session.run([TextBlock("see"), ImageBlock(PNG)])
                    ident, path = session.session_id, session.session_path
                    with session.fork() as child:
                        child.run("continue")
                        self.assertIn("data:image/png;base64,", user_parts(config)[0][1]["image_url"]["url"])
                resume = {"resume_id": ident} if sqlite else {"resume_from": path}
                with Session(config, **resume) as recovered:
                    recovered.run("continue")
                self.assertEqual(user_parts(config)[0][0]["text"], "see")
                self.assertEqual(base64.b64decode(user_parts(config)[0][1]["image_url"]["url"].split(",")[1]), PNG)

    def test_steer_during_tool_is_user_input_and_acknowledged_before_next_request(self):
        decisions = []
        def work():
            decisions.append(session.steer("  use Chinese  "))
            with self.assertRaises(SessionBusyError):
                session.drain_steers()
            return "worked"
        config = options(self.workspace, [
            [chunk(tool_calls=[call_fragment(0, "work", "work", "{}")])], [chunk("done")],
        ], tools=(Tool("work", "Work", {"type": "object"}, work, requires_approval=False),))
        with Session(config) as session:
            self.assertFalse(session.steer("idle"))
            events = list(session.stream("start"))
            self.assertEqual(session.drain_steers(), [])
        accepted = next(e for e in events if isinstance(e, SteerAccepted))
        self.assertEqual(decisions, [True])
        self.assertEqual(accepted.text, "use Chinese")
        self.assertEqual(accepted.session_id, session.session_id)
        self.assertTrue(accepted.run_id)
        requests = [i for i, e in enumerate(events) if e.kind == "request.started"]
        self.assertLess(events.index(accepted), requests[1])
        self.assertTrue(any(m["role"] == "user" and "use Chinese" in str(m["content"])
                            for m in config.model.client.completions.calls[-1]["messages"]))

    def test_late_steer_is_recoverable_and_never_leaks_to_next_turn_or_fork(self):
        submitted = []
        def stopped(_):
            if not submitted:
                submitted.append(session.steer("late preference"))
            return HookDecision(False)
        config = options(self.workspace, [[chunk("done")], [chunk("child")], [chunk("next")]],
                         hooks=(Hook("Stop", stopped),))
        with Session(config) as session:
            events = list(session.stream("start"))
            self.assertFalse(any(isinstance(e, SteerAccepted) for e in events))
            with session.fork(options=replace(config, hooks=())) as child:
                child.run("child")
                self.assertEqual(child.drain_steers(), [])
            session.run("next")
            self.assertNotIn("late preference", str(config.model.client.completions.calls[-1]["messages"]))
        self.assertEqual(submitted, [True])
        self.assertEqual(session.drain_steers(), ["late preference"])
        self.assertEqual(session.drain_steers(), [])
        with self.assertRaises(SessionClosedError):
            session.steer("closed")

    def test_blocked_prompt_preserves_queued_steer_without_model_request(self):
        def submitted(_):
            self.assertTrue(session.steer("keep this"))
            return HookDecision(True, "blocked")
        config = options(self.workspace, [], hooks=(Hook("UserPromptSubmit", submitted),))
        with Session(config) as session:
            session.run("start")
            self.assertEqual(session.drain_steers(), ["keep this"])
        self.assertEqual(config.model.client.completions.calls, [])

    def test_startup_steer_is_rejected_before_kernel_clears_its_queue(self):
        offered = []
        def started(_):
            offered.append(session.steer("too early"))
            return HookDecision(False)
        config = options(self.workspace, [[chunk("done")]], hooks=(Hook("SessionStart", started),))
        with Session(config) as session:
            session.run("start")
            self.assertEqual(session.drain_steers(), [])
        self.assertEqual(offered, [False])
        self.assertNotIn("too early", str(config.model.client.completions.calls))

    def test_accepted_steer_survives_resume_once_as_user_history(self):
        def submitted(_):
            self.assertTrue(session.steer("persist this preference"))
            return HookDecision(False)
        config = options(self.workspace, [[chunk("first")], [chunk("adjusted")], [chunk("resumed")]],
                         session_dir=self.workspace / "logs", hooks=(Hook("UserPromptSubmit", submitted),))
        with Session(config) as session:
            session.run("start")
            path = session.session_path
        recovered_config = replace(config, hooks=())
        with Session(recovered_config, resume_from=path) as recovered:
            recovered.run("continue")
            self.assertEqual(recovered.drain_steers(), [])
        messages = config.model.client.completions.calls[-1]["messages"]
        delivered = [m for m in messages if m["role"] == "user" and "persist this preference" in str(m["content"])]
        self.assertEqual(len(delivered), 1)

    def test_cancelled_turn_preserves_pending_input_and_rejects_further_steer(self):
        def submitted(_):
            self.assertFalse(session.steer("  "))
            with self.assertRaises(ConfigurationError):
                session.steer([TextBlock("invalid")])
            self.assertTrue(session.steer("recover me"))
            session.interrupt()
            self.assertFalse(session.steer("after cancel"))
            return HookDecision(False)
        config = options(self.workspace, [], hooks=(Hook("UserPromptSubmit", submitted),))
        with Session(config) as session:
            self.assertTrue(session.run("start").interrupted)
            self.assertEqual(session.drain_steers(), ["recover me"])


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class AsyncInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()

    async def test_multimodal_one_shot_and_stream(self):
        config = options(self.workspace, [[chunk("once")], [chunk("stream")]])
        prompt: Prompt = (TextBlock("see"), ImageBlock(PNG))
        self.assertEqual((await run_async(prompt, config)).text, "once")
        async with AsyncSession(config) as session:
            events = [e async for e in session.stream(prompt)]
            self.assertEqual(events[-1].result.text, "stream")
        self.assertEqual(user_parts(config)[0][0]["text"], "see")

    async def test_host_can_steer_while_async_tool_waits(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def work():
            entered.set()
            await release.wait()
            return "worked"
        config = options(self.workspace, [
            [chunk(tool_calls=[call_fragment(0, "work", "work", "{}")])], [chunk("done")],
        ], tools=(Tool("work", "Work", {"type": "object"}, work, requires_approval=False),))
        async with AsyncSession(config) as session:
            events = []
            async def consume():
                events.extend([e async for e in session.stream("start")])
            task = asyncio.create_task(consume())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                self.assertTrue(session.steer("new constraint"))
            finally:
                release.set()
            await asyncio.wait_for(task, 3)
            self.assertEqual(session.drain_steers(), [])
        self.assertTrue(any(isinstance(e, SteerAccepted) and e.text == "new constraint" for e in events))
