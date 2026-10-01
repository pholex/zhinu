"""P4 storage through public SDK APIs and actual SQLite/process boundaries."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))

from xiaoyu_agent_sdk import (
    AsyncSession, ConfigurationError, ModelOptions, RunCompleted, Session,
    SessionLockedError, SessionOptions, SessionStorageError, SQLiteSessionStore,
    Tool,
)
from tests.sdk_store_contracts import check_session_store
from tests.test_agent_paths import FakeClient, chunk
from tests.test_sdk import call


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()
        self.store = SQLiteSessionStore(self.workspace / "sessions.sqlite")

    def options(self, script, **kwargs):
        return SessionOptions(ModelOptions("storage-model", client=FakeClient(script)),
                              self.workspace, builtin_tools=(), session_store=self.store, **kwargs)

    def test_reusable_contract(self):
        check_session_store(self.store, self.workspace)

    def test_concurrent_session_writers_keep_every_committed_record(self):
        ready = threading.Barrier(8)

        def write(index):
            key = str(index)
            writer = self.store.open(key, metadata={"event": "meta", "session_id": key})
            try:
                ready.wait(timeout=10)
                for number in range(20):
                    writer.append(str(number), {"session": key, "number": number})
            finally:
                writer.close()
            return key

        with ThreadPoolExecutor(max_workers=8) as pool:
            keys = list(pool.map(write, range(8)))
        for key in keys:
            writer = self.store.open(key, metadata={}, resume=True)
            try:
                self.assertEqual(writer.read()[1:], [{"session": key, "number": n} for n in range(20)])
            finally:
                writer.close()

    def test_timed_out_writer_does_not_block_later_writes(self):
        store = SQLiteSessionStore(self.workspace / "bounded.sqlite", timeout=0.05)
        first = store.open("first", metadata={"event": "meta", "session_id": "first"})
        second = store.open("second", metadata={"event": "meta", "session_id": "second"})
        entered, release = threading.Event(), threading.Event()

        def stall(sql):
            if sql.startswith("INSERT OR IGNORE INTO sdk_records"):
                entered.set()
                release.wait(5)

        first._connection.set_trace_callback(stall)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(first.append, "one", {"number": 1})
                try:
                    self.assertTrue(entered.wait(2))
                    with self.assertRaises(SessionStorageError) as error:
                        second.append("two", {"number": 2})
                    self.assertIn("admission timed out", str(error.exception.__cause__))
                finally:
                    release.set()
                pending.result(timeout=5)
            second.append("two", {"number": 2})
            self.assertEqual(second.read()[1:], [{"number": 2}])
        finally:
            release.set()
            first.close()
            second.close()

    def test_broken_log_retains_original_storage_error_chain(self):
        with Session(self.options([])) as session:
            failure = OSError("private-storage-detail")
            with patch.object(session._log.writer, "append", side_effect=failure):
                with self.assertRaises(SessionStorageError):
                    session._log.event("first")
                with self.assertRaises(SessionStorageError) as error:
                    session._log.event("second")
                self.assertIs(error.exception.__cause__, failure)
                self.assertNotIn("private-storage-detail", str(error.exception))

    def test_initialization_and_reads_wait_with_writers_and_release_failed_lease(self):
        store = SQLiteSessionStore(self.workspace / "opening.sqlite", timeout=0.05)
        first = store.open("first", metadata={"event": "meta", "session_id": "first"})
        second = store.open("second", metadata={"event": "meta", "session_id": "second"})
        entered, release = threading.Event(), threading.Event()

        def stall(sql):
            if sql.startswith("INSERT OR IGNORE INTO sdk_records"):
                entered.set()
                release.wait(5)

        first._connection.execute("BEGIN EXCLUSIVE")
        first._connection.set_trace_callback(stall)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(first.append, "one", {"number": 1})
                try:
                    self.assertTrue(entered.wait(2))
                    operations = [second.read, store.list_sessions,
                                  lambda: store.open("third", metadata={"event": "meta", "session_id": "third"})]
                    for operation in operations:
                        with self.subTest(operation=operation):
                            with self.assertRaises(SessionStorageError) as error:
                                operation()
                            cause = error.exception
                            while cause.__cause__ is not None:
                                cause = cause.__cause__
                            self.assertIn("admission timed out", str(cause))
                finally:
                    release.set()
                pending.result(timeout=5)
            third = store.open("third", metadata={"event": "meta", "session_id": "third"})
            third.close()
            self.assertEqual({item.session_id for item in store.list_sessions()}, {"first", "second", "third"})
            self.assertEqual(second.read(), [{"event": "meta", "session_id": "second"}])
        finally:
            release.set()
            first.close()
            second.close()

    def test_resume_and_fork_keep_distinct_identity_and_history(self):
        options = self.options([[chunk("first")]])
        with Session(options) as session:
            session.run("remember-me")
            key = session.session_id
            self.assertIsNone(session.session_path)
            with self.assertRaises(SessionLockedError):
                Session(options, resume_id=key)
            child_options = self.options([[chunk("forked")]])
            with session.fork(options=child_options) as child:
                self.assertNotEqual(child.session_id, key)
                self.assertEqual(child.run("fork").text, "forked")
                self.assertTrue(any(m.get("content") == "remember-me"
                                    for m in child_options.model.client.completions.calls[-1]["messages"]))
        # A new adapter object and connection read the committed data.
        resumed_options = replace(self.options([[chunk("resumed")]]),
                                  session_store=SQLiteSessionStore(self.store.path))
        with Session(resumed_options, resume_id=key) as restored:
            self.assertEqual(restored.session_id, key)
            self.assertEqual(restored.checkpoints(), ())
            self.assertEqual(restored.run("continue").text, "resumed")
            self.assertTrue(any(m.get("content") == "remember-me"
                                for m in resumed_options.model.client.completions.calls[-1]["messages"]))
        self.assertEqual(len(self.store.list_sessions()), 2)

    def test_configuration_and_workspace_errors_preserve_history(self):
        with self.assertRaises(ConfigurationError):
            Session(self.options([], session_dir=self.workspace / "logs"))
        with self.assertRaises(ConfigurationError):
            Session(replace(self.options([]), session_store=None), resume_id="missing")
        with Session(self.options([[chunk("done")]])) as session:
            session.run("keep")
            key = session.session_id
        writer = self.store.open(key, metadata={}, resume=True)
        before = writer.read()
        writer.close()
        other = self.workspace / "other"
        other.mkdir()
        with self.assertRaises(SessionStorageError):
            Session(replace(self.options([]), workspace=other), resume_id=key)
        writer = self.store.open(key, metadata={}, resume=True)
        try:
            self.assertEqual(writer.read(), before)
        finally:
            writer.close()

    def test_storage_failure_never_reports_completed_or_runs_next_turn(self):
        options = self.options([[chunk("must-not-complete")]])
        with Session(options) as session:
            writer = session._log.writer
            with patch.object(writer, "append", side_effect=OSError("private-storage-detail")):
                events = []
                with self.assertRaises(SessionStorageError) as error:
                    for event in session.stream("fail"):
                        events.append(event)
                self.assertNotIn("private-storage-detail", str(error.exception))
                self.assertFalse(any(isinstance(event, RunCompleted) for event in events))
                with self.assertRaises(SessionStorageError):
                    session.run("next")
            self.assertEqual(options.model.client.completions.calls, [])
        self.assertTrue(session.closed)

    def test_kernel_replay_compact_rewind_and_clear(self):
        from xiaoyu.session_log import replay_records
        records = [{"role": "user", "content": "old"},
                   {"event": "compact", "replacement": [{"role": "user", "content": "summary"}]},
                   {"role": "assistant", "content": "after"},
                   {"event": "rewind", "replacement": [{"role": "user", "content": "rewound"}]}]
        self.assertEqual(replay_records(records), [{"role": "user", "content": "rewound"}])
        self.assertEqual(replay_records(records + [{"event": "clear"}]), [])

    def test_close_failure_retains_lock_until_successful_retry(self):
        options = self.options([])
        session = Session(options)
        try:
            key = session.session_id
            with patch.object(session._log.writer, "close", side_effect=OSError("blocked close")):
                with self.assertRaises(SessionStorageError):
                    session.close()
                self.assertFalse(session.closed)
                with self.assertRaises(SessionLockedError):
                    Session(options, resume_id=key)
            session.close()
            self.assertTrue(session.closed)
            writer = self.store.open(key, metadata={}, resume=True)
            try:
                self.assertEqual(sum(r.get("event") == "exit" for r in writer.read()), 1)
            finally:
                writer.close()
        finally:
            session.close()

    def test_exclusive_ownership_across_processes(self):
        root = Path(__file__).resolve().parents[1]
        code = '''
import sys
from pathlib import Path
from xiaoyu_agent_sdk import SQLiteSessionStore, SessionLockedError
try:
    writer = SQLiteSessionStore(Path(sys.argv[1])).open(sys.argv[2], metadata={}, resume=True)
except SessionLockedError:
    sys.exit(0)
writer.close()
sys.exit(9)
'''
        env = {key: value for key, value in os.environ.items() if not key.startswith("COVERAGE_")}
        env["PYTHONPATH"] = os.pathsep.join([str(root), str(root / "packages/xiaoyu-agent-sdk/src")])
        with Session(self.options([])) as session:
            result = subprocess.run([sys.executable, "-c", code, str(self.store.path), session.session_id],
                                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_corrupt_record_and_new_format_fail_without_modifying_data(self):
        with Session(self.options([])) as session:
            key = session.session_id
        connection = sqlite3.connect(self.store.path)
        try:
            with connection:
                connection.execute("UPDATE sdk_records SET body='not-json' WHERE session_id=?", (key,))
            with self.assertRaises(SessionStorageError):
                Session(self.options([]), resume_id=key)
            with connection:
                connection.execute("DELETE FROM sdk_records WHERE session_id=?", (key,))
                row = connection.execute("SELECT metadata FROM sdk_sessions WHERE id=?", (key,)).fetchone()
                meta = json.loads(row[0])
                meta["format"] = 99999
                connection.execute("UPDATE sdk_sessions SET metadata=? WHERE id=?", (json.dumps(meta), key))
            with self.assertRaises(SessionStorageError):
                Session(self.options([]), resume_id=key)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sdk_records WHERE session_id=?", (key,)).fetchone()[0], 0)
        finally:
            connection.close()

    def test_process_exit_releases_ownership_and_unknown_tool_is_not_replayed(self):
        root = Path(__file__).resolve().parents[1]
        code = '''
import os, sys
from pathlib import Path
from xiaoyu_agent_sdk import SQLiteSessionStore
from xiaoyu_agent_sdk.storage import session_metadata
store = SQLiteSessionStore(Path(sys.argv[1]))
writer = store.open("crashed", metadata=session_metadata("crashed", "test", Path(sys.argv[2])))
writer.append("user", {"role":"user", "content":"charge once"})
writer.append("call", {"role":"assistant", "content":None, "tool_calls":[{"id":"charge-id", "type":"function", "function":{"name":"charge", "arguments":"{}"}}]})
os._exit(17)
'''
        env = {key: value for key, value in os.environ.items() if not key.startswith("COVERAGE_")}
        env["PYTHONPATH"] = os.pathsep.join([str(root), str(root / "packages/xiaoyu-agent-sdk/src")])
        process = subprocess.run([sys.executable, "-c", code, str(self.store.path), str(self.workspace)],
                                 env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(process.returncode, 17, process.stderr)
        effects = []
        tool = Tool("charge", "charge", {"type": "object"}, lambda: effects.append(1), requires_approval=False)
        options = self.options([[chunk("outcome unknown")]], tools=(tool,))
        with Session(options, resume_id="crashed") as restored:
            restored.run("continue")
        self.assertEqual(effects, [])
        history = options.model.client.completions.calls[-1]["messages"]
        result = next(m for m in history if m.get("tool_call_id") == "charge-id")
        self.assertIn("结果未知", result["content"])

    def test_completed_tool_result_is_restored_without_replaying_effect(self):
        effects = []
        tool = Tool("save", "save", {"type": "object"}, lambda: effects.append(1) or "saved", requires_approval=False)
        with Session(self.options([[call("save", "{}")], [chunk("done")]], tools=(tool,))) as session:
            session.run("save")
            key = session.session_id
        with Session(self.options([[chunk("restored")]], tools=(tool,)), resume_id=key) as session:
            session.run("continue")
        self.assertEqual(effects, [1])


class AsyncStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_resume_and_parallel_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp).resolve()
            store = SQLiteSessionStore(workspace / "state.sqlite")
            def options():
                return SessionOptions(ModelOptions("test", client=FakeClient([[chunk("ok")]])),
                                      workspace, builtin_tools=(), session_store=store)
            async with AsyncSession(options()) as first, AsyncSession(options()) as second:
                results = await asyncio.gather(first.run("left"), second.run("right"))
                self.assertEqual([r.text for r in results], ["ok", "ok"])
                key = first.session_id
            restored_options = options()
            async with AsyncSession(restored_options, resume_id=key) as restored:
                await restored.run("again")
            self.assertTrue(any(m.get("content") == "left" for m in restored_options.model.client.completions.calls[-1]["messages"]))
