"""预览只读；确认只能执行审阅过的检查点、文件和对话版本。"""
from __future__ import annotations

import contextlib
import copy
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_rewind as support
from xiaoyu.cli import _rewind_flow
from xiaoyu.rewind import MAX_FILE_BYTES, PREVIEW_CHARS, RewindStore


class PreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.store = RewindStore(self.root)
        self.events = []
        self.store.journal = lambda kind, **fields: self.events.append((kind, fields))
        self.path = self.root / 'a.txt'
        self.path.write_bytes(b'before\n')
        self.store.begin('edit')
        self.edit(self.path, b'after\n')
        self.new = self.root / 'new.txt'
        self.edit(self.new, b'new\n')
        self.store.finish()

    def edit(self, path, content):
        self.store.record(path, path.read_bytes() if path.exists() else None)
        path.write_bytes(content)
        self.store.written(path, content)

    def test_preview_is_read_only_and_diff_runs_towards_target(self):
        before = copy.deepcopy(self.events)
        preview = self.store.preview(1)
        self.assertEqual([(i.path, i.action) for i in preview.files], [('a.txt', 'restore'), ('new.txt', 'delete')])
        self.assertIn('-after\n+before\n', preview.files[0].diff)
        self.assertIn('-new\n', preview.files[1].diff)
        self.assertEqual(self.path.read_bytes(), b'after\n')
        self.assertEqual(self.new.read_bytes(), b'new\n')
        self.assertEqual(self.events, before)
        self.assertEqual(preview.token, self.store.preview(1, include_diff=False).token)
        self.assertFalse(self.store.preview(99).available)

    def test_external_edit_and_deleted_file_are_conflicts(self):
        self.path.write_bytes(b'user\n')
        self.new.unlink()
        preview = self.store.preview(1)
        self.assertTrue(all(i.action == 'conflict' for i in preview.files))
        self.assertIn('-user\n+before\n', preview.files[0].diff)
        self.assertEqual(self.path.read_bytes(), b'user\n')

    def test_unconfirmed_and_oversized_files_are_distinguished(self):
        self.store.begin('next')
        self.store.record(self.path, b'after\n')
        huge = self.root / 'huge.bin'
        self.store.record(huge, b'x' * (MAX_FILE_BYTES + 1))
        self.store.finish()
        items = {i.path: i for i in self.store.preview(1).files}
        self.assertEqual(items['a.txt'].action, 'conflict')
        self.assertIn('写后确认', items['a.txt'].note)
        self.assertEqual(items['huge.bin'].action, 'skip')

    def test_binary_legacy_encoding_and_large_diff_are_bounded(self):
        self.store.begin('formats')
        for name, content in [('binary.bin', b'\x00\xff'), ('legacy.txt', '原文'.encode('gbk')),
                              ('big.txt', b'x' * 70000), ('long.txt', b'y' * 30000)]:
            self.edit(self.root / name, content)
        self.store.finish()
        preview = self.store.preview(2)
        items = {i.path: i for i in preview.files}
        self.assertEqual(items['binary.bin'].diff, '')
        self.assertEqual(items['legacy.txt'].diff, '')
        self.assertEqual(items['big.txt'].diff, '')
        self.assertTrue(items['long.txt'].diff_truncated)
        self.assertLessEqual(sum(len(i.diff) for i in preview.files), PREVIEW_CHARS)

    def test_symlink_replacement_does_not_read_target(self):
        outside = self.root / 'outside.txt'
        outside.write_bytes(b'PRIVATE CONTENT')
        self.path.unlink()
        try:
            self.path.symlink_to(outside)
        except OSError:
            self.skipTest('Symlink creation unavailable')
        preview = self.store.preview(1)
        self.assertEqual(preview.files[0].action, 'conflict')
        self.assertNotIn('PRIVATE CONTENT', str(preview))

    def test_path_resolution_failure_remains_a_conflict_on_older_python(self):
        # 较早的 pathlib 对符号链接循环抛 RuntimeError，而非 OSError。
        with patch.object(Path, 'resolve', side_effect=RuntimeError('symlink loop')):
            self.assertEqual(self.store.preview(1).files[0].action, 'conflict')
            self.assertIn(str(self.path), self.store.conflicts(1))
            self.assertFalse(self.store.rewind_files(1)[0])

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX FIFO required')
    def test_fifo_is_rejected_without_opening(self):
        self.path.unlink()
        os.mkfifo(self.path)
        real_open = os.open
        def open_file(path, *args, **kwargs):
            self.assertNotEqual(Path(path), self.path)
            return real_open(path, *args, **kwargs)
        with patch('xiaoyu.rewind.os.open', side_effect=open_file):
            self.assertEqual(self.store.preview(1).files[0].action, 'conflict')


class AgentPreviewTests(unittest.TestCase):
    make_agent = support.AgentRewindTest.make_agent
    turn = support.AgentRewindTest.turn

    def setUp(self):
        self.agent = self.make_agent()
        self.path = self.agent.config.workspace / 'a.txt'
        self.path.write_bytes(b'before\n')
        store = self.agent.toolbox.rewind
        store.begin('edit')
        self.agent.messages.append({'role': 'user', 'content': 'edit'})
        store.record(self.path, self.path.read_bytes())
        self.path.write_bytes(b'after\n')
        store.written(self.path, self.path.read_bytes())
        store.finish()

    def test_success_and_scope_binding(self):
        preview = self.agent.preview_rewind(1, conversation=False)
        history = copy.deepcopy(self.agent.messages)
        self.assertFalse(preview.conversation_available)
        self.assertEqual(self.agent.rewind_result(1, preview=preview).status, 'conflict')
        self.assertEqual(self.agent.messages, history)
        result = self.agent.rewind_result(1, conversation=False, preview=preview)
        self.assertEqual(result.status, 'completed')
        self.assertEqual(self.path.read_bytes(), b'before\n')

    def test_stale_file_cannot_be_overridden_with_force(self):
        preview = self.agent.preview_rewind(1)
        self.path.write_bytes(b'user\n')
        history = copy.deepcopy(self.agent.messages)
        result = self.agent.rewind_result(1, preview=preview, check_conflicts=False)
        self.assertEqual(result.status, 'conflict')
        self.assertIn('预览已失效', result.summary)
        self.assertEqual(self.agent.messages, history)
        self.assertEqual(self.path.read_bytes(), b'user\n')

    def test_new_turn_and_changed_history_expire_preview(self):
        preview = self.agent.preview_rewind(1)
        self.turn(self.agent, 'next')
        self.assertEqual(self.agent.rewind_result(1, preview=preview).status, 'conflict')
        preview = self.agent.preview_rewind(1)
        self.agent.messages = self.agent.messages[:1]
        self.assertEqual(self.agent.rewind_result(1, preview=preview).status, 'conflict')
        self.assertFalse(self.agent.preview_rewind(1).conversation_available)

    def test_preview_from_another_store_is_rejected(self):
        preview = self.agent.preview_rewind(1)
        self.agent.toolbox.rewind._preview_nonce = 'different session'
        self.assertEqual(self.agent.rewind_result(1, preview=preview).status, 'conflict')

    def test_last_check_after_journal_write_rejects_external_edit(self):
        preview = self.agent.preview_rewind(1)
        def journal(kind, **fields):
            if fields.get('action') == 'restore_started':
                self.path.write_bytes(b'edited while committing\n')
        self.agent.toolbox.rewind.journal = journal
        result = self.agent.rewind_result(1, preview=preview)
        self.assertEqual(result.status, 'failed')
        self.assertFalse(result.conversation_rewound)
        self.assertEqual(self.path.read_bytes(), b'edited while committing\n')

    def run_cli(self, replies, arguments=None):
        output = io.StringIO()
        with patch('builtins.input', side_effect=replies), contextlib.redirect_stdout(output):
            _rewind_flow(self.agent, arguments or ['1'])
        return output.getvalue()

    def test_cli_cancel_and_preview_only_leave_files_and_history(self):
        before = copy.deepcopy(self.agent.messages)
        shown = self.run_cli(['1', 'n'])
        self.assertIn('-after\n+before\n', shown)
        self.assertEqual(self.path.read_bytes(), b'after\n')
        self.assertEqual(self.agent.messages, before)
        shown = self.run_cli(['1'], ['1', '--preview'])
        self.assertIn('[恢复] a.txt', shown)
        self.assertEqual(self.agent.messages, before)
        self.assertEqual(self.path.read_bytes(), b'after\n')

    def test_cli_state_change_while_confirming_requires_new_preview(self):
        def answer(prompt):
            if '执行以上回滚' in prompt:
                self.path.write_bytes(b'user\n')
                return 'y'
            return '1'
        shown = self.run_cli(answer)
        self.assertIn('预览已失效', shown)
        self.assertEqual(self.path.read_bytes(), b'user\n')

    def test_cli_explicitly_confirmed_conflict_and_control_characters(self):
        self.path.write_bytes(b'\x1b[2Juser\n')
        shown = self.run_cli(['3', 'y'])
        self.assertIn('[冲突] a.txt', shown)
        self.assertNotIn('\x1b', shown)
        self.assertEqual(self.path.read_bytes(), b'before\n')
