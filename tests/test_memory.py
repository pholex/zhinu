import contextlib
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from xiaoyu._state_db import StateError
from xiaoyu.memory import MemoryStore, MemoryScope, MemoryCandidate, MemoryConflict


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.db"
        self.now = [100.0]
        self.store = MemoryStore(self.path, clock=lambda: self.now[0])
        self.scope = MemoryScope("alice", "project-a")

    def candidate(self, **kwargs):
        return replace(MemoryCandidate("test-runner", "项目测试使用 unittest", "lesson", "session#1", "请用 unittest"), **kwargs)

    def accepted(self, **kwargs):
        r = self.store.propose(self.scope, self.candidate(**kwargs))
        return self.store.accept(self.scope, r.id, reviewer="alice")

    def test_pending_never_recalled_and_scopes_do_not_fall_back(self):
        r = self.store.propose(self.scope, self.candidate())
        self.assertEqual(self.store.recall(self.scope, "unittest"), ())
        self.store.accept(self.scope, r.id, reviewer="alice")
        self.assertEqual(len(self.store.recall(self.scope, "测试")), 1)
        for scope in (MemoryScope("bob", "project-a"), MemoryScope("alice"), MemoryScope("alice", "project-b")):
            self.assertEqual(self.store.recall(scope, "unittest"), ())
            with self.assertRaises(KeyError): self.store.get(scope, r.id)
        reopened = MemoryStore(self.path)
        self.assertEqual(reopened.get(self.scope, r.id).status, "active")

    def test_revision_conflict_preserves_newer_correction(self):
        first = self.accepted()
        a = self.store.propose(self.scope, self.candidate(text="new A", base_revision=first.revision))
        b = self.store.propose(self.scope, self.candidate(text="new B", base_revision=first.revision))
        updated = self.store.accept(self.scope, a.id, reviewer="alice")
        with self.assertRaises(MemoryConflict): self.store.accept(self.scope, b.id, reviewer="alice")
        self.assertEqual(self.store.current(self.scope, "test-runner"), updated)
        self.assertEqual(self.store.get(self.scope, first.id).status, "superseded")
        self.assertEqual(self.store.get(self.scope, b.id).status, "pending")

    def test_expiry_budget_and_complete_records(self):
        self.accepted(expires_at=110)
        self.assertEqual(self.store.recall(self.scope, "unittest", char_budget=3), ())
        for budget in (20, 500, 1000):
            text = self.store.context(self.scope, "unittest", char_budget=budget)
            self.assertLessEqual(len(text), budget)
            if text: self.assertIn("请用 unittest", text)
        self.now[0] = 110
        self.assertEqual(self.store.recall(self.scope, "unittest"), ())
        self.assertEqual(len(self.store.list(self.scope)), 1)

    def test_transactional_receipt_replay_and_forgotten_data_stays_forgotten(self):
        digest = hashlib.sha256(b"source").hexdigest()
        values = self.store.stage(self.scope, source="session:1:2", digest=digest, candidates=(self.candidate(),))
        self.assertEqual(self.store.stage(self.scope, source="session:1:2", digest=digest, candidates=()), values)
        self.store.accept(self.scope, values[0].id, reviewer="alice")
        self.store.forget(self.scope, "test-runner")
        replay = self.store.stage(self.scope, source="session:1:2", digest=digest, candidates=(self.candidate(),))
        self.assertEqual(replay[0].status, "forgotten")
        self.assertEqual(replay[0].candidate.text, "")
        self.assertEqual(self.store.recall(self.scope, "unittest"), ())
        with self.assertRaises(MemoryConflict):
            self.store.stage(self.scope, source="session:1:2", digest="0" * 64, candidates=())

    def test_stage_and_accept_failure_roll_back_every_record(self):
        original = self.store._save
        count = [0]
        def broken(db, record):
            original(db, record)
            count[0] += 1
            if count[0] == 2: raise OSError("disk full")
        with patch.object(self.store, "_save", side_effect=broken), self.assertRaises(OSError):
            self.store.stage(self.scope, source="span", digest="1" * 64,
                             candidates=(self.candidate(), self.candidate(key="another")))
        self.assertEqual(self.store.list(self.scope), ())
        self.assertIsNone(self.store.receipt(self.scope, "span", "1" * 64))
        first = self.accepted()
        next_record = self.store.propose(self.scope, self.candidate(text="changed", base_revision=1))
        count[0] = 0
        with patch.object(self.store, "_save", side_effect=broken), self.assertRaises(OSError):
            self.store.accept(self.scope, next_record.id, reviewer="alice")
        self.assertEqual(self.store.current(self.scope, "test-runner"), first)

    def test_rejection_and_skill_publication_require_review(self):
        r = self.store.propose(self.scope, self.candidate(kind="skill", text="---\nname: test-helper\ndescription: Run project tests\n---\nUse unittest."))
        dest = self.path.parent / "skills/test-helper/SKILL.md"
        with self.assertRaises(MemoryConflict): self.store.export_skill(self.scope, r.id, dest)
        self.store.accept(self.scope, r.id, reviewer="alice")
        self.store.export_skill(self.scope, r.id, dest)
        with self.assertRaises(FileExistsError): self.store.export_skill(self.scope, r.id, dest)
        self.assertEqual(dest.read_text(encoding="utf-8"), r.candidate.text)
        rejected = self.store.propose(self.scope, self.candidate(key="bad"))
        self.store.reject(self.scope, rejected.id)
        with self.assertRaises(MemoryConflict): self.store.accept(self.scope, rejected.id, reviewer="alice")

    def test_corrupt_and_foreign_databases_are_not_rebuilt(self):
        for raw in (b"damaged\xff", b"{\"state\":1}"):
            other = self.path.parent / "bad.db"
            other.write_bytes(raw)
            with self.assertRaises(StateError): MemoryStore(other)
            self.assertEqual(other.read_bytes(), raw)
        import sqlite3
        other.unlink()
        with sqlite3.connect(other) as db: db.execute("CREATE TABLE other (id TEXT)")
        with self.assertRaises(StateError): MemoryStore(other)
        with sqlite3.connect(other) as db:
            self.assertEqual(db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("other",)])

    def test_cli_review_recall_and_forget(self):
        from xiaoyu.cli_memory import memory_command
        def cli(*args):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(memory_command(["--db", str(self.path), "--owner", "alice", "--project", "project-a", *args]), 0)
            return out.getvalue()
        record = json.loads(cli("propose", "runner", "use unittest", "--source", "chat#1", "--evidence", "use unittest"))
        self.assertEqual(cli("recall", "unittest").strip(), "")
        cli("accept", record["id"], "--reviewer", "alice")
        self.assertIn("use unittest", cli("recall", "unittest"))
        cli("forget", "runner")
        self.assertEqual(cli("recall", "unittest").strip(), "")
