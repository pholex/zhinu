"""同步/异步宿主审阅后回滚，持久化恢复后旧预览失效。"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import importlib.util
import json
import unittest

from tests import test_sdk_controls as support
from xiaoyu_agent_sdk import AsyncSession, RewindPreview, RewindFilePreview, Session, SessionClosedError, SQLiteSessionStore


@unittest.skipUnless(importlib.util.find_spec('jsonschema'), 'Install the sdk extra')
class SDKPreviewTests(unittest.TestCase):
    setUp = support.ControlTests.setUp

    def config(self, **storage):
        return support.options(self.workspace, [
            [support.call('write_file', {'path': 'result.txt', 'content': 'created\n'})],
            [support.chunk('done')]], builtin_tools=('write_file',), approver=lambda *_: True, **storage)

    def test_preview_does_not_write_log_and_rewind_accepts_its_token(self):
        with Session(self.config(session_dir=self.workspace / 'logs')) as session:
            session.run('create')
            before = session.session_path.read_bytes()
            history = session.snapshot().history
            preview = session.preview_rewind(1)
            self.assertIsInstance(preview, RewindPreview)
            self.assertIsInstance(preview.files[0], RewindFilePreview)
            self.assertEqual(session.session_path.read_bytes(), before)
            self.assertEqual(session.snapshot().history, history)
            self.assertEqual(json.loads(json.dumps(asdict(preview)))['files'][0]['action'], 'delete')
            self.assertEqual(session.rewind(1, preview=preview).status, 'completed')
            self.assertFalse((self.workspace / 'result.txt').exists())
        with self.assertRaises(SessionClosedError):
            session.preview_rewind(1)

    def test_external_change_keeps_both_file_and_conversation(self):
        with Session(self.config()) as session:
            session.run('create')
            preview = session.preview_rewind(1)
            (self.workspace / 'result.txt').write_text('external', encoding='utf-8')
            history = session.snapshot().history
            result = session.rewind(1, preview=preview)
            self.assertEqual(result.status, 'conflict')
            self.assertEqual(session.snapshot().history, history)
            self.assertEqual((self.workspace / 'result.txt').read_text(), 'external')
            fresh = session.preview_rewind(1)
            self.assertEqual(fresh.files[0].action, 'conflict')
            self.assertEqual(session.rewind(1, preview=fresh).status, 'conflict')

    def test_resume_requires_new_preview_on_both_backends(self):
        for backend in ('files', 'sqlite'):
            with self.subTest(backend=backend):
                (self.workspace / 'result.txt').unlink(missing_ok=True)
                storage = ({'session_dir': self.workspace / 'logs'} if backend == 'files' else
                           {'session_store': SQLiteSessionStore(self.workspace / 'sessions.sqlite')})
                config = self.config(**storage)
                with Session(config) as session:
                    session.run('create')
                    old = session.preview_rewind(1)
                    resume = {'resume_from': session.session_path} if backend == 'files' else {'resume_id': session.session_id}
                with Session(config, **resume) as resumed:
                    self.assertEqual(resumed.rewind(1, preview=old).status, 'conflict')
                    fresh = resumed.preview_rewind(1)
                    self.assertEqual(fresh.files[0].action, 'delete')
                    self.assertEqual(resumed.rewind(1, preview=fresh).status, 'completed')

    def test_async_preview_and_apply(self):
        async def scenario():
            async with await AsyncSession.open(self.config()) as session:
                await session.run('create')
                preview = await session.preview_rewind(1, conversation=False)
                self.assertFalse(preview.conversation_requested)
                result = await session.rewind(1, conversation=False, preview=preview)
                self.assertEqual(result.status, 'completed')
                self.assertFalse((self.workspace / 'result.txt').exists())
        asyncio.run(scenario())
