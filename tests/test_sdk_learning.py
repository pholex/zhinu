import asyncio
from dataclasses import replace
from pathlib import Path
import threading
import unittest

from . import test_sdk_tasks
from .test_agent_paths import chunk
from xiaoyu_agent_sdk import (Session, AsyncSession, MemoryStore, MemoryScope, MemoryCandidate,
                             ConfigurationError, SessionBusyError, CloseTimeoutError)


class LearningTests(unittest.TestCase):
    setUp = test_sdk_tasks.TaskTests.setUp
    options = test_sdk_tasks.TaskTests.options

    def memory(self):
        return MemoryStore(Path(self.tmp.name) / "memory.sqlite"), MemoryScope("alice", "repo")

    def extract(self, span):
        m = next(m for m in span.messages if "unittest" in m.text and m.role == "user")
        return (MemoryCandidate("runner", "Use unittest", "lesson", m.source_ref, "unittest"),)

    def test_explicit_extraction_receipt_review_and_next_session_context(self):
        store, scope = self.memory()
        opts = self.options([[chunk("understood")]])
        with Session(opts) as session:
            session.run("Use unittest for project tests")
            values = session.extract_memory(store, scope, self.extract)
            session.extract_memory(store, scope, lambda span: self.fail("receipt must avoid another extraction"))
            self.assertEqual(len(store.list(scope)), 1)
            self.assertEqual(session.recall_memory(store, scope, "tests"), "")
            key = session.session_id
        store.accept(scope, values[0].id, reviewer="alice")
        opts = replace(self.options([[chunk("using remembered rule")]]), memory_store=store, memory_scope=scope)
        with Session(opts) as fresh:
            fresh.run("Which runner should I use?")
            self.assertIn("Use unittest", str(opts.model.client.completions.calls[0]["messages"]))
            injected = [m for m in fresh._agent.messages if "历史记忆" in str(m.get("content"))]
            self.assertTrue(injected)
            self.assertTrue(all(m["role"] != "system" for m in injected))
        with Session(self.options(), resume_id=key) as resumed:
            self.assertEqual(resumed.extract_memory(store, scope, lambda span: self.fail("replayed"))[0].id, values[0].id)

    def test_bad_evidence_and_failed_callback_do_not_mark_source_complete(self):
        store, scope = self.memory()
        with Session(self.options([[chunk("ok")]])) as session:
            session.run("Use unittest")
            def bad(span): return (MemoryCandidate("key", "guess", "fact", span.messages[0].source_ref, "never said"),)
            with self.assertRaises(ConfigurationError): session.extract_memory(store, scope, bad)
            self.assertEqual(store.list(scope), ())
            self.assertEqual(len(session.extract_memory(store, scope, self.extract)), 1)
            with self.assertRaises(ConfigurationError): session.extract_memory(store, scope, self.extract, start=999)

    def test_sync_callback_timeout_keeps_session_owned_until_settled(self):
        store, scope = self.memory()
        entered, release = threading.Event(), threading.Event()
        session = Session(self.options([[chunk("ok")]], close_timeout=.02))
        session.run("Use unittest")
        def blocked(span):
            entered.set()
            release.wait(5)
            return self.extract(span)
        try:
            with self.assertRaises(TimeoutError): session.extract_memory(store, scope, blocked, timeout=.01)
            self.assertTrue(entered.is_set())
            with self.assertRaises(SessionBusyError): session.run("must wait")
            with self.assertRaises(CloseTimeoutError): session.close()
            self.assertEqual(store.list(scope), ())
        finally:
            release.set()
            # close owns and waits for the callback, rather than leaking its resources.
            session.options = replace(session.options, close_timeout=5)
            session.close()
        self.assertEqual(store.list(scope), ())

    def test_async_extractor_and_omitted_memory_are_explicit(self):
        store, scope = self.memory()
        async def go():
            async with AsyncSession(self.options([[chunk("ok")]])) as session:
                await session.run("Use unittest")
                async def extract(span): return self.extract(span)
                records = await session.extract_memory(store, scope, extract)
                self.assertEqual(len(records), 1)
                self.assertEqual(await session.recall_memory(store, scope, "unittest"), "")
        asyncio.run(go())
        with self.assertRaises(ConfigurationError): Session(replace(self.options(), memory_store=store))

    def test_extraction_is_bounded_without_silently_clipping_evidence(self):
        store, scope = self.memory()
        with Session(self.options([[chunk("ok")]])) as session:
            session.run("unittest " * 100)
            with self.assertRaises(ConfigurationError): session.extract_memory(store, scope, self.extract, max_chars=256)
            self.assertEqual(store.list(scope), ())
