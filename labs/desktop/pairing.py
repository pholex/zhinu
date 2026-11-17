"""Private local pairing: connection credentials live only in process memory."""
from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import struct
import sys

MAX_FRAME = 16384
REQUEST_TIMEOUT = 3.0
CLOSE_TIMEOUT = 1.0


def socket_path(workspace: Path) -> Path:
    key = hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:24]
    return Path('/tmp') / f'xiaoyu-desktop-{os.getuid()}' / f'{key}.sock'


def peer_uid(connection) -> int:
    if sys.platform == 'darwin':
        function = ctypes.CDLL(None, use_errno=True).getpeereid
        function.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint)]
        function.restype = ctypes.c_int
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        if function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise OSError('无法核验本机连接身份')
        return uid.value
    if hasattr(socket, 'SO_PEERCRED'):
        return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    raise OSError('此平台不支持本机身份核验')


class PairingServer:
    def __init__(self, workspace: Path, base_url: str, token: str, instance_id: str, *, path=None):
        self.workspace = str(workspace.resolve())
        self.path = Path(path) if path is not None else socket_path(workspace)
        self._response = {'protocol': 1, 'workspace': self.workspace, 'base_url': base_url,
                          'token': token, 'instance_id': instance_id}
        self.server = None
        self.identity = None
        self.clients = set()
        self._lifecycle = asyncio.Lock()
        self._closing = False
        self._frame = json.dumps(self._response).encode('utf-8') + b'\n'
        if len(self._frame) > MAX_FRAME:
            raise ValueError('本机配对响应过大')

    def _unlink_owned(self):
        try:
            current = self.path.lstat()
            if (stat.S_ISSOCK(current.st_mode) and current.st_uid == os.getuid()
                    and (current.st_dev, current.st_ino) == self.identity):
                self.path.unlink()
        except FileNotFoundError:
            pass
        finally:
            self.identity = None

    async def start(self):
        async with self._lifecycle:
            return await self._start()

    async def _start(self):
        if self.server is not None:
            raise RuntimeError('本机配对服务已启动')
        self._closing = False
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = self.path.parent.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid()
                or stat.S_IMODE(parent.st_mode) != 0o700):
            raise RuntimeError('本机配对目录权限不正确')
        try:
            previous = self.path.lstat()
        except FileNotFoundError:
            previous = None
        if previous is not None:
            if (not stat.S_ISSOCK(previous.st_mode) or previous.st_uid != os.getuid()
                    or stat.S_IMODE(previous.st_mode) != 0o600):
                raise RuntimeError('本机配对入口不安全')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(.2)
                try:
                    probe.connect(str(self.path))
                except (ConnectionRefusedError, FileNotFoundError):
                    current = self.path.lstat()
                    if (current.st_dev, current.st_ino) != (previous.st_dev, previous.st_ino):
                        raise RuntimeError('本机配对入口正在变化') from None
                    self.path.unlink()
                except OSError:
                    raise RuntimeError('无法核验现有本机配对入口') from None
                else:
                    raise RuntimeError('此项目已有本机配对服务')
        # Passing path= would let asyncio unlink a socket installed after our probe.
        # Bind ourselves so a competing owner wins safely with EADDRINUSE.
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.bind(str(self.path))
            current = self.path.lstat()
            self.identity = (current.st_dev, current.st_ino)
            os.chmod(self.path, 0o600, follow_symlinks=False)
            listener.setblocking(False)
            options = {'cleanup_socket': False} if sys.version_info >= (3, 13) else {}
            self.server = await asyncio.start_unix_server(self._accept, sock=listener, limit=MAX_FRAME,
                                                        start_serving=False, **options)
            await self.server.start_serving()
        except BaseException:
            try:
                if self.server is not None:
                    await self._close()
            finally:
                listener.close()
                self._unlink_owned()
            raise
        return self

    async def _accept(self, reader, writer):
        task = asyncio.current_task()
        self.clients.add(task)
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT):
                if self._closing:
                    raise ValueError('closing')
                if peer_uid(writer.get_extra_info('socket')) != os.getuid():
                    raise ValueError('peer_denied')
                frame = await reader.readline()
                if len(frame) > MAX_FRAME or not frame.endswith(b'\n'):
                    raise ValueError('invalid_request')
                body = json.loads(frame.decode('utf-8', errors='replace'))
                if (not isinstance(body, dict) or set(body) != {'protocol', 'client', 'workspace'}
                        or type(body.get('protocol')) is not int or body.get('protocol') != 1
                        or body.get('client') != 'xiaoyu-cli' or body.get('workspace') != self.workspace):
                    raise ValueError('invalid_request')
                writer.write(self._frame)
                await writer.drain()
        except (OSError, ValueError, TimeoutError):
            # Do not reflect request data, credentials or parser details.
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    writer.write(b'{"protocol":1,"error":"pairing_denied"}\n')
                    await writer.drain()
            except (OSError, TimeoutError):
                pass
        finally:
            writer.close()
            try:
                async with asyncio.timeout(CLOSE_TIMEOUT):
                    await writer.wait_closed()
            except (OSError, TimeoutError):
                writer.transport.abort()
            self.clients.discard(task)

    async def close(self):
        async with self._lifecycle:
            await self._close()

    async def _close(self):
        self._closing = True
        server, self.server = self.server, None
        if server:
            server.close()
        for task in list(self.clients):
            task.cancel()
        try:
            async with asyncio.timeout(CLOSE_TIMEOUT * 2):
                if self.clients:
                    await asyncio.gather(*list(self.clients), return_exceptions=True)
                if server:
                    await server.wait_closed()
        finally:
            self._unlink_owned()
