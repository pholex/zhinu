"""恢复只依赖日志里确认过的版本，不能把崩溃后的磁盘现状当成写后证据。"""
from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from xiaoyu.rewind import MAX_FILE_BYTES, RewindStore
from xiaoyu.session_log import load_messages, replay_records


class DurableRewindTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.target = self.root / "data.bin"
        self.target.write_bytes(b"before\x00\xff")
        self.records = [{"event": "meta", "workspace": str(self.root)}]
        self.store = RewindStore(self.root)
        self.store.journal = lambda event, **fields: self.records.append({"event": event, **fields})

    def edit(self, prompt="edit", content=b"after", finish=True):
        self.store.begin(prompt)
        self.records.append({"role": "user", "content": prompt})
        self.store.record(self.target, self.target.read_bytes())
        self.target.write_bytes(content)
        self.store.written(self.target, content)
        if finish:
            self.store.finish()

    def replay(self, records=None):
        loaded = replay_records(self.records if records is None else records)
        self.assertIsNotNone(loaded.checkpoints)
        return loaded.checkpoints

    def test_crash_after_confirmed_write_preserves_original_bytes(self):
        self.edit(finish=False)
        recovered = self.replay()
        self.assertEqual(recovered.conflicts(1), [])
        self.assertTrue(recovered.rewind_files(1)[0])
        self.assertEqual(self.target.read_bytes(), b"before\x00\xff")

    def test_process_exit_before_turn_finish_can_resume_from_disk(self):
        script = """
import os, sys
from pathlib import Path
from xiaoyu.rewind import RewindStore
from xiaoyu.session_log import SessionLog
root = Path(sys.argv[1])
log = SessionLog.create('test', str(root), directory=root / 'logs')
(root / 'log-path.txt').write_text(str(log.path), encoding='utf-8')
store = RewindStore(root)
store.journal = log.commit_event
store.begin('edit')
log.append({'role': 'user', 'content': 'edit'})
target = root / 'data.bin'
store.record(target, target.read_bytes())
target.write_bytes(b'written before process exit')
store.written(target, target.read_bytes())
os._exit(0)
"""
        completed = subprocess.run([sys.executable, "-P", "-c", script, str(self.root)],
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        path = Path((self.root / "log-path.txt").read_text(encoding="utf-8"))
        recovered = load_messages(path).checkpoints
        self.assertEqual(recovered.conflicts(1), [])
        self.assertTrue(recovered.rewind_files(1)[0])
        self.assertEqual(self.target.read_bytes(), b"before\x00\xff")

    def test_crash_between_write_and_confirmation_cannot_claim_current_version(self):
        self.edit(finish=False)
        self.records.pop()  # 模拟已写盘，确认尚未入日志
        recovered = self.replay()
        self.assertEqual(recovered.conflicts(1), [str(self.target)])
        self.target.write_bytes(b"user after crash")
        self.assertEqual(recovered.conflicts(1), [str(self.target)])

    def test_crash_during_second_write_does_not_reuse_first_confirmation(self):
        self.edit(finish=False)
        self.store.record(self.target, b"after")
        self.target.write_bytes(b"second")
        self.assertEqual(self.replay().conflicts(1), [str(self.target)])

    def test_external_edit_before_finish_is_not_absorbed(self):
        self.edit(finish=False)
        self.target.write_bytes(b"user")
        self.store.finish()
        self.assertEqual(self.store.conflicts(1), [str(self.target)])
        self.assertEqual(self.replay().conflicts(1), [str(self.target)])

    def test_restore_crash_marks_targets_uncertain_and_cancel_restores_eligibility(self):
        self.edit()
        self.records.append({"event": "checkpoint", "version": 1, "action": "restore_started", "index": 1})
        self.assertEqual(self.replay().conflicts(1), [str(self.target)])
        self.records.append({"event": "checkpoint", "version": 1, "action": "restore_cancelled", "index": 1})
        self.assertEqual(self.replay().conflicts(1), [])

    def test_copy_is_self_contained_and_counter_does_not_reuse_ids(self):
        self.edit()
        second = RewindStore(self.root)
        copied = [self.records[0]]
        second.journal = lambda event, **fields: copied.append({"event": event, **fields})
        second.adopt(self.replay(), persist=True)
        self.records.clear()
        recovered = self.replay(copied)
        self.assertEqual(recovered.files_from(1), {str(self.target): b"before\x00\xff"})
        recovered.begin("next")
        self.assertEqual(recovered._current.index, 2)

    def test_different_workspace_cannot_adopt_snapshots(self):
        self.edit()
        other = RewindStore(self.root / "other")
        other.adopt(self.replay(), persist=False)
        self.assertEqual(other.points(), [])

    def test_symlink_substitution_blocks_even_forced_restore(self):
        self.edit()
        outside = self.root / "untouched.bin"
        outside.write_bytes(b"user")
        self.target.unlink()
        try:
            self.target.symlink_to(outside)
        except OSError:
            self.skipTest("Symlink creation unavailable")
        recovered = self.replay()
        self.assertEqual(recovered.conflicts(1), [str(self.target)])
        self.assertFalse(recovered.rewind_files(1)[0])
        self.assertEqual(outside.read_bytes(), b"user")

    def test_malformed_paths_and_snapshots_disable_restoration(self):
        self.edit()
        for changes in ({"path": "../outside"}, {"path": str(self.target)},
                        {"before": "!not-base64!"}, {"version": 99},
                        {"before": base64.b64encode(b"x" * (MAX_FILE_BYTES + 1)).decode()}):
            records = copy.deepcopy(self.records)
            next(r for r in records if r.get("action") == "prepare").update(changes)
            recovered = self.replay(records)
            self.assertEqual(recovered.points(), [])
            self.assertTrue(recovered.problem)

    def test_corrupt_journal_cannot_silently_skip_a_mutation_record(self):
        self.edit()
        path = self.root / "log.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in self.records) + "\n{broken", encoding="utf-8")
        loaded = load_messages(path)
        self.assertEqual(loaded[0]["content"], "edit")
        self.assertEqual(loaded.checkpoints.points(), [])
        self.assertTrue(loaded.checkpoints.problem)

    def test_snapshot_bytes_never_enter_model_history(self):
        self.edit()
        loaded = replay_records(self.records)
        self.assertEqual(loaded, [{"role": "user", "content": "edit"}])
        self.assertNotIn("before", str(loaded))
