"""Durable question state, transactional acceptance and recovery windows."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError, replace
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
    AsyncSession, BudgetOptions, BudgetExceededError, ConfigurationError, Hook, HookDecision,
    QuestionAnswer, QuestionConflictError, QuestionNotFoundError, QuestionOptions,
    Session, SessionClosedError, SessionOptions, SessionStorageError, SQLiteSessionStore, Tool,
)
from tests.test_sdk_controls import options, call
from tests.test_agent_paths import chunk, call_fragment


QUESTION = {"questions": [{"question": "Which color?", "options": ["Blue", "Green"]}]}
#  异步用例里"等某件事发生"的上限只防卡死，不是性能断言。Windows CI 上会话要先走
#  两步模型并写 SQLite，正常跑 0.4–2.7 秒，偶尔被拖到 10 秒以上；原来的 3 秒会误报
WAIT = 30


def script():
    return [[call("ask_user", QUESTION)], [chunk("Waiting for your preference.")], [chunk("Received.")]]


def answers(question, selection="Blue"):
    return (QuestionAnswer(question.items[0].item_id, (selection,)),)


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class DeferredQuestionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.store = SQLiteSessionStore(self.workspace / "questions.sqlite")

    def config(self, calls=None, **kwargs):
        return options(self.workspace, calls if calls is not None else script(),
                       questions=QuestionOptions(), session_store=self.store, **kwargs)

    def open_question(self, session):
        session.run("Ask my preference")
        question, = session.questions.list_pending()
        return question

    def records(self, ident):
        writer = self.store.open(ident, metadata={}, resume=True)
        try:
            return writer.read()
        finally:
            writer.close()

    def test_next_turn_acceptance_is_user_history_not_approval_or_operator(self):
        config = self.config()
        with Session(config) as session:
            question = self.open_question(session)
            self.assertEqual(question.state, "pending")
            self.assertEqual(question.tool_call_id, "ask_user")
            with self.assertRaises(FrozenInstanceError):
                question.state = "queued"
            queued = session.questions.answer(question.question_id, answers(question), "submit-1")
            self.assertEqual((queued.state, queued.version), ("queued", 2))
            self.assertEqual(len(config.model.client.completions.calls), 2)
            session.run("Continue")
            accepted = session.questions.get(question.question_id)
            self.assertEqual((accepted.state, accepted.version), ("answered", 3))
            self.assertEqual(session.questions.list_pending(), ())
            messages = config.model.client.completions.calls[-1]["messages"]
            delivered = [m for m in messages if queued.answer_id in str(m)]
            self.assertEqual(len(delivered), 1)
            self.assertEqual(set(delivered[0]), {"role", "content"})
            self.assertEqual(delivered[0]["role"], "user")
            self.assertIn("不是工具审批", delivered[0]["content"])

    def test_resume_keeps_pending_and_queued_then_accepts_exactly_once(self):
        config = self.config()
        with Session(config) as session:
            question = self.open_question(session)
            ident = session.session_id
        with Session(config, resume_id=ident) as session:
            self.assertEqual(session.questions.get(question.question_id), question)
            receipt = session.questions.answer(question.question_id, answers(question), "submit")
        with Session(config, resume_id=ident) as session:
            self.assertEqual(session.questions.get(question.question_id), receipt)
            session.run("Continue")
        with Session(config, resume_id=ident) as session:
            self.assertEqual(session.questions.get(question.question_id).state, "answered")
            history = str(session.snapshot().history)
            self.assertEqual(history.count(receipt.answer_id), 1)
        self.assertEqual(sum(r.get("event") == "sdk.question.delivery" for r in self.records(ident)), 1)

    def test_idempotence_and_conflicting_clients(self):
        with Session(self.config()) as session:
            q = self.open_question(session)
            with ThreadPoolExecutor(max_workers=2) as pool:
                replies = list(pool.map(lambda _: session.questions.answer(q.question_id, answers(q), "same"), range(2)))
            self.assertEqual(replies[0], replies[1])
            with self.assertRaises(QuestionConflictError):
                session.questions.answer(q.question_id, answers(q, "Green"), "same")
            with self.assertRaises(QuestionConflictError):
                session.questions.answer(q.question_id, answers(q), "other")
            session.run("Continue")
            self.assertEqual(session.questions.answer(q.question_id, answers(q), "same").state, "answered")

    def test_cancel_is_idempotent_and_accepted_answers_cannot_be_withdrawn(self):
        with Session(self.config()) as session:
            q = self.open_question(session)
            session.questions.answer(q.question_id, answers(q), "submit")
            cancelled = session.questions.cancel(q.question_id)
            self.assertEqual(session.questions.cancel(q.question_id), cancelled)
            with self.assertRaises(QuestionConflictError):
                session.questions.answer(q.question_id, answers(q), "submit")
            session.run("Continue")
            self.assertEqual(session.questions.get(q.question_id).state, "cancelled")
        with Session(self.config()) as session:
            q = self.open_question(session)
            session.questions.answer(q.question_id, answers(q), "submit")
            session.run("Continue")
            with self.assertRaises(QuestionConflictError):
                session.questions.cancel(q.question_id)

    def test_reset_invalidates_old_answers_and_fork_has_no_answerable_questions(self):
        config = self.config()
        with Session(config) as session:
            q = self.open_question(session)
            session.questions.answer(q.question_id, answers(q), "submit")
            with session.fork() as child:
                self.assertEqual(child.questions.list_pending(), ())
                with self.assertRaises(QuestionNotFoundError):
                    child.questions.answer(q.question_id, answers(q), "submit")
            session.reset()
            ident = session.session_id
            with self.assertRaises(QuestionConflictError):
                session.questions.answer(q.question_id, answers(q), "submit")
        with Session(config, resume_id=ident) as session:
            self.assertEqual(session.questions.get(q.question_id).state, "cancelled")
            self.assertEqual(session.snapshot().history, ())
            with self.assertRaises(QuestionConflictError):
                session.questions.answer(q.question_id, answers(q), "old")

    def test_validation_requires_complete_and_explicit_answers(self):
        with Session(self.config()) as session:
            q = self.open_question(session)
            item = q.items[0].item_id
            for invalid in ((), None, (QuestionAnswer("unknown", ("Blue",)),),
                            (QuestionAnswer(item, ("Bad",)),), (QuestionAnswer(item, ("Blue", "Green")),),
                            (QuestionAnswer(item),), (QuestionAnswer(item, ("Blue",), skipped=True),),
                            (QuestionAnswer(item, custom="x" * 4001),)):
                with self.subTest(invalid=type(invalid)):
                    with self.assertRaises(ConfigurationError):
                        session.questions.answer(q.question_id, invalid, "submit")
            self.assertEqual(session.questions.get(q.question_id), q)
            receipt = session.questions.answer(q.question_id, (QuestionAnswer(item, skipped=True),), "skip")
            self.assertTrue(receipt.answers[0].skipped)

    def test_multi_select_answer_order_is_canonical(self):
        payload = {"questions": [{**QUESTION["questions"][0], "multi_select": True}]}
        with Session(self.config([[call("ask_user", payload)], [chunk("pending")]])) as session:
            q = self.open_question(session)
            one = session.questions.answer(q.question_id, (QuestionAnswer(q.items[0].item_id, ("Green", "Blue")),), "same")
            two = session.questions.answer(q.question_id, (QuestionAnswer(q.items[0].item_id, ("Blue", "Green")),), "same")
            self.assertEqual(one, two)

    def test_storage_write_windows_recover_without_duplicate_delivery(self):
        for stage in ("queued", "answered"):
            for committed in (False, True):
                with self.subTest(stage=stage, committed=committed):
                    config = self.config()
                    with Session(config) as session:
                        q = self.open_question(session)
                        ident = session.session_id
                        if stage == "answered":
                            session.questions.answer(q.question_id, answers(q), "submit")
                        original = session._log.writer.append
                        def fail(record_id, record):
                            if record.get("question", {}).get("state") == stage:
                                if committed:
                                    original(record_id, record)
                                raise OSError("simulated lost write acknowledgement")
                            original(record_id, record)
                        with patch.object(session._log.writer, "append", side_effect=fail):
                            with self.assertRaises(SessionStorageError):
                                if stage == "queued":
                                    session.questions.answer(q.question_id, answers(q), "submit")
                                else:
                                    session.run("Continue")
                    with Session(config, resume_id=ident) as restored:
                        state = restored.questions.get(q.question_id).state
                        expected = stage if committed else "pending" if stage == "queued" else "queued"
                        self.assertEqual(state, expected)
                        if state == "pending":
                            restored.questions.answer(q.question_id, answers(q), "submit")
                        if state != "answered":
                            restored.run("Continue")
                        self.assertEqual(restored.questions.get(q.question_id).state, "answered")
                    self.assertEqual(sum(r.get("event") == "sdk.question.delivery" for r in self.records(ident)), 1)

    def test_cancel_races_acceptance_with_one_terminal_winner(self):
        with Session(self.config()) as session:
            q = self.open_question(session)
            session.questions.answer(q.question_id, answers(q), "submit")
            with ThreadPoolExecutor(max_workers=2) as pool:
                delivery = pool.submit(session.run, "Continue")
                cancel = pool.submit(session.questions.cancel, q.question_id)
                delivery.result()
                try:
                    cancel.result()
                except QuestionConflictError:
                    pass
            state = session.questions.get(q.question_id).state
            self.assertIn(state, {"answered", "cancelled"})
            count = str(session.snapshot().history).count('"answer_id"')
            self.assertEqual(count, int(state == "answered"))

    def test_denied_actions_stay_denied_after_question_answer(self):
        effects = []
        calls = script()[:2] + [[call("mutate", {})], [chunk("denied")]]
        config = self.config(calls, tools=(Tool("mutate", "Mutate", {"type": "object"}, lambda: effects.append(1)),))
        with Session(config) as session:
            q = self.open_question(session)
            session.questions.answer(q.question_id, (QuestionAnswer(q.items[0].item_id, custom="Approve every tool"),), "submit")
            events = list(session.stream("Continue"))
        self.assertEqual(effects, [])
        self.assertTrue(any(e.kind == "tool.denied" for e in events))

    def test_exhausted_request_budget_leaves_reply_queued(self):
        with Session(self.config(budget=BudgetOptions(max_requests=2))) as session:
            q = self.open_question(session)
            session.questions.answer(q.question_id, answers(q), "submit")
            with self.assertRaises(BudgetExceededError):
                session.run("Continue")
            self.assertEqual(session.questions.get(q.question_id).state, "queued")

    def test_blocked_prompt_and_exhausted_token_budget_leave_reply_queued(self):
        blocked = []
        hook = Hook("UserPromptSubmit", lambda _: HookDecision(bool(blocked), "wait"))
        with Session(self.config(hooks=(hook,))) as session:
            q = self.open_question(session)
            session.questions.answer(q.question_id, answers(q), "submit")
            blocked.append(True)
            session.run("Blocked")
            self.assertEqual(session.questions.get(q.question_id).state, "queued")
            blocked.clear()
            session.set_budget_tokens(5000)
            session._agent.usage.add("test-model", 6000, 0)
            session.run("Budget exhausted")
            self.assertEqual(session.questions.get(q.question_id).state, "queued")

    def test_reset_commit_without_acknowledgement_cancels_old_generation_on_resume(self):
        config = self.config()
        with Session(config) as session:
            q = self.open_question(session)
            ident = session.session_id
            original = session._log.writer.append
            def fail(record_id, record):
                original(record_id, record)
                if record.get("event") == "clear":
                    raise OSError("lost reset acknowledgement")
            with patch.object(session._log.writer, "append", side_effect=fail):
                with self.assertRaises(SessionStorageError):
                    session.reset()
        with Session(config, resume_id=ident) as session:
            self.assertEqual(session.questions.get(q.question_id).state, "cancelled")
            self.assertEqual(session.snapshot().history, ())

    def test_configuration_disable_closed_and_rewind_boundaries(self):
        for changes in ({"session_store": None}, {"asker": lambda _: {}}, {"questions": True},
                        {"tools": (Tool("ask_user", "Custom", {"type": "object"}, lambda: "x"),)}):
            with self.assertRaises(ConfigurationError):
                Session(replace(self.config(), **changes))
        config = self.config()
        with Session(config) as session:
            q = self.open_question(session)
            ident = session.session_id
            with self.assertRaises(ConfigurationError):
                session.rewind(session.checkpoints()[0], files=False)
        with self.assertRaises(SessionClosedError):
            session.questions.list_pending()
        with Session(replace(config, questions=None), resume_id=ident) as disabled:
            with self.assertRaises(ConfigurationError):
                _ = disabled.questions
            disabled.reset()
        with Session(config, resume_id=ident) as restored:
            self.assertEqual(restored.questions.get(q.question_id).state, "cancelled")

    def test_watch_versions_coalescing_and_terminal_reconnect(self):
        config = self.config()
        with Session(config) as session:
            q = self.open_question(session)
            with closing(session.questions.watch()) as watch:
                first = next(watch)
                self.assertEqual((first.kind, first.question), ("question.pending", q))
                receipt = session.questions.answer(q.question_id, answers(q), "submit")
                self.assertEqual(next(watch).question, receipt)
                session.run("Continue")
                final = next(watch)
                self.assertEqual((final.kind, final.question.version), ("question.answered", 3))
                self.assertEqual(first.question.state, "pending")
                self.assertEqual(final.question.tool_call_id, "ask_user")
                with self.assertRaises(FrozenInstanceError):
                    final.kind = "question.pending"
            ident = session.session_id
        with Session(config, resume_id=ident) as session:
            with closing(session.questions.watch()) as watch:
                self.assertEqual(next(watch), final)
        with Session(self.config()) as session:
            q = self.open_question(session)
            with closing(session.questions.watch()) as watch:
                next(watch)
                session.questions.answer(q.question_id, answers(q), "submit")
                session.questions.cancel(q.question_id)
                # A slow observer receives the latest committed version only.
                event = next(watch)
                self.assertEqual((event.kind, event.question.version), ("question.cancelled", 3))

    def test_watch_reset_and_multiple_subscribers(self):
        with Session(self.config()) as session:
            q = self.open_question(session)
            with closing(session.questions.watch()) as first, closing(session.questions.watch()) as second:
                self.assertEqual(next(first), next(second))
                session.questions.answer(q.question_id, answers(q), "submit")
                next(first)
                session.reset()
                one, two = next(first), next(second)
                self.assertEqual(one, two)
                self.assertEqual((one.kind, one.question.version), ("question.cancelled", 3))

    def test_watch_observes_committed_reset_before_control_cleanup_finishes(self):
        entered, release = threading.Event(), threading.Event()
        with Session(self.config()) as session:
            self.open_question(session)
            with closing(session.questions.watch()) as watch:
                next(watch)
                original = session._save_usage
                def wait():
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("reset was not released")
                    original()
                with patch.object(session, "_save_usage", side_effect=wait):
                    with ThreadPoolExecutor(max_workers=1) as pool:
                        active = pool.submit(session.reset)
                        try:
                            self.assertTrue(entered.wait(3))
                            self.assertEqual(next(watch).kind, "question.cancelled")
                        finally:
                            release.set()
                        active.result(timeout=3)

    def test_watch_close_unblocks_empty_session_and_after_close_ends(self):
        session = Session(self.config([]))
        watch = session.questions.watch()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(next, watch, "ended")
            try:
                session.close()
                self.assertEqual(pending.result(timeout=3), "ended")
            finally:
                session.close()
                watch.close()
        self.assertEqual(list(session.questions.watch()), [])

    def test_failed_submission_does_not_publish_uncommitted_state(self):
        with Session(self.config()) as session:
            q = self.open_question(session)
            with closing(session.questions.watch()) as watch:
                next(watch)
                subscription, = session.questions._changes._subscriptions
                with patch.object(session._log.writer, "append", side_effect=OSError("fail before commit")):
                    with self.assertRaises(SessionStorageError):
                        session.questions.answer(q.question_id, answers(q), "submit")
                self.assertTrue(subscription.queue.empty())
                self.assertEqual(session.questions._items[q.question_id], q)

    def test_final_text_boundary_accepts_and_continues_same_run(self):
        def final_text():
            q, = session.questions.list_pending()
            session.questions.answer(q.question_id, answers(q), "during-response")
            yield chunk("Independent work complete.")
        config = self.config([[call("ask_user", QUESTION)], final_text(), [chunk("Answer received.")]])
        with Session(config) as session:
            result = session.run("Ask and work")
            self.assertEqual(result.text, "Answer received.")
            q = next(iter(session.questions._items.values()))
            self.assertEqual(q.state, "answered")
            self.assertEqual(len(config.model.client.completions.calls), 3)
            self.assertEqual(str(config.model.client.completions.calls[-1]["messages"]).count(q.answer_id), 1)

    def test_answer_waits_for_all_tool_results_in_batch(self):
        def submit():
            q, = session.questions.list_pending()
            session.questions.answer(q.question_id, answers(q), "during-tool")
            self.assertEqual(session.questions.get(q.question_id).state, "queued")
            return "Independent work complete"
        batch = chunk(tool_calls=[call_fragment(0, "ask", "ask_user", json.dumps(QUESTION)),
                                  call_fragment(1, "submit", "independent", "{}")])
        config = self.config([[batch], [chunk("Answer received.")]], tools=(
            Tool("independent", "Work", {"type": "object"}, submit, requires_approval=False),))
        with Session(config) as session:
            session.run("Ask and work")
            q = next(iter(session.questions._items.values()))
            messages = config.model.client.completions.calls[-1]["messages"]
            delivery_index = next(i for i, m in enumerate(messages) if q.answer_id in str(m))
            results = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
            self.assertEqual(len(results), 2)
            self.assertGreater(delivery_index, max(results))

    def test_runtime_delivery_write_failure_recovers_once(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                def final_text():
                    q, = session.questions.list_pending()
                    session.questions.answer(q.question_id, answers(q), "during-response")
                    yield chunk("Independent work done.")
                config = self.config([[call("ask_user", QUESTION)], final_text(), [chunk("Received")]])
                with Session(config) as session:
                    ident = session.session_id
                    original = session._log.writer.append
                    def fail(record_id, record):
                        if record.get("event") == "sdk.question.delivery":
                            if committed:
                                original(record_id, record)
                            raise OSError("lost delivery acknowledgement")
                        original(record_id, record)
                    with patch.object(session._log.writer, "append", side_effect=fail):
                        with self.assertRaises(SessionStorageError):
                            session.run("Ask and work")
                with Session(config, resume_id=ident) as restored:
                    q = next(iter(restored.questions._items.values()))
                    self.assertEqual(q.state, "answered" if committed else "queued")
                    restored.run("Continue")
                    self.assertEqual(str(restored.snapshot().history).count(q.answer_id), 1)
                self.assertEqual(sum(r.get("event") == "sdk.question.delivery" for r in self.records(ident)), 1)

    def test_runtime_budget_and_interrupt_leave_answer_queued(self):
        for stop in ("budget", "interrupt"):
            with self.subTest(stop=stop):
                def final_text():
                    q, = session.questions.list_pending()
                    session.questions.answer(q.question_id, answers(q), "during-response")
                    if stop == "interrupt":
                        session.interrupt()
                    yield chunk("Done")
                config = self.config([[call("ask_user", QUESTION)], final_text()],
                                     budget=BudgetOptions(max_requests=2))
                with Session(config) as session:
                    session.run("Ask and work")
                    q, = session.questions.list_pending()
                    self.assertEqual(q.state, "queued")
                    self.assertNotIn(q.answer_id, str(session.snapshot().history))
                    self.assertEqual(len(config.model.client.completions.calls), 2)

    def test_corrupt_question_state_rejects_resume(self):
        config = self.config()
        with Session(config) as session:
            q = self.open_question(session)
            ident = session.session_id
        writer = self.store.open(ident, metadata={}, resume=True)
        try:
            record = next(r for r in writer.read() if r.get("event") == "sdk.question")
            record["question"]["version"] = 99
            writer.append("corrupt", record)
        finally:
            writer.close()
        with self.assertRaises(SessionStorageError):
            Session(config, resume_id=ident)


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "Install the sdk extra")
class AsyncDeferredQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_during_turn_is_delivered_at_next_step(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
            return "Independent work done"
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            config = options(workspace, [[call("ask_user", QUESTION)], [call("independent", {})],
                             [chunk("done")], [chunk("received")]], questions=QuestionOptions(),
                             session_store=SQLiteSessionStore(workspace / "questions.sqlite"),
                             tools=(Tool("independent", "Wait", {"type": "object"}, wait, requires_approval=False),))
            async with AsyncSession(config) as session:
                active = asyncio.create_task(session.run("Ask and work"))
                try:
                    await asyncio.wait_for(entered.wait(), WAIT)
                    q, = await session.questions.list_pending()
                    receipt = await session.questions.answer(q.question_id, answers(q), "submit")
                finally:
                    release.set()
                await active
                self.assertEqual((await session.questions.get(q.question_id)).state, "answered")
                self.assertIn(receipt.answer_id, str(config.model.client.completions.calls[-1]["messages"]))
                self.assertEqual(len(config.model.client.completions.calls), 3)

    async def test_async_watch_can_submit_while_run_is_active_and_closes(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def wait():
            entered.set()
            await release.wait()
            return "Done"
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            config = options(workspace, [[call("ask_user", QUESTION)], [call("independent", {})],
                             [chunk("Received")]], questions=QuestionOptions(),
                             session_store=SQLiteSessionStore(workspace / "questions.sqlite"),
                             tools=(Tool("independent", "Wait", {"type": "object"}, wait, requires_approval=False),))
            async with AsyncSession(config) as session:
                watch = session.questions.watch()
                pending_event = asyncio.create_task(anext(watch))
                active = asyncio.create_task(session.run("Ask and work"))
                try:
                    event = await asyncio.wait_for(pending_event, WAIT)
                    self.assertEqual(event.kind, "question.pending")
                    await asyncio.wait_for(entered.wait(), WAIT)
                    receipt = await session.questions.answer(event.question.question_id, answers(event.question), "submit")
                    self.assertEqual((await asyncio.wait_for(anext(watch), WAIT)).question, receipt)
                finally:
                    release.set()
                await active
                accepted = await asyncio.wait_for(anext(watch), WAIT)
                self.assertEqual((accepted.kind, accepted.question.version), ("question.answered", 3))
                closing_event = asyncio.create_task(anext(watch, None))
            self.assertIsNone(await asyncio.wait_for(closing_event, WAIT))
            await watch.aclose()

    async def test_async_watch_cancellation_removes_subscription(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary).resolve()
            config = options(workspace, [], questions=QuestionOptions(),
                             session_store=SQLiteSessionStore(workspace / "questions.sqlite"))
            async with AsyncSession(config) as session:
                watch = session.questions.watch()
                pending = asyncio.create_task(anext(watch))
                await asyncio.sleep(0)
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await pending
                self.assertEqual(session.questions._manager._changes._subscriptions, set())
                await watch.aclose()
