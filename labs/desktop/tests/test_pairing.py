"""Private Unix pairing lifecycle and interoperability without model traffic."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
from unittest import mock

from labs.desktop import pairing
from xiaoyu import cli_connect as client


class PairingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir='/tmp', prefix='pair-test-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.directory = self.root / 'private'
        self.path = self.directory / 'p.sock'
        self.servers = []

    async def asyncTearDown(self):
        for server in reversed(self.servers):
            await server.close()

    def server(self, **kwargs):
        value = pairing.PairingServer(self.root, 'http://127.0.0.1:8420', 'memory-secret', 'instance-a',
                                      path=kwargs.pop('path', self.path), **kwargs)
        self.servers.append(value)
        return value

    async def frame(self, value):
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        try:
            writer.write(value)
            await writer.drain()
            return await asyncio.wait_for(reader.readline(), 2)
        finally:
            writer.close()
            await writer.wait_closed()

    def request(self):
        return json.dumps({'protocol':1,'client':'xiaoyu-cli','workspace':str(self.root)}).encode() + b'\n'

    async def test_real_client_pairing_and_same_user_peer(self):
        server = await self.server().start()
        with mock.patch.object(client, 'pairing_socket_path', return_value=self.path):
            result = await asyncio.to_thread(client.pair, self.root)
        self.assertEqual(result.workspace, str(self.root))
        self.assertEqual(result.token, 'memory-secret')
        self.assertNotIn('memory-secret', repr(result))
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        await server.close()
        self.assertFalse(self.path.exists())

    async def test_wrong_peer_uid_and_bad_requests_never_receive_token(self):
        await self.server().start()
        with mock.patch.object(pairing, 'peer_uid', return_value=os.getuid()+1):
            denied = await self.frame(self.request())
        self.assertNotIn(b'memory-secret', denied)
        self.assertEqual(json.loads(denied), {'protocol':1,'error':'pairing_denied'})
        for value in ({'protocol':True,'client':'xiaoyu-cli','workspace':str(self.root)},
                      {'protocol':1,'client':'xiaoyu-cli','workspace':'/wrong'},
                      {'protocol':1,'client':'xiaoyu-cli','workspace':str(self.root),'extra':1}):
            response = await self.frame(json.dumps(value).encode()+b'\n')
            self.assertNotIn(b'memory-secret', response)
            self.assertIn(b'pairing_denied', response)

    async def test_oversized_and_incomplete_frames_are_bounded(self):
        await self.server().start()
        response = await self.frame(b'x' * (pairing.MAX_FRAME+1) + b'\n')
        self.assertIn(b'pairing_denied', response)
        with mock.patch.object(pairing, 'REQUEST_TIMEOUT', .05):
            response = await self.frame(b'{')
        self.assertIn(b'pairing_denied', response)

    async def test_live_socket_is_never_replaced(self):
        first = await self.server().start()
        identity = self.path.stat().st_ino
        with self.assertRaisesRegex(RuntimeError, '已有'):
            await self.server().start()
        self.assertEqual(self.path.stat().st_ino, identity)
        self.assertIn(b'memory-secret', await self.frame(self.request()))
        self.assertIsNotNone(first.server)

    async def test_stale_private_socket_is_removed_but_other_nodes_are_not(self):
        self.directory.mkdir(mode=0o700)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        self.path.chmod(0o600)
        stale.close()
        server = await self.server().start()
        self.assertIn(b'memory-secret', await self.frame(self.request()))
        await server.close()
        self.path.write_text('preserve')
        self.path.chmod(0o600)
        with self.assertRaises(RuntimeError):
            await self.server().start()
        self.assertEqual(self.path.read_text(), 'preserve')

    async def test_parent_and_endpoint_symlinks_or_wrong_permissions_are_rejected(self):
        self.directory.mkdir(mode=0o755)
        with self.assertRaises(RuntimeError):
            await self.server().start()
        self.directory.chmod(0o700)
        target = self.directory / 'keep'
        target.write_text('unchanged')
        self.path.symlink_to(target)
        with self.assertRaises(RuntimeError):
            await self.server().start()
        self.assertTrue(self.path.is_symlink())
        alias = self.root / 'alias'
        alias.symlink_to(self.directory)
        with self.assertRaises(RuntimeError):
            await self.server(path=alias/'p.sock').start()
        self.assertEqual(target.read_text(), 'unchanged')

    async def test_close_does_not_unlink_replacement_socket(self):
        server = await self.server().start()
        self.path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(replacement.close)
        replacement.bind(str(self.path))
        self.path.chmod(0o600)
        identity = self.path.stat().st_ino
        await server.close()
        self.assertEqual(self.path.stat().st_ino, identity)

    async def test_start_failure_cleans_only_its_own_listener(self):
        server = self.server()
        with mock.patch.object(pairing.asyncio, 'start_unix_server', side_effect=RuntimeError('setup failed')):
            with self.assertRaisesRegex(RuntimeError, 'setup failed'):
                await server.start()
        self.assertFalse(self.path.exists())
        self.assertIsNone(server.server)
        self.assertIsNone(server.identity)

    async def test_close_cancels_idle_clients_before_waiting_for_listener_shutdown(self):
        server = await self.server().start()
        reader, writer = await asyncio.open_unix_connection(str(self.path))
        await asyncio.sleep(.01)
        self.assertTrue(server.clients)
        await asyncio.wait_for(server.close(), .5)
        self.assertEqual(await asyncio.wait_for(reader.read(), .5), b'')
        writer.close()
        await writer.wait_closed()
        self.assertFalse(server.clients)
        self.assertFalse(self.path.exists())

    async def test_socket_appearing_between_probe_and_bind_is_not_unlinked(self):
        self.directory.mkdir(mode=0o700)
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(replacement.close)
        original = socket.socket.bind
        installed = False
        def race(sock, address):
            nonlocal installed
            if not installed:
                installed = True
                original(replacement, address)
                os.chmod(address, 0o600)
            return original(sock, address)
        with mock.patch.object(socket.socket, 'bind', race):
            with self.assertRaises(OSError):
                await self.server().start()
        self.assertTrue(self.path.exists())
        self.assertEqual(replacement.getsockname(), str(self.path))

    async def test_stale_probe_identity_change_preserves_new_node(self):
        self.directory.mkdir(mode=0o700)
        stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        stale.bind(str(self.path))
        self.path.chmod(0o600)
        stale.close()
        def replaced(sock, address):
            self.path.unlink()
            self.path.write_text('new owner')
            raise ConnectionRefusedError()
        with mock.patch.object(socket.socket, 'connect', replaced):
            with self.assertRaisesRegex(RuntimeError, '正在变化'):
                await self.server().start()
        self.assertEqual(self.path.read_text(), 'new owner')

    async def test_cancellation_after_binding_releases_listener_and_owned_path(self):
        server = self.server()
        with mock.patch.object(asyncio.Server, 'start_serving', side_effect=asyncio.CancelledError):
            with self.assertRaises(asyncio.CancelledError):
                await server.start()
        self.assertIsNone(server.server)
        self.assertFalse(self.path.exists())

    async def test_close_serializes_with_start_in_progress(self):
        server = self.server()
        entered, release = asyncio.Event(), asyncio.Event()
        original = asyncio.Server.start_serving
        async def delayed(listener):
            entered.set()
            await release.wait()
            await original(listener)
        with mock.patch.object(asyncio.Server, 'start_serving', delayed):
            starting = asyncio.create_task(server.start())
            await entered.wait()
            closing = asyncio.create_task(server.close())
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(starting, closing)
        self.assertIsNone(server.server)
        self.assertFalse(self.path.exists())


if __name__ == '__main__':
    unittest.main()
