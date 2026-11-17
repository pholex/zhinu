from __future__ import annotations
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from labs.desktop.terminal import TerminalRegistry, TerminalConflict, install_terminal_routes


def wait(fn):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(.02)
    raise AssertionError('PTY did not reach expected state')


class TerminalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.registry = TerminalRegistry(); self.addCleanup(self.registry.close)

    def terminal(self):
        result = self.registry.create('create', 'session', self.root, 'bash', 80, 24)
        terminal = self.registry.get(result['id'])
        wait(lambda: terminal.output(1)['chunks'])
        terminal.write('stty -echo\r', 1)
        time.sleep(.1)
        return terminal

    def text(self, terminal):
        with terminal.lock:
            return ''.join(chunk['text'] for chunk in terminal.chunks)

    def test_real_tty_resize_control_and_exact_duplicate(self):
        terminal = self.terminal()
        terminal.resize(101, 33)
        command = "test -t 0 && test -t 1 && printf 'TTY_OK\\n'; stty size; printf x >> counter\r"
        terminal.write(command, 2)
        self.assertTrue(terminal.write(command, 2)['duplicate'])
        wait(lambda: '33 101' in self.text(terminal))
        self.assertIn('TTY_OK', self.text(terminal))
        self.assertEqual((self.root/'counter').read_text(), 'x')
        with self.assertRaises(TerminalConflict): terminal.write('other', 2)
        with self.assertRaises(TerminalConflict): terminal.write('skip', 4)
        terminal.write('sleep 20\r', 3)
        time.sleep(.1)
        terminal.write('\x03', 4)
        terminal.write("printf 'AFTER_INTERRUPT\\n'\r", 5)
        wait(lambda: 'AFTER_INTERRUPT' in self.text(terminal))
        terminal.stop()
        wait(lambda: terminal.info()['state'] == 'exited')

    def test_no_model_secrets_and_registry_exact_create(self):
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'fixture-secret', 'XIAOYU_TOKEN': 'fixture-token'}):
            terminal = self.terminal()
        terminal.write("printf 'ENV:%s:%s\\n' \"$OPENAI_API_KEY\" \"$XIAOYU_TOKEN\"\r", 2)
        wait(lambda: 'ENV::' in self.text(terminal))
        duplicate = self.registry.create('create', 'session', self.root, 'bash', 80, 24)
        self.assertTrue(duplicate['duplicate']); self.assertEqual(duplicate['id'], terminal.id)
        with self.assertRaises(TerminalConflict): self.registry.create('create', 'other', self.root, 'bash', 80, 24)

    def test_bounded_output_and_close(self):
        terminal = self.terminal()
        terminal.write("head -c 600000 /dev/zero | tr '\\000' x; printf '\\nDONE\\n'\r", 2)
        wait(lambda: 'DONE' in self.text(terminal))
        result = terminal.output(1)
        self.assertTrue(result['truncated']); self.assertLessEqual(terminal.bytes, 512*1024)
        self.registry.close()
        self.assertEqual(terminal.info()['state'], 'exited')
        with self.assertRaises(TerminalConflict): self.registry.create('new', 'session', self.root, 'bash', 80, 24)

    def test_partial_input_failure_reserves_number_and_never_replays(self):
        terminal = self.terminal()
        real_write = os.write
        calls = []
        def partial(fd, value):
            calls.append(bytes(value))
            if len(calls) == 1:
                return real_write(fd, value[:1])
            raise OSError('fixture interruption after first byte')
        with patch('labs.desktop.terminal.os.write', side_effect=partial):
            with self.assertRaises(OSError): terminal.write('abc', 2)
        self.assertEqual(terminal.info()['input_next'], 3)
        with patch('labs.desktop.terminal.os.write') as never:
            self.assertTrue(terminal.write('abc', 2)['duplicate'])
            never.assert_not_called()
        terminal.write('\x15', 3)

    def test_http_instance_scope_owner_and_safety_stop(self):
        app = FastAPI(); app.state.instance_id = 'epoch-one'
        app.state.execution_owner = SimpleNamespace(held=True)
        app.state.sessions = {'sid': SimpleNamespace(workspace=self.root, closing=False)}
        registry = install_terminal_routes(app, 'fixture-bearer', self.root)
        headers = {'Authorization':'Bearer fixture-bearer', 'X-Xiaoyu-Instance':'epoch-one', 'Idempotency-Key':'one'}
        with TestClient(app) as client:
            self.assertEqual(client.get('/desktop/terminals').status_code, 401)
            payload = {'session_id':'sid','shell':'bash','columns':80,'rows':24}
            self.assertEqual(client.post('/desktop/terminals',json=payload,headers={**headers,'X-Xiaoyu-Instance':'old'}).status_code,409)
            self.assertFalse(registry.terminals)
            made=client.post('/desktop/terminals',json=payload,headers=headers); self.assertEqual(made.status_code,200,made.text)
            tid=made.json()['id']
            self.assertEqual(client.post('/desktop/terminals',json=payload,headers=headers).json()['id'],tid)
            self.assertEqual(client.post('/desktop/terminals',json={**payload,'cwd':'/'},headers=headers).status_code,422)
            app.state.execution_owner.held=False
            self.assertEqual(client.post(f'/desktop/terminals/{tid}/input',json={'text':'x','input_id':1},headers=headers).status_code,503)
            self.assertEqual(client.post(f'/desktop/terminals/{tid}/stop',json={'signal':'terminate'},headers=headers).status_code,200)
        self.assertTrue(registry.closed)
        self.assertEqual(registry.get(tid).info()['state'],'exited')
