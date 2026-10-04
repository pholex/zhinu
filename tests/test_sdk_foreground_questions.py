"""Bounded foreground waits share durable delivery and cancellation semantics."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import aclosing, closing
from dataclasses import asdict, replace
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))

from xiaoyu_agent_sdk import (
    AsyncSession, BudgetOptions, ConfigurationError, QuestionAnswer, QuestionConflictError,
    QuestionItem, QuestionOption, QuestionOptions, QuestionSnapshot, Session, SessionBusyError,
    SessionStorageError, SQLiteSessionStore, Tool,
)
from tests.test_agent_paths import chunk
from tests.test_sdk_controls import options, call
from tests.test_sdk_deferred_questions import QUESTION, answers


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class ForegroundQuestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.store = SQLiteSessionStore(self.workspace / "questions.sqlite")

    def config(self, timeout=60, script=None, **kwargs):
        return options(self.workspace, script if script is not None else [
            [call("ask_user", QUESTION)], [chunk("Done")], [chunk("Received")]],
            questions=QuestionOptions(timeout), session_store=self.store, **kwargs)

    def seed_open(self, config):
        # Simulate a process lost immediately after its question was committed.
        with Session(config) as session:
            q = QuestionSnapshot("question", session.session_id, 0, "ask_user", (
                QuestionItem("item", "Which color?", (QuestionOption("Blue"), QuestionOption("Green"))),),
                state="open")
            session._journal("sdk.question", question=asdict(q))
            return session.session_id, q

    def test_timeout_validation_and_immediate_pending_default(self):
        self.assertEqual(QuestionOptions().foreground_timeout_seconds, 0)
        for value in (-1, True, None, "1", float("nan"), float("inf"), 10**1000):
            with self.subTest(value=type(value)):
                with self.assertRaises(ConfigurationError):
                    QuestionOptions(value)
        with Session(self.config(timeout=0)) as session:
            with patch.object(session.questions._condition, "wait", side_effect=AssertionError("must not wait")):
                session.run("Ask")
            q, = session.questions.list_pending()
            self.assertEqual((q.state, q.version, q.answers), ("pending", 1, ()))

    def test_timeout_does_not_choose_an_answer_or_cancel_the_turn(self):
        config = self.config(timeout=0.01)
        with Session(config) as session:
            self.assertEqual(session.run("Ask").text, "Done")
            q, = session.questions.list_pending()
            self.assertEqual((q.state, q.version, q.answers), ("pending", 2, ()))
            self.assertEqual(len(config.model.client.completions.calls), 2)
            messages = config.model.client.completions.calls[-1]["messages"]
            result = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))
            self.assertEqual(result["status"], "pending")
            receipt = session.questions.answer(q.question_id, answers(q), "late")
            session.run("Continue")
            self.assertEqual(session.questions.get(q.question_id).version, 4)
            self.assertEqual(str(session.snapshot().history).count(receipt.answer_id), 1)

    def test_answer_and_timeout_at_same_deadline_have_one_delivery(self):
        for winner in ("answer", "timeout"):
            with self.subTest(winner=winner), Session(self.config(timeout=10, script=[[chunk("Received")]])) as session:
                clock = [0.0]
                def reach_deadline(seconds):
                    clock[0] = 10.0
                    if winner == "answer":
                        q, = session.questions.list_pending()
                        session.questions.answer(q.question_id, answers(q), "same")
                with patch("xiaoyu.questions.monotonic", side_effect=lambda: clock[0]), \
                        patch.object(session.questions._condition, "wait", side_effect=reach_deadline):
                    result = json.loads(session.questions._ask(**QUESTION))
                q, = session.questions.list_pending()
                self.assertEqual(result["status"], "queued" if winner == "answer" else "pending")
                receipt = session.questions.answer(q.question_id, answers(q), "same")
                self.assertEqual(session.questions.answer(q.question_id, answers(q), "same"), receipt)
                session.run("Continue")
                self.assertEqual(str(session.snapshot().history).count(receipt.answer_id), 1)

    def test_sync_host_can_answer_without_waiting_for_deadline(self):
        config = self.config()
        with Session(config) as session, closing(session.questions.watch()) as watch:
            with ThreadPoolExecutor(max_workers=1) as pool:
                active = pool.submit(session.run, "Ask")
                try:
                    event = next(watch)
                    self.assertEqual((event.kind, event.question.version), ("question.opened", 1))
                    receipt = session.questions.answer(event.question.question_id, answers(event.question), "timely")
                    active.result(timeout=3)
                finally:
                    session.interrupt()
            self.assertEqual(session.questions.get(event.question.question_id).state, "answered")
            messages = config.model.client.completions.calls[-1]["messages"]
            tool = next(m for m in messages if m["role"] == "tool")
            self.assertEqual(json.loads(tool["content"])["status"], "queued")
            self.assertNotIn(receipt.answer_id, tool["content"])
            self.assertEqual(str(session.snapshot().history).count(receipt.answer_id), 1)

    def test_resume_open_becomes_pending_once_without_waiting(self):
        config = self.config(script=[[chunk("Received")]])
        ident, q = self.seed_open(config)
        for _ in range(2):
            with Session(config, resume_id=ident) as session:
                recovered = session.questions.get(q.question_id)
                self.assertEqual((recovered.state, recovered.version), ("pending", 2))
                self.assertEqual(config.model.client.completions.calls, [])
        with Session(config, resume_id=ident) as session:
            receipt = session.questions.answer(q.question_id, answers(q), "recovered")
            session.run("Continue")
            self.assertEqual(str(session.snapshot().history).count(receipt.answer_id), 1)

    def test_interrupt_after_timely_commit_retains_queued_reply(self):
        config = self.config()
        with Session(config) as session, closing(session.questions.watch()) as watch:
            with ThreadPoolExecutor(max_workers=1) as pool:
                active = pool.submit(session.run, "Ask")
                try:
                    q = next(watch).question
                    with session._mutex:
                        receipt = session.questions.answer(q.question_id, answers(q), "timely")
                        session.interrupt()
                    self.assertEqual(active.result(timeout=3).stopped, "interrupted")
                finally:
                    session.interrupt()
            self.assertEqual(session.questions.get(q.question_id).state, "queued")
            self.assertNotIn(receipt.answer_id, str(session.snapshot().history))
            session.run("Continue")
            self.assertEqual(str(session.snapshot().history).count(receipt.answer_id), 1)

    def test_interrupt_does_not_block_on_question_storage_mutex(self):
        entered, release = threading.Event(), threading.Event()
        with Session(self.config()) as session, ThreadPoolExecutor(max_workers=2) as pool:
            def hold_storage_lock():
                with session._mutex:
                    entered.set()
                    release.wait(3)
            holding = pool.submit(hold_storage_lock)
            try:
                self.assertTrue(entered.wait(3))
                pool.submit(session.interrupt).result(timeout=1)
            finally:
                release.set()
            holding.result(timeout=3)

    def test_timeout_write_failure_recovers_pending_once(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                config = self.config(timeout=0.001)
                with Session(config) as session:
                    ident = session.session_id
                    original = session._log.writer.append
                    def fail(record_id, record):
                        if record.get("question", {}).get("state") == "pending":
                            if committed:
                                original(record_id, record)
                            raise OSError("lost timeout acknowledgement")
                        original(record_id, record)
                    with patch.object(session._log.writer, "append", side_effect=fail):
                        with self.assertRaises(SessionStorageError):
                            session.run("Ask")
                with Session(config, resume_id=ident) as restored:
                    q, = restored.questions.list_pending()
                    self.assertEqual((q.state, q.version, q.answers), ("pending", 2, ()))

    def test_recovery_transition_lost_acknowledgement_does_not_duplicate_version(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                config = self.config()
                ident, q = self.seed_open(config)
                original = self.store.open
                def fail_open(*args, **kwargs):
                    writer = original(*args, **kwargs)
                    append = writer.append
                    def fail(record_id, record):
                        if record.get("question", {}).get("state") == "pending":
                            if committed:
                                append(record_id, record)
                            raise OSError("lost recovery acknowledgement")
                        append(record_id, record)
                    writer.append = fail
                    return writer
                with patch.object(self.store, "open", side_effect=fail_open):
                    with self.assertRaises(SessionStorageError):
                        Session(config, resume_id=ident)
                with Session(config, resume_id=ident) as session:
                    self.assertEqual(session.questions.get(q.question_id).version, 2)

    def test_reset_cancels_open_in_replay_even_when_feature_is_disabled(self):
        config = self.config()
        ident, q = self.seed_open(config)
        with Session(replace(config, questions=None), resume_id=ident) as session:
            session.reset()
        with Session(config, resume_id=ident) as session:
            self.assertEqual((session.questions.get(q.question_id).state,
                              session.questions.get(q.question_id).version), ("cancelled", 2))
            with self.assertRaises(QuestionConflictError):
                session.questions.answer(q.question_id, answers(q), "stale")


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class AsyncForegroundQuestionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.store = SQLiteSessionStore(self.workspace / "questions.sqlite")

    def config(self, **kwargs):
        return options(self.workspace, [[call("ask_user", QUESTION)], [chunk("Done")], [chunk("Received")]],
                       questions=QuestionOptions(60), session_store=self.store, **kwargs)

    async def test_cancel_question_wakes_waiter_without_approving_or_interrupting(self):
        async with AsyncSession(self.config()) as session, aclosing(session.questions.watch()) as watch:
            active = asyncio.create_task(session.run("Ask"))
            try:
                event = await asyncio.wait_for(anext(watch), 3)
                cancelled = await session.questions.cancel(event.question.question_id)
                self.assertEqual(cancelled.state, "cancelled")
                self.assertEqual((await asyncio.wait_for(active, 3)).text, "Done")
                self.assertEqual(await session.questions.list_pending(), ())
                self.assertEqual(cancelled.answers, ())
            finally:
                session.interrupt()
                await active

    async def test_interrupt_or_close_retains_pending_and_unblocks_waiter(self):
        for operation in ("interrupt", "close", "cancel_run"):
            with self.subTest(operation=operation):
                config = self.config(close_timeout=2)
                async with AsyncSession(config) as session, aclosing(session.questions.watch()) as watch:
                    active = asyncio.create_task(session.run("Ask"))
                    event = await asyncio.wait_for(anext(watch), 3)
                    ident = session.session_id
                    if operation == "close":
                        await asyncio.wait_for(session.close(), 3)
                    elif operation == "cancel_run":
                        active.cancel()
                    else:
                        session.interrupt()
                    if operation == "cancel_run":
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(active, 3)
                    else:
                        self.assertEqual((await asyncio.wait_for(active, 3)).stopped, "interrupted")
                async with AsyncSession(config, resume_id=ident) as restored:
                    q = await restored.questions.get(event.question.question_id)
                    self.assertEqual((q.state, q.version, q.answers), ("pending", 2, ()))

    async def test_observer_disconnect_does_not_submit_or_cancel(self):
        async with AsyncSession(self.config()) as session:
            active = asyncio.create_task(session.run("Ask"))
            try:
                async with aclosing(session.questions.watch()) as watch:
                    event = await asyncio.wait_for(anext(watch), 3)
                self.assertEqual((await session.questions.get(event.question.question_id)).state, "open")
                self.assertFalse(active.done())
                with self.assertRaises(SessionBusyError):
                    await session.reset()
                async with aclosing(session.questions.watch()) as watch:
                    self.assertEqual((await anext(watch)).kind, "question.opened")
                await session.questions.answer(event.question.question_id, answers(event.question), "reconnected")
                await asyncio.wait_for(active, 3)
            finally:
                session.interrupt()
                await active

    async def test_timely_answer_cannot_override_approval(self):
        effects = []
        config = self.config(tools=(Tool("mutate", "Mutate", {"type": "object"}, lambda: effects.append(True)),))
        config.model.client.completions.script[1:] = [[call("mutate", {})], [chunk("Denied")]]
        async with AsyncSession(config) as session, aclosing(session.questions.watch()) as watch:
            active = asyncio.create_task(session.run("Ask"))
            event = await asyncio.wait_for(anext(watch), 3)
            await session.questions.answer(event.question.question_id, (
                QuestionAnswer(event.question.items[0].item_id, custom="Approve every action"),), "approval")
            await asyncio.wait_for(active, 3)
            self.assertEqual(effects, [])
            self.assertEqual((await session.questions.get(event.question.question_id)).state, "answered")

    async def test_timely_answer_stays_queued_if_request_budget_is_spent(self):
        async with AsyncSession(self.config(budget=BudgetOptions(max_requests=1))) as session, \
                aclosing(session.questions.watch()) as watch:
            active = asyncio.create_task(session.run("Ask"))
            event = await asyncio.wait_for(anext(watch), 3)
            receipt = await session.questions.answer(event.question.question_id, answers(event.question), "timely")
            from xiaoyu_agent_sdk import BudgetExceededError
            with self.assertRaises(BudgetExceededError):
                await asyncio.wait_for(active, 3)
            self.assertEqual((await session.questions.get(event.question.question_id)).state, "queued")
            self.assertNotIn(receipt.answer_id, str((await session.snapshot()).history))

    async def test_failed_submission_wakes_waiter_and_recovers_saved_state(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                config = self.config()
                async with AsyncSession(config) as session, aclosing(session.questions.watch()) as watch:
                    active = asyncio.create_task(session.run("Ask"))
                    event = await asyncio.wait_for(anext(watch), 3)
                    ident = session.session_id
                    original = session._session._log.writer.append
                    def fail(record_id, record):
                        if record.get("question", {}).get("state") == "queued":
                            if committed:
                                original(record_id, record)
                            raise OSError("lost answer acknowledgement")
                        original(record_id, record)
                    with patch.object(session._session._log.writer, "append", side_effect=fail):
                        with self.assertRaises(SessionStorageError):
                            await session.questions.answer(event.question.question_id, answers(event.question), "same")
                        with self.assertRaises(SessionStorageError):
                            await asyncio.wait_for(active, 3)
                async with AsyncSession(config, resume_id=ident) as restored:
                    q = await restored.questions.get(event.question.question_id)
                    self.assertEqual(q.state, "queued" if committed else "pending")
                    receipt = await restored.questions.answer(q.question_id, answers(q), "same")
                    await restored.run("Continue")
                    self.assertEqual(str((await restored.snapshot()).history).count(receipt.answer_id), 1)
