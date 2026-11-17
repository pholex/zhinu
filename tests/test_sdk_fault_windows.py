"""Barrier-driven cancellation and actual host-exit windows through SDK APIs."""
from __future__ import annotations

import asyncio
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))
from xiaoyu_agent_sdk import (AsyncSession, ModelOptions, RunCompleted, Session, SessionBusyError,
    SessionLockedError, SessionOptions, SessionStorageError, SQLiteSessionStore, Tool)
from tests.test_agent_paths import FakeClient, chunk
from tests.test_sdk import call


class CancellationWindows(unittest.IsolatedAsyncioTestCase):
    async def test_request_stream_and_tool_cancel_then_continue(self):
        for stage in ("request", "stream", "tool"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                entered, release, settled = threading.Event(), threading.Event(), threading.Event()
                class Client:
                    def __init__(self):
                        self.chat = SimpleNamespace(completions=self)
                        self.calls = 0

                    def create(self, **kwargs):
                        self.calls += 1
                        if self.calls != 1:
                            return iter([chunk("recovered")])
                        if stage == "request":
                            entered.set()
                            release.wait(5)
                            return iter([chunk("first")])
                        if stage == "tool":
                            return iter([call("wait", "{}")])
                        def stream():
                            yield chunk("partial")
                            entered.set()
                            release.wait(5)
                            yield chunk("rest")
                        return stream()

                def business():
                    entered.set()
                    try:
                        release.wait(5)
                        return "42"
                    finally:
                        settled.set()

                opts = SessionOptions(ModelOptions("fault", client=Client()), Path(tmp), builtin_tools=(),
                    tools=(Tool("wait", "controlled barrier", {"type": "object"}, business, requires_approval=False),))
                async with AsyncSession(opts) as session:
                    task = asyncio.create_task(session.run("go"))
                    try:
                        self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                        started = time.monotonic()
                        task.cancel()
                        # Let cancellation reach the session before providing
                        # the cooperative I/O boundary. No timing-only race.
                        await asyncio.sleep(0)
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 2)
                        self.assertLess(time.monotonic() - started, 2)
                        if stage == "tool":
                            self.assertTrue(await asyncio.to_thread(settled.wait, 2))
                        async def recover():
                            while True:
                                try:
                                    return await session.run("continue")
                                except SessionBusyError:
                                    # A released synchronous host callback may
                                    # still be finishing its Future bookkeeping.
                                    await asyncio.sleep(.001)
                        self.assertEqual((await asyncio.wait_for(recover(), 2)).text, "recovered")
                    finally:
                        release.set()


HOST = r'''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from xiaoyu_agent_sdk import Session, SessionOptions, ModelOptions, Tool
from tests.test_agent_paths import chunk
from tests.test_sdk import call
root, stage = Path(sys.argv[1]), sys.argv[2]
def exit_host():
    os._exit(23)
def business():
    with (root / "effects").open("a") as out:
        out.write("committed\n")
        out.flush()
        os.fsync(out.fileno())
    if stage == "effect_before_result":
        exit_host()
    return "42"
def approve(*args):
    if stage == "approval":
        exit_host()
    return True
class Client:
    def __init__(self):
        self.chat = SimpleNamespace(completions=self)
        self.calls = 0
    def create(self, **kwargs):
        self.calls += 1
        if stage == "request" or stage == "result_before_next_response" and self.calls == 2:
            exit_host()
        return iter([call("business", "{}")])
opts = SessionOptions(ModelOptions("fault", client=Client()), root, builtin_tools=(),
    session_dir=root / "logs", tools=(Tool("business", "commit test effect", {"type":"object"}, business),), approver=approve)
session = Session(opts)
(root / "session-path").write_text(str(session.session_path), encoding="utf-8")
if stage == "idle":
    exit_host()
if stage == "torn_write":
    with session.session_path.open("ab") as out:
        out.write(b'{"event":"unfinished')
        out.flush()
        os.fsync(out.fileno())
    exit_host()
session.run("commit once")
raise AssertionError("Did not reach exit window")
'''


class HostExitWindows(unittest.TestCase):
    def test_six_exit_windows_restore_without_replaying_effects(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "packages/xiaoyu-agent-sdk/src")))
        for stage in ("idle", "request", "approval", "effect_before_result", "result_before_next_response", "torn_write"):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                process = subprocess.run([sys.executable, "-c", HOST, str(workspace), stage],
                    env=env, capture_output=True, timeout=15)
                self.assertEqual(process.returncode, 23, process.stderr.decode(errors="replace"))
                log = Path((workspace / "session-path").read_text(encoding="utf-8"))
                effect = workspace / "effects"
                before = effect.read_text() if effect.exists() else ""
                self.assertEqual(before, "committed\n" if stage in {"effect_before_result", "result_before_next_response"} else "")
                records = []
                for line in log.read_text(encoding="utf-8").splitlines():
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        self.assertEqual(stage, "torn_write")
                self.assertEqual(sum(r.get("role") == "tool" for r in records),
                                 1 if stage == "result_before_next_response" else 0)
                options = SessionOptions(ModelOptions("fault", client=FakeClient([[chunk("recovered")]])),
                    workspace, builtin_tools=(), session_dir=workspace / "logs",
                    tools=(Tool("business", "test", {"type": "object"}, lambda: self.fail("must not replay")),))
                with Session(options, resume_from=log) as restored:
                    self.assertEqual(restored.run("continue").text, "recovered")
                    history = options.model.client.completions.calls[-1]["messages"]
                    calls = {c["id"] for m in history for c in m.get("tool_calls", [])}
                    results = {m["tool_call_id"] for m in history if m.get("role") == "tool"}
                    self.assertEqual(calls, results)
                    if stage in {"approval", "effect_before_result"}:
                        self.assertTrue(any("结果未知" in str(m.get("content")) for m in history if m.get("role") == "tool"))
                self.assertEqual(effect.read_text() if effect.exists() else "", before)

    def test_host_idempotency_controls_model_reissued_actions_after_uncertain_exit(self):
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join((str(root), str(root / "packages/xiaoyu-agent-sdk/src")))
        for idempotent in (False, True):
            with self.subTest(idempotent=idempotent), tempfile.TemporaryDirectory() as tmp:
                workspace = Path(tmp).resolve()
                process = subprocess.run([sys.executable, "-c", HOST, str(workspace), "effect_before_result"],
                    env=env, capture_output=True, timeout=15)
                self.assertEqual(process.returncode, 23, process.stderr.decode(errors="replace"))
                log = Path((workspace / "session-path").read_text(encoding="utf-8"))
                ledger = workspace / "effects"
                calls = []
                def reissued():
                    calls.append(1)
                    if idempotent and ledger.exists():
                        return "already committed for this business key"
                    with ledger.open("a") as stream:
                        stream.write("committed\n")
                    return "new commit"
                client = FakeClient([[call("business", "{}", "repeat")], [chunk("done")]])
                options = SessionOptions(ModelOptions("fault", client=client), workspace, builtin_tools=(),
                    session_dir=workspace / "logs", tools=(Tool("business", "test", {"type": "object"},
                        reissued, requires_approval=False),))
                with Session(options, resume_from=log) as session:
                    self.assertEqual(calls, [])  # restore itself never replayed
                    self.assertEqual(session.run("model asks for the action again").text, "done")
                self.assertEqual(calls, [1])
                self.assertEqual(len(ledger.read_text().splitlines()), 1 if idempotent else 2)


class StorageErrorWindows(unittest.TestCase):
    def test_error_classes_preserve_records_and_hold_lease_until_close_succeeds(self):
        for number in (errno.EACCES, errno.ENOSPC, errno.EIO):
            for operation in ("append", "close"):
                with self.subTest(errno=number, operation=operation), tempfile.TemporaryDirectory() as tmp:
                    workspace = Path(tmp).resolve()
                    store = SQLiteSessionStore(workspace / "sessions.sqlite")
                    options = SessionOptions(ModelOptions("fault", client=FakeClient([[chunk("recovered")]])),
                        workspace, builtin_tools=(), session_store=store)
                    session = Session(options)
                    try:
                        writer = session._log.writer
                        before = writer.read()
                        with patch.object(writer, operation, side_effect=OSError(number, "synthetic-storage-private")):
                            with self.assertRaises(SessionStorageError) as error:
                                if operation == "append":
                                    events = []
                                    for event in session.stream("fail write"):
                                        events.append(event)
                                else:
                                    session.close()
                            self.assertNotIn("synthetic-storage-private", str(error.exception))
                            self.assertFalse(session.closed)
                            if operation == "append":
                                self.assertFalse(any(isinstance(e, RunCompleted) for e in events))
                                self.assertEqual(options.model.client.completions.calls, [])
                            records = writer.read()
                            self.assertEqual(records[:len(before)], before)
                            # close journals its exit intent before releasing
                            # the writer; a failed release must preserve both.
                            self.assertTrue(all(r.get("event") == "exit" for r in records[len(before):]))
                            if operation == "append":
                                self.assertEqual(records, before)
                            with self.assertRaises(SessionLockedError):
                                Session(options, resume_id=session.session_id)
                        session.close()
                        with Session(options, resume_id=session.session_id) as recovered:
                            self.assertEqual(recovered.run("continue").text, "recovered")
                    finally:
                        session.close()
