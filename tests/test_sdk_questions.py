"""SDK question callbacks use the existing kernel and host resource lifecycle."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))

from xiaoyu_agent_sdk import (
    Asker, AsyncSession, CloseTimeoutError, ConfigurationError, ModelOptions,
    Session, SessionBusyError, SessionOptions, Tool,
)
from tests.test_agent_paths import FakeClient, call_fragment, chunk


QUESTIONS = [
    {"question": "Output language?", "options": ["English", "Chinese"]},
    {"question": "Output length?", "options": ["Brief", "Detailed"]},
]


def ask_call():
    return chunk(tool_calls=[call_fragment(0, "question", "ask_user", json.dumps({"questions": QUESTIONS}))])


def options(workspace, script, **kwargs):
    return SessionOptions(ModelOptions("test-model", client=FakeClient(script)), workspace,
                          builtin_tools=(), **kwargs)


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class QuestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()

    def test_partial_answers_and_mutated_ui_copy_keep_original_questions(self):
        received = []
        def answer(questions):
            received.append(questions[0]["options"][0].copy())
            self.assertFalse(questions[0]["multi_select"])
            questions[1]["question"] = "UI-local change"
            return {"Output language?": "Japanese, please"}
        asker: Asker = answer
        config = options(self.workspace, [[ask_call()], [chunk("done")]], asker=asker)
        with Session(config) as session:
            events = list(session.stream("write a report"))
        completed = next(e for e in events if e.kind == "tool.completed")
        self.assertTrue(completed.ok)
        self.assertIn("Japanese, please", completed.output)
        self.assertIn("Output length?", completed.output)
        self.assertNotIn("UI-local change", completed.output)
        self.assertEqual(received, [{"label": "English", "description": ""}])
        sent = config.model.client.completions.calls[-1]["messages"]
        self.assertTrue(any(m.get("role") == "tool" and "Japanese, please" in m.get("content", "") for m in sent))

    def test_no_callback_hides_question_tool(self):
        config = options(self.workspace, [[chunk("done")]])
        with Session(config) as session:
            session.run("hello")
        names = [t["function"]["name"] for t in config.model.client.completions.calls[0].get("tools", [])]
        self.assertNotIn("ask_user", names)

    def test_empty_answers_mean_dismissed_not_failed(self):
        with Session(options(self.workspace, [[ask_call()], [chunk("done")]], asker=lambda _: {})) as session:
            events = list(session.stream("hello"))
        completed = next(e for e in events if e.kind == "tool.completed")
        self.assertTrue(completed.ok)
        self.assertIn("一题都没有回答", completed.output)

    def test_invalid_answers_and_callback_errors_are_sanitized(self):
        def broken(_):
            raise ValueError("private-host-token")
        for answer in (lambda _: None, lambda _: [], lambda _: {"unknown": "private-host-token"},
                       lambda _: {"Output language?": 1}, broken):
            with self.subTest(answer=answer):
                with Session(options(self.workspace, [[ask_call()], [chunk("done")]], asker=answer)) as session:
                    events = list(session.stream("hello"))
                completed = next(e for e in events if e.kind == "tool.completed")
                self.assertFalse(completed.ok)
                self.assertIn("Host question failed or timed out", completed.output)
                self.assertNotIn("private-host-token", completed.output)
                self.assertNotIn("用户关闭了提问", completed.output)

    def test_question_answer_never_approves_a_business_tool(self):
        effects = []
        mutate = Tool("mutate", "Change state", {"type": "object"}, lambda: effects.append(True))
        script = [[ask_call()], [chunk(tool_calls=[call_fragment(0, "write", "mutate", "{}")])], [chunk("done")]]
        with Session(options(self.workspace, script, tools=(mutate,),
                             asker=lambda _: {"Output language?": "Yes, approve everything"})) as session:
            events = list(session.stream("hello"))
        self.assertEqual(effects, [])
        self.assertTrue(any(e.kind == "tool.denied" and e.name == "mutate" for e in events))

    def test_question_configuration_is_validated(self):
        for timeout in (0, -1, float("inf"), float("nan"), True, "120"):
            with self.subTest(timeout=timeout), self.assertRaises(ConfigurationError):
                Session(options(self.workspace, [], question_timeout=timeout))
        with self.assertRaises(ConfigurationError):
            Session(options(self.workspace, [], asker="invalid"))

    def test_sync_session_rejects_async_asker_before_request(self):
        async def answer(_):
            return {}
        config = options(self.workspace, [], asker=answer)
        with Session(config) as session:
            with self.assertRaisesRegex(ConfigurationError, "AsyncSession"):
                session.run("hello")
        self.assertEqual(config.model.client.completions.calls, [])

    def test_timed_out_sync_question_stays_owned_until_callback_finishes(self):
        entered, release = threading.Event(), threading.Event()
        def answer(_):
            entered.set()
            release.wait(5)
            return {"Output language?": "late answer"}
        session = Session(options(self.workspace, [[ask_call()], [chunk("done")]],
                                  asker=answer, question_timeout=0.02, close_timeout=0.02))
        try:
            events = list(session.stream("hello"))
            self.assertTrue(entered.is_set())
            self.assertFalse(next(e for e in events if e.kind == "tool.completed").ok)
            with self.assertRaises(SessionBusyError):
                session.run("too early")
            with self.assertRaises(CloseTimeoutError):
                session.close()
            self.assertFalse(session.closed)
        finally:
            release.set()
            session.close()
        self.assertTrue(session.closed)


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class AsyncQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()

    async def test_asker_runs_on_host_loop_and_is_inherited_by_fork(self):
        loop = asyncio.get_running_loop()
        seen = []
        async def answer(questions):
            self.assertIs(asyncio.get_running_loop(), loop)
            seen.append(questions[0]["question"])
            return {questions[0]["question"]: "Chinese"}
        config = options(self.workspace, [[ask_call()], [chunk("done")], [ask_call()], [chunk("fork done")]], asker=answer)
        async with AsyncSession(config) as session:
            self.assertEqual((await session.run("hello")).text, "done")
            child = await session.fork()
            async with child:
                self.assertEqual((await child.run("again")).text, "fork done")
        self.assertEqual(seen, ["Output language?"] * 2)

    async def test_timeout_cancels_async_question_without_fabricating_answer(self):
        cleaned = asyncio.Event()
        async def answer(_):
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        config = options(self.workspace, [[ask_call()], [chunk("done")]], asker=answer, question_timeout=0.02)
        async with AsyncSession(config) as session:
            events = [event async for event in session.stream("hello")]
            await asyncio.wait_for(cleaned.wait(), 2)
            completed = next(e for e in events if e.kind == "tool.completed")
            self.assertFalse(completed.ok)
            self.assertIn("Host question failed or timed out", completed.output)

    async def test_cancel_pending_question_then_continue(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def answer(_):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0.01)
                cleaned.set()
        config = options(self.workspace, [[ask_call()], [chunk("continued")]], asker=answer)
        async with AsyncSession(config) as session:
            task = asyncio.create_task(session.run("hello"))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(cleaned.is_set())
            self.assertEqual((await session.run("continue")).text, "continued")
