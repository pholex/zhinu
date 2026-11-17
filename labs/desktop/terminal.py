"""User-operated desktop PTYs, separate from the model's tool execution surface."""
from __future__ import annotations

import codecs
from collections import deque, OrderedDict
import hashlib
import os
from pathlib import Path
import signal
import select
import struct
import subprocess
import sys
import threading
import time
import uuid


class TerminalConflict(ValueError):
    pass


def dimensions(columns, rows):
    if type(columns) is not int or type(rows) is not int or not 20 <= columns <= 300 or not 5 <= rows <= 150:
        raise ValueError('终端尺寸须为 20–300 列、5–150 行')
    return columns, rows


class Terminal:
    def __init__(self, session_id, workspace, shell, columns, rows):
        import fcntl
        import pty
        import termios
        dimensions(columns, rows)
        executable = {'zsh': '/bin/zsh', 'bash': '/bin/bash'}.get(shell)
        if executable is None or not Path(executable).is_file():
            raise ValueError('此系统不支持所选 shell')
        self.id = uuid.uuid4().hex
        self.session_id, self.workspace, self.shell = session_id, str(workspace), shell
        self.lock = threading.RLock()
        self.input_lock = threading.Lock()
        self.chunks = deque()
        self.bytes = 0
        self.next_seq = 1
        self.input_next = 1
        self.input_hashes = OrderedDict()
        self.state, self.exit_code = 'running', None
        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        # Explicit GUI action starts an ordinary user shell, never an agent command.
        # Do not inherit model/API credentials or application bearer tokens.
        env = {key: os.environ[key] for key in ('HOME', 'USER', 'LOGNAME', 'LANG', 'LC_CTYPE', 'TMPDIR') if key in os.environ}
        env.update(PATH='/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin',
                   TERM='xterm-256color', COLORTERM='truecolor')
        try:
            self.process = subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()),
                '--pty-child', executable], cwd=workspace, env=env, stdin=slave, stdout=slave,
                stderr=slave, start_new_session=True, close_fds=True)
        except BaseException:
            os.close(self.master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(self.master, False)
        self.reader = threading.Thread(target=self._read, daemon=True, name='desktop-pty-reader')
        self.reader.start()

    def _read(self):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while True:
                try:
                    if not select.select([self.master], [], [], .2)[0]:
                        if self.process.poll() is not None:
                            break
                        continue
                    raw = os.read(self.master, 16384)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not raw:
                    break
                text = decoder.decode(raw)
                with self.lock:
                    if text:
                        self.chunks.append({'seq': self.next_seq, 'text': text})
                        self.next_seq += 1
                        self.bytes += len(text.encode('utf-8'))
                        while self.bytes > 512 * 1024 or len(self.chunks) > 1024:
                            self.bytes -= len(self.chunks.popleft()['text'].encode('utf-8'))
        finally:
            code = self.process.wait()
            with self.lock:
                self.exit_code, self.state = code, 'exited'
                try:
                    os.close(self.master)
                except OSError:
                    pass

    def info(self):
        with self.lock:
            return {'id': self.id, 'session_id': self.session_id, 'workspace': self.workspace,
                'shell': self.shell, 'pid': self.process.pid, 'state': self.state,
                'exit_code': self.exit_code, 'first_seq': self.chunks[0]['seq'] if self.chunks else self.next_seq,
                'next_seq': self.next_seq, 'input_next': self.input_next, 'source': 'user'}

    def output(self, cursor):
        if type(cursor) is not int or cursor < 1:
            raise ValueError('输出游标不正确')
        with self.lock:
            value = self.info()
            chunks = [dict(c) for c in self.chunks if c['seq'] >= cursor][:128]
            value.update(chunks=chunks, truncated=cursor < value['first_seq'],
                         next_seq=chunks[-1]['seq'] + 1 if chunks else max(cursor, value['first_seq']))
            return value

    def write(self, text, input_id):
        if not isinstance(text, str) or not text or len(text.encode('utf-8')) > 32768 or type(input_id) is not int or input_id < 1:
            raise ValueError('终端输入不正确或过长')
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        with self.input_lock:
            with self.lock:
                if input_id < self.input_next:
                    if input_id in self.input_hashes and self.input_hashes[input_id] != digest:
                        raise TerminalConflict('此输入编号已用于不同内容；请核对当前终端')
                    return {'duplicate': True, 'input_next': self.input_next}
                if input_id != self.input_next or self.state != 'running':
                    raise TerminalConflict('输入顺序或进程状态已变化，请核对后继续')
                # Reserve before partial write; uncertain input is never replayed.
                self.input_hashes[input_id] = digest
                self.input_next += 1
                while len(self.input_hashes) > 256:
                    self.input_hashes.popitem(last=False)
            pending = text.encode('utf-8')
            deadline = time.monotonic() + 2
            while pending:
                if time.monotonic() >= deadline:
                    raise OSError('终端输入背压；部分输入可能已发送，不能重放')
                if not select.select([], [self.master], [], .1)[1]:
                    continue
                try:
                    size = os.write(self.master, pending)
                except BlockingIOError:
                    continue
                pending = pending[size:]
            return {'duplicate': False, 'input_next': self.input_next}

    def resize(self, columns, rows):
        import fcntl
        import termios
        dimensions(columns, rows)
        with self.lock:
            if self.state != 'running':
                raise TerminalConflict('终端进程已退出')
            fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', rows, columns, 0, 0))
        return self.info()

    def stop(self):
        with self.lock:
            if self.state != 'running':
                return self.info()
            self.state = 'closing'
            pid = self.process.pid
        def terminate():
            # Stop the foreground job as well as the shell's original process group.
            groups = {pid}
            try:
                groups.add(os.tcgetpgrp(self.master))
            except OSError:
                pass
            for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGKILL):
                for group in groups:
                    if group > 1:
                        try:
                            os.killpg(group, sig)
                        except ProcessLookupError:
                            pass
                time.sleep(.15)
        self.stop_thread = threading.Thread(target=terminate, daemon=True, name='desktop-pty-stop')
        self.stop_thread.start()
        return self.info()


class TerminalRegistry:
    def __init__(self):
        self.lock = threading.RLock()
        self.terminals = {}
        self.receipts = {}
        self.closed = False

    def create(self, key, session_id, workspace, shell, columns, rows):
        if not isinstance(key, str) or not 1 <= len(key) <= 120:
            raise ValueError('创建终端需要稳定请求标识')
        signature = (session_id, str(workspace), shell, columns, rows)
        with self.lock:
            if key in self.receipts:
                old, tid = self.receipts[key]
                if old != signature:
                    raise TerminalConflict('此请求标识已用于另一终端')
                return {**self.terminals[tid].info(), 'duplicate': True}
            if self.closed or len(self.terminals) >= 32 or sum(t.state != 'exited' for t in self.terminals.values()) >= 8:
                raise TerminalConflict('终端数量已达上限或服务正在退出')
            terminal = Terminal(session_id, workspace, shell, columns, rows)
            self.terminals[terminal.id] = terminal
            self.receipts[key] = signature, terminal.id
            return {**terminal.info(), 'duplicate': False}

    def get(self, tid):
        with self.lock:
            return self.terminals[tid]

    def close(self):
        with self.lock:
            self.closed = True
            terminals = list(self.terminals.values())
        for terminal in terminals:
            terminal.stop()
        deadline = time.monotonic() + 1.5
        for terminal in terminals:
            terminal.reader.join(timeout=max(0, deadline - time.monotonic()))
            stopper = getattr(terminal, 'stop_thread', None)
            if stopper is not None:
                stopper.join(timeout=max(0, deadline - time.monotonic()))


def install_terminal_routes(app, token, root):
    import asyncio
    from contextlib import asynccontextmanager
    import json
    import secrets
    from fastapi import HTTPException, Request
    from starlette.responses import JSONResponse
    registry = TerminalRegistry()
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            try:
                yield
            finally:
                await asyncio.to_thread(registry.close)
    app.router.lifespan_context = lifespan

    def authorize(request, write=False, safety_stop=False):
        if not secrets.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + token).encode()):
            raise HTTPException(401, '需要本机应用令牌')
        if write:
            if request.headers.get('x-xiaoyu-instance') != getattr(app.state, 'instance_id', None):
                raise HTTPException(409, '服务实例已变化；终端操作不会重放')
            owner = getattr(app.state, 'execution_owner', None)
            if not safety_stop and (owner is None or not owner.held):
                raise HTTPException(503, '服务已失去工作区所有权')

    async def body(request, fields, safety_stop=False):
        authorize(request, True, safety_stop)
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 131072:
                raise HTTPException(413, '终端请求过长')
        try:
            data = json.loads(raw.decode('utf-8', errors='replace'))
            if not isinstance(data, dict) or set(data) != fields:
                raise ValueError()
            return data
        except ValueError:
            raise HTTPException(422, '终端请求格式不正确') from None

    def response(operation):
        try:
            return JSONResponse(operation(), headers={'Cache-Control': 'no-store'})
        except KeyError:
            raise HTTPException(404, '终端不存在；重启后不会恢复活进程') from None
        except TerminalConflict as error:
            raise HTTPException(409, str(error)) from None
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
        except OSError:
            raise HTTPException(503, '终端输入或进程状态未知，请先核对，不要重复发送') from None

    async def listing(request):
        authorize(request)
        return response(lambda: {'instance_id': app.state.instance_id,
            'terminals': [t.info() for t in list(registry.terminals.values())]})

    async def create(request):
        data = await body(request, {'session_id', 'shell', 'columns', 'rows'})
        sid = data['session_id']
        session = app.state.sessions.get(sid) if isinstance(sid, str) else None
        if session is None or session.closing:
            raise HTTPException(409, '请选择一个有效会话作为终端的工作目录来源')
        workspace = session.workspace.resolve(strict=True)
        if not workspace.is_dir() or not workspace.is_relative_to(Path(root).resolve()):
            raise HTTPException(403, '终端工作目录不属于当前工作台')
        return await asyncio.to_thread(response, lambda: registry.create(request.headers.get('idempotency-key'), sid,
            workspace, data['shell'], data['columns'], data['rows']))

    async def output(request, terminal_id):
        authorize(request)
        return response(lambda: registry.get(terminal_id).output(int(request.query_params.get('from', '1'))))

    async def write(request, terminal_id):
        data = await body(request, {'text', 'input_id'})
        return await asyncio.to_thread(response, lambda: registry.get(terminal_id).write(data['text'], data['input_id']))

    async def resize(request, terminal_id):
        data = await body(request, {'columns', 'rows'})
        return response(lambda: registry.get(terminal_id).resize(data['columns'], data['rows']))

    async def stop(request, terminal_id):
        data = await body(request, {'signal'}, safety_stop=True)
        if data['signal'] != 'terminate':
            raise HTTPException(422, '仅支持停止本终端进程')
        return response(lambda: registry.get(terminal_id).stop())

    for endpoint, path, method in [(listing, '/desktop/terminals', 'GET'), (create, '/desktop/terminals', 'POST'),
        (output, '/desktop/terminals/{terminal_id}/output', 'GET'), (write, '/desktop/terminals/{terminal_id}/input', 'POST'),
        (resize, '/desktop/terminals/{terminal_id}/resize', 'POST'), (stop, '/desktop/terminals/{terminal_id}/stop', 'POST')]:
        endpoint.__annotations__['request'] = Request
        app.add_api_route(path, endpoint, methods=[method], include_in_schema=False)
    return registry


def _child():
    import fcntl
    import termios
    shell = sys.argv[2]
    if shell not in {'/bin/zsh', '/bin/bash'}:
        raise SystemExit(2)
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    args = [shell, '-f', '-i'] if shell.endswith('zsh') else [shell, '--noprofile', '--norc', '-i']
    os.execv(shell, args)


if __name__ == '__main__' and len(sys.argv) == 3 and sys.argv[1] == '--pty-child':
    _child()
