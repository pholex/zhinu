"""Desktop-owned connection configuration and session-scoped SDK leases.

OAuth tokens, redirects and operation receipts are deliberately memory-only.
User-entered static headers and environment values belong to saved configuration.
The service must hold its management_guard across each mutating registry call.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import stat
import threading
import time
from urllib.parse import urlsplit

from labs.desktop.sdk_runtime import ensure_sdk
ensure_sdk()
from xiaoyu_agent_sdk.mcp import McpPool
from xiaoyu_agent_sdk.oauth import OAuthClient, MemoryTokenStore
from xiaoyu_agent_sdk.types import McpServer


class ConnectionError(ValueError):
    pass


class ConnectionConflict(ConnectionError):
    pass


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', value):
        raise ConnectionError('连接标识不正确')
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def public_url(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ConnectionError('连接地址不正确')
    try:
        parts = urlsplit(value)
        valid = (parts.scheme == 'https' or (parts.scheme == 'http' and parts.hostname in
                 {'localhost', '127.0.0.1', '::1'})) and parts.hostname
        if not valid or parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError()
        parts.port
    except ValueError:
        raise ConnectionError('地址须为 HTTPS 或本机 HTTP，且不含凭据、查询参数或片段') from None
    return value


def env_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,127}', value):
        raise ConnectionError('请填写有效的环境变量名称，不是变量值')
    return value


def string_map(value, *, headers=False, references=False):
    if not isinstance(value, dict) or len(value) > 64:
        raise ConnectionError('键值配置不正确，最多 64 项')
    result, names = {}, set()
    for key, text in value.items():
        if headers:
            if not isinstance(key, str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}", key):
                raise ConnectionError('请求头名称不正确')
            if key.lower() in names or key.lower() in {'host', 'content-length', 'connection', 'transfer-encoding', 'mcp-session-id', 'mcp-protocol-version'}:
                raise ConnectionError('请求头名称重复或由传输协议管理')
            names.add(key.lower())
        else:
            env_name(key)
        if not isinstance(text, str) or len(text) > 4096 or '\0' in text or headers and any(ord(c) < 32 or ord(c) > 126 for c in text):
            raise ConnectionError('配置值格式不正确')
        if references:
            env_name(text)
        result[key] = text
    return result


def runtime_options(config):
    def read(name):
        value = os.environ.get(name)
        if value is None or value == '':
            raise ConnectionConflict(f'应用环境中没有设置 {name}，请设置后重新打开应用')
        return value
    headers = dict(config.get('headers', {}))
    headers.update({key: read(name) for key, name in config.get('env_headers', {}).items()})
    if config.get('bearer_token_env_var'):
        headers['Authorization'] = 'Bearer ' + read(config['bearer_token_env_var'])
    try:
        string_map(headers, headers=True)
    except ConnectionError:
        raise ConnectionConflict('请求头环境变量的值格式不正确，请检查变量配置') from None
    env = {name: read(name) for name in config.get('inherit_env', [])}
    env.update(config.get('env', {}))
    cwd = config.get('cwd', '')
    if cwd:
        cwd = str(Path(cwd).expanduser().absolute())
        if not Path(cwd).is_dir():
            raise ConnectionConflict('工作目录不存在或不是文件夹，请修改连接配置')
    return dict(headers=headers, env=env, cwd=cwd)


def definition(value):
    if not isinstance(value, dict) or set(value) - {'name', 'transport', 'url', 'command', 'args', 'timeout', 'oauth', 'headers', 'env_headers', 'bearer_token_env_var', 'env', 'inherit_env', 'cwd'}:
        raise ConnectionError('连接配置含不支持的字段')
    name = value.get('name', '')
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ConnectionError('请填写连接名称')
    timeout = value.get('timeout', 30)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise ConnectionError('连接超时须为 1–120 秒')
    result = {'name': name.strip(), 'transport': value.get('transport'), 'timeout': timeout}
    if result['transport'] == 'http':
        if any(k in value for k in ('command', 'args', 'env', 'inherit_env', 'cwd')):
            raise ConnectionError('HTTP 连接不可包含本机命令')
        result['url'] = public_url(value.get('url'))
        used = set()
        for field in ('headers', 'env_headers'):
            entries = string_map(value.get(field, {}), headers=True, references=field == 'env_headers')
            for key in entries:
                if key.lower() in used:
                    raise ConnectionError('请求头名称重复')
                used.add(key.lower())
            if entries:
                result[field] = entries
        bearer = value.get('bearer_token_env_var', '')
        if bearer:
            env_name(bearer)
            result['bearer_token_env_var'] = bearer
        if (bearer and 'authorization' in used) or (value.get('oauth') and (bearer or 'authorization' in used)):
            raise ConnectionError('Bearer、Authorization 请求头与 OAuth 请只选择一种授权方式')
        if 'oauth' in value:
            oauth = value['oauth']
            if not isinstance(oauth, dict) or set(oauth) - {'client_id', 'authorization_endpoint', 'token_endpoint', 'scope', 'revocation_endpoint', 'issuer'}:
                raise ConnectionError('OAuth 配置不正确；只支持公开客户端')
            client_id = oauth.get('client_id')
            if not isinstance(client_id, str) or not client_id or len(client_id) > 512:
                raise ConnectionError('OAuth client_id 不正确')
            clean = {'client_id': client_id}
            for key in ('authorization_endpoint', 'token_endpoint'):
                clean[key] = public_url(oauth.get(key))
            for key in ('revocation_endpoint', 'issuer'):
                if oauth.get(key):
                    clean[key] = public_url(oauth[key])
            scope = oauth.get('scope', '')
            if not isinstance(scope, str) or len(scope) > 2000:
                raise ConnectionError('OAuth scope 不正确')
            clean['scope'] = scope
            result['oauth'] = clean
    elif result['transport'] == 'stdio':
        if any(k in value for k in ('url', 'oauth', 'headers', 'env_headers', 'bearer_token_env_var')):
            raise ConnectionError('本机进程不支持此 OAuth 配置')
        command, args = value.get('command'), value.get('args', [])
        if not isinstance(command, str) or not command or len(command) > 4096 or '\0' in command:
            raise ConnectionError('请明确填写要执行的命令')
        if not isinstance(args, list) or len(args) > 64 or any(not isinstance(a, str) or len(a) > 4096 or '\0' in a for a in args):
            raise ConnectionError('命令参数不正确')
        result.update(command=command, args=args)
        env = string_map(value.get('env', {}))
        inherited = value.get('inherit_env', [])
        if not isinstance(inherited, list) or len(inherited) > 64:
            raise ConnectionError('环境变量传递配置不正确')
        for key in inherited:
            env_name(key)
        if len(set(inherited)) != len(inherited) or set(inherited) & set(env):
            raise ConnectionError('环境变量名称重复')
        cwd = value.get('cwd', '')
        if not isinstance(cwd, str) or len(cwd) > 4096 or '\0' in cwd:
            raise ConnectionError('工作目录不正确')
        if env:
            result['env'] = env
        if inherited:
            result['inherit_env'] = inherited
        if cwd:
            result['cwd'] = cwd
    else:
        raise ConnectionError('连接类型须为 http 或 stdio')
    return result


class _EphemeralTokens:
    """SDK memory store with irreversible local close, including late refreshes."""
    def __init__(self):
        self.memory = MemoryTokenStore()
        self.lock = threading.Lock()
        self.sealed = False

    def load(self):
        with self.lock:
            return self.memory.load() if not self.sealed else None

    def save(self, tokens):
        with self.lock:
            if self.sealed:
                raise ConnectionError('授权已关闭')
            self.memory.save(tokens)

    def clear(self):
        with self.lock:
            self.memory.clear()

    def seal(self):
        with self.lock:
            self.sealed = True
            self.memory.clear()


class ConnectionStore:
    def __init__(self, path: Path, legacy_path: Path | None = None):
        self.path = Path(path)
        self.legacy_path = legacy_path
        self.lock = threading.RLock()
        self.guard = lambda: None

    def read(self):
        with self.lock:
            try:
                fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                if self.legacy_path:
                    return ConnectionStore(self.legacy_path).read()
                return {'revision': 0, 'connections': []}
            with os.fdopen(fd, 'rb') as stream:
                meta = os.fstat(stream.fileno())
                if not stat.S_ISREG(meta.st_mode) or meta.st_nlink != 1 or meta.st_size > 1024 * 1024:
                    raise ConnectionError('连接配置文件不可安全读取')
                data = json.loads(stream.read(1024 * 1024 + 1).decode('utf-8', errors='replace'))
            if not isinstance(data, dict) or set(data) != {'revision', 'connections'} or type(data['revision']) is not int or data['revision'] < 0 or not isinstance(data['connections'], list) or len(data['connections']) > 64:
                raise ConnectionError('连接配置文件已损坏')
            seen = set()
            for item in data['connections']:
                if not isinstance(item, dict) or set(item) != {'id', 'revision', 'definition'}:
                    raise ConnectionError('连接配置记录已损坏')
                identifier(item['id'])
                if item['id'] in seen or type(item['revision']) is not int or item['revision'] < 1:
                    raise ConnectionError('连接配置记录已损坏')
                seen.add(item['id'])
                item['definition'] = definition(item['definition'])
            return data

    def _save(self, state):
        self.guard()
        encoded = json.dumps(state, ensure_ascii=False).encode('utf-8')
        if len(encoded) > 1024 * 1024:
            raise ConnectionError('连接配置总量超出限制')
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = self.path.with_name(self.path.name + '.' + secrets.token_hex(8))
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(state, stream, ensure_ascii=False)
                stream.flush()
                os.fsync(stream.fileno())
            self.guard()
            os.replace(temporary, self.path)
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    def configure(self, revision, connection_id, value):
        identifier(connection_id)
        value = definition(value)
        with self.lock:
            state = self.read()
            old = next((c for c in state['connections'] if c['id'] == connection_id), None)
            if old and old['definition'] == value:
                return state
            if type(revision) is not int or revision != state['revision']:
                raise ConnectionConflict('连接配置已变化，请重新读取')
            if old:
                old.update(definition=value, revision=state['revision'] + 1)
            else:
                if len(state['connections']) >= 64:
                    raise ConnectionConflict('连接数量已达上限')
                state['connections'].append({'id': connection_id, 'revision': state['revision'] + 1, 'definition': value})
            state['revision'] += 1
            self._save(state)
            return state

    def get(self, connection_id):
        identifier(connection_id)
        for item in self.read()['connections']:
            if item['id'] == connection_id:
                return item
        raise KeyError(connection_id)

    def delete(self, revision, connection_id):
        identifier(connection_id)
        with self.lock:
            state = self.read()
            if not any(c['id'] == connection_id for c in state['connections']):
                return state
            if type(revision) is not int or revision != state['revision']:
                raise ConnectionConflict('连接配置已变化，请重新读取')
            state['connections'] = [c for c in state['connections'] if c['id'] != connection_id]
            state['revision'] += 1
            self._save(state)
            return state


def idle_reason(session):
    if getattr(session, 'closing', False):
        return '会话正在关闭'
    if getattr(session, 'busy', False):
        return '请等待当前轮次结束'
    tasks = (getattr(session, '_task', None), getattr(getattr(session, 'async_agent', None), '_task', None))
    if any(task is not None and not task.done() for task in tasks):
        return '当前执行仍在退出'
    if getattr(session, 'snapshot_pending', lambda: [])():
        return '会话仍有待处理审批'
    runtime = getattr(session.agent, 'chenshu', None)
    if runtime is not None:
        with runtime.lock:
            if any(t.is_alive() for t in runtime._threads.values()):
                return '后台成员仍在执行'
    view = getattr(session, 'fleets', None)
    if view is not None and (not getattr(view, 'integrity', True) or getattr(view, 'truncated', False)):
        return '后台成员记录不完整，无法确认空闲'
    fleets = getattr(view, 'fleets', {})
    # Stored read models retain uncertainty after restart; never infer idle from parent alone.
    for board in list(fleets.values()):
        if board.get('limits', {}).get('members', 0):
            return '后台成员记录被截断，无法确认空闲'
        for member in board.get('members', []):
            if member.get('execution_state') in {'running', 'starting', 'unknown'} or (member.get('status') == 'retired' and member.get('thread_alive') is True):
                return '后台成员仍在执行或状态未知'
    weaves = getattr(session, 'weaves', None)
    if weaves is not None:
        if not getattr(weaves, 'integrity', True) or getattr(weaves, 'truncated', False):
            return '并行任务记录不完整，无法确认空闲'
        if any(e.get('execution_state') in {'starting', 'running', 'unknown'}
               for e in list(getattr(weaves, 'executions', {}).values())):
            return '并行任务仍在执行或状态未知'
    controls = getattr(session.agent, '_weave_controls', None)
    if controls is not None:
        with controls._lock:
            if any(not e.closed for e in controls._executions.values()):
                return '并行任务仍在执行'
    return ''


class ConnectionRegistry:
    def __init__(self, store: ConnectionStore, origin: str, *, authorization_timeout=180):
        parts = urlsplit(public_url(origin))
        if parts.scheme != 'http' or parts.hostname not in {'127.0.0.1', '::1'} or parts.path not in ('', '/') or not parts.port:
            raise ConnectionError('授权回调须使用本机服务 origin')
        self.store, self.origin = store, origin.rstrip('/')
        self.authorization_timeout = authorization_timeout
        self.lock = threading.RLock()
        self.sessions = {}
        self.authorizations = {}
        self.credentials = {}
        self.closed = False
        self.guard = lambda: None

    def _lease(self, session):
        lease = self.sessions.get(session.id)
        if lease is None:
            existing = session.agent.toolbox.mcp_manager
            if existing is not None:
                raise ConnectionConflict('当前会话已有其他来源的连接，请使用独立会话管理')
            pool = McpPool()
            pool.acquire(session.id)
            lease = {'pool': pool, 'mounted': {}, 'receipts': {}, 'session': session}
            self.sessions[session.id] = lease
            toolbox = session.agent.toolbox
            toolbox._mcp = pool
            toolbox._mcp_search = True
            toolbox._register_mcp_search()
        return lease

    def _credential(self, session_id, item):
        return self.credentials.get((session_id, item['id'], item['revision']))

    def _declarations(self, lease, connection_id):
        pool = lease['pool']
        name = 'desktop_' + connection_id
        with pool._lock:
            manager = pool._managers.get(name)
            if manager is None:
                return [], '', ''
            with manager._lock:
                declared = copy.deepcopy(manager._declared.get(name, []))
                quarantined = set(manager._quarantined.get(name, []))
        if len(json.dumps(declared, ensure_ascii=False).encode('utf-8')) > 128 * 1024 or len(declared) > 256:
            return [], '', '工具声明超过展示限制；本界面不能批准，需减少服务工具数量后重连'
        tools = [{'name': d.get('name', ''), 'description': str(d.get('description', '')),
                  'inputSchema': d.get('inputSchema', {}), 'quarantined': d.get('name') in quarantined} for d in declared]
        return tools, digest(declared) if declared else '', ''

    def state(self, session, item):
        cid = item['id']
        lease = self.sessions.get(session.id)
        mounted = lease is not None and cid in lease['mounted']
        tools, fingerprint, declaration_warning = self._declarations(lease, cid) if mounted else ([], '', '')
        state = lease['pool'].server_states().get('desktop_' + cid, 'stopped') if mounted else 'unmounted'
        credential = self._credential(session.id, item)
        oauth_status = 'not_configured' if 'oauth' not in item['definition'] else 'unauthorized'
        if credential is not None and credential['store'].load() is not None:
            oauth_status = 'authorized'
        elif any(j['session_id'] == session.id and j['connection_id'] == cid and j['status'] in {'pending', 'authorizing'} for j in self.authorizations.values()):
            oauth_status = 'pending'
        return {'id': cid, 'configuration_revision': item['revision'], 'mounted': mounted,
                'state': state, 'tools': tools, 'declaration_fingerprint': fingerprint, 'declaration_warning': declaration_warning, 'oauth_status': oauth_status}

    def list(self, session):
        with self.lock:
            reason = idle_reason(session)
            return {'connections': [self.state(session, item) for item in self.store.read()['connections']],
                    'can_manage': not reason and not self.closed, 'reason': reason}

    def configure(self, revision, cid, value):
        with self.lock:
            if self.closed:
                raise ConnectionConflict('连接管理已关闭')
            normalized = definition(value)
            try:
                old = self.store.get(cid)
            except KeyError:
                old = None
            if old and old['definition'] != normalized and self._in_use(cid):
                raise ConnectionConflict('请先解除所有会话的挂载并取消授权，再修改配置')
            result = self.store.configure(revision, cid, normalized)
            if old and old['definition'] != normalized:
                for key in [k for k in self.credentials if k[1] == cid]:
                    self.credentials.pop(key)['store'].seal()
            return result

    def _in_use(self, cid):
        return any(cid in l['mounted'] for l in self.sessions.values()) or any(j['connection_id'] == cid and j['status'] in {'pending', 'authorizing'} for j in self.authorizations.values())

    def delete(self, revision, cid):
        with self.lock:
            if self.closed:
                raise ConnectionConflict('连接管理已关闭')
            if self._in_use(cid):
                raise ConnectionConflict('请先解除挂载并取消授权')
            result = self.store.delete(revision, cid)
            for key in [k for k in self.credentials if k[1] == cid]:
                self.credentials.pop(key)['store'].seal()
            return result

    def receipt(self, session_id, cid, body, operation_id):
        identifier(operation_id)
        lease = self.sessions.get(session_id)
        receipt = lease['receipts'].get(operation_id) if lease else None
        if receipt is None:
            return None
        if receipt['signature'] != digest({'connection_id': cid, 'body': body}):
            raise ConnectionConflict('同一回执标识不可用于另一操作')
        return {**copy.deepcopy(receipt['result']), 'duplicate': True}

    def manage(self, session, cid, body, operation_id):
        identifier(operation_id)
        if not isinstance(body, dict) or set(body) - {'action', 'configuration_revision', 'expected_fingerprint'}:
            raise ConnectionError('连接管理请求格式不正确')
        action = body.get('action')
        if action not in {'mount', 'start', 'stop', 'remove', 'reconnect', 'approve_changes'}:
            raise ConnectionError('未知连接操作')
        signature = digest({'connection_id': cid, 'body': body})
        with self.lock:
            lease = self.sessions.get(session.id)
            receipt = lease['receipts'].get(operation_id) if lease else None
            if receipt:
                if receipt['signature'] != signature:
                    raise ConnectionConflict('同一回执标识不可用于另一操作')
                return {**copy.deepcopy(receipt['result']), 'duplicate': True}
            self.guard()
            if self.closed or idle_reason(session):
                raise ConnectionConflict(idle_reason(session) or '连接管理已关闭')
            item = self.store.get(cid)
            if type(body.get('configuration_revision')) is not int or body['configuration_revision'] != item['revision']:
                raise ConnectionConflict('连接配置已变化')
            options = runtime_options(item['definition']) if action == 'mount' else {}
            if action == 'mount' and item['definition']['transport'] == 'stdio':
                from xiaoyu.mcp import find_command
                if find_command(item['definition']['command'], options.get('env')) is None:
                    raise ConnectionConflict('找不到连接器启动命令；请安装所需运行环境，或在配置中填写命令绝对路径或 PATH')
            lease = self._lease(session)
            if len(lease['receipts']) >= 1000:
                raise ConnectionConflict('本会话连接操作回执已达上限')
            if action != 'mount' and cid not in lease['mounted']:
                raise ConnectionConflict('当前会话尚未挂载此连接')
            pool, name = lease['pool'], 'desktop_' + cid
            lease['receipts'][operation_id] = {'signature': signature, 'result': {'status': 'unknown', 'warning': '操作可能已执行，请读取状态核对', 'duplicate': False}}
            try:
                if action == 'mount':
                    if cid in lease['mounted']:
                        raise ConnectionConflict('此连接已挂载')
                    config = item['definition']
                    credential = self._credential(session.id, item)
                    if config.get('oauth') and (credential is None or credential['store'].load() is None):
                        raise ConnectionConflict('请先完成当前会话的授权')
                    server = McpServer(name=name, url=config.get('url', ''), command=config.get('command', ''),
                        args=tuple(config.get('args', [])), timeout=config['timeout'], **options,
                        oauth=credential['client'] if credential else None)
                    # Mark ownership before add: a failing start can still own a child process.
                    lease['mounted'][cid] = item['revision']
                    try:
                        pool.add(server, _owner=session.id)
                    except Exception:
                        raise ConnectionConflict('连接启动未完成，请读取状态核对；不会自动重试') from None
                elif action == 'approve_changes':
                    with pool._lock:
                        manager = pool._managers.get(name)
                        if manager is None:
                            raise ConnectionConflict('连接尚未启动')
                        with manager._lock:
                            declared = manager._declared.get(name, [])
                            if (not declared or len(declared) > 256
                                or len(json.dumps(declared, ensure_ascii=False).encode('utf-8')) > 128 * 1024
                                or body.get('expected_fingerprint') != digest(declared)):
                                raise ConnectionConflict('工具声明已变化，请重新查看')
                            # SDK public approve delegates here but cannot atomically compare its
                            # declaration. Keep this private adapter under the same manager lock.
                            manager._approve_locked(name)
                            if manager._quarantined.get(name):
                                raise ConnectionConflict('工具声明批准未完成')
                else:
                    try:
                        getattr(pool, action)(name, _owner=session.id)
                    except Exception:
                        raise ConnectionConflict('连接操作未完成，请读取状态核对；不会自动重试') from None
                    if action == 'remove':
                        lease['mounted'].pop(cid, None)
            except Exception:
                raise ConnectionConflict('连接操作未完成，请读取状态核对；同一回执不会重新执行') from None
            result = {'connection': self.state(session, item), 'duplicate': False}
            lease['receipts'][operation_id] = {'signature': signature, 'result': copy.deepcopy(result)}
            return result

    def begin_authorization(self, session, cid):
        with self.lock:
            self.guard()
            if self.closed or getattr(session, 'closing', False):
                raise ConnectionConflict('会话已关闭')
            item = self.store.get(cid)
            config = item['definition']
            if not config.get('oauth'):
                raise ConnectionConflict('此连接未配置 OAuth')
            for aid, job in self.authorizations.items():
                if job['connection_id'] == cid and job['session_id'] == session.id and job['status'] in {'pending', 'authorizing'}:
                    return self.authorization(cid, aid)
            lease = self.sessions.get(session.id)
            if lease and cid in lease['mounted']:
                raise ConnectionConflict('请先解除挂载再重新授权')
            if len(self.authorizations) >= 256:
                raise ConnectionConflict('本次应用授权次数已达上限')
            aid = secrets.token_urlsafe(24)
            callback = self.origin + '/desktop/oauth/callback/' + aid
            store = _EphemeralTokens()
            client = OAuthClient(resource=config['url'], redirect_uri=callback, token_store=store, **config['oauth'])
            job = {'authorization_id': aid, 'connection_id': cid, 'session_id': session.id,
                'configuration_revision': item['revision'], 'status': 'authorizing',
                'expires_at': time.time() + self.authorization_timeout, 'url': '',
                'queue': queue.Queue(maxsize=1), 'callback': callback, 'store': store, 'client': client}
            if self.closed:
                raise ConnectionConflict('连接管理已关闭')
            self.authorizations[aid] = job
            worker = threading.Thread(target=self._authorize, args=(job,), daemon=True, name='desktop-oauth')
            job['thread'] = worker
            worker.start()
            return self.authorization(cid, aid)

    def _authorize(self, job):
        def browser(url):
            with self.lock:
                if self.closed or job['status'] != 'authorizing':
                    raise ConnectionError('授权已取消')
                job.update(status='pending', url=url)
            try:
                result = job['queue'].get(timeout=max(0, job['expires_at'] - time.time()))
            except queue.Empty:
                raise ConnectionError('授权已过期') from None
            if result is None:
                raise ConnectionError('授权已取消')
            return result
        try:
            job['client'].authorize(browser)
            with self.lock:
                self.guard()
                if self.closed or job['status'] in {'cancelled', 'expired'} or time.time() >= job['expires_at']:
                    job['store'].seal()
                    if job['status'] != 'cancelled':
                        job['status'] = 'expired'
                else:
                    job['status'] = 'authorized'
                    key = (job['session_id'], job['connection_id'], job['configuration_revision'])
                    old = self.credentials.get(key)
                    if old:
                        old['store'].seal()
                    self.credentials[key] = {'client': job['client'], 'store': job['store']}
        except Exception:
            with self.lock:
                job['store'].seal()
                if job['status'] not in {'cancelled', 'expired'}:
                    job['status'] = 'expired' if time.time() >= job['expires_at'] else 'failed'
        finally:
            with self.lock:
                job['url'] = ''

    def authorization(self, cid, aid):
        with self.lock:
            job = self.authorizations.get(aid)
            if job is None or job['connection_id'] != cid:
                raise KeyError(aid)
            return {key: job[key] for key in ('authorization_id', 'status', 'expires_at')}

    def open_url(self, cid, aid):
        with self.lock:
            self.authorization(cid, aid)
            job = self.authorizations[aid]
            if job['status'] != 'pending' or time.time() >= job['expires_at']:
                raise ConnectionConflict('授权链接已失效')
            return {'url': job['url']}

    def callback(self, aid, query):
        with self.lock:
            job = self.authorizations.get(aid)
            if job is None or job['status'] != 'pending' or time.time() >= job['expires_at']:
                raise ConnectionConflict('授权请求已失效')
            if not isinstance(query, str) or len(query) > 16384:
                raise ConnectionError('授权响应过长')
            job['status'] = 'authorizing'
            job['url'] = ''
            job['queue'].put_nowait(job['callback'] + '?' + query)

    def cancel(self, cid, aid):
        with self.lock:
            self.authorization(cid, aid)
            job = self.authorizations[aid]
            if job['status'] in {'pending', 'authorizing'}:
                job.update(status='cancelled', url='')
                job['store'].seal()
                try:
                    job['queue'].put_nowait(None)
                except queue.Full:
                    pass
            return self.authorization(cid, aid)

    def revoke(self, session, cid):
        with self.lock:
            if idle_reason(session):
                raise ConnectionConflict(idle_reason(session))
            lease = self.sessions.get(session.id)
            if lease and cid in lease['mounted']:
                raise ConnectionConflict('请先解除此连接的挂载再撤销授权')
            for aid, job in self.authorizations.items():
                if job['connection_id'] == cid and job['session_id'] == session.id:
                    self.cancel(cid, aid)
            credentials = [self.credentials.pop(k) for k in list(self.credentials) if k[:2] == (session.id, cid)]
        remote_revoked = True
        for credential in credentials:
            try:
                credential['client'].revoke()
            except Exception:
                remote_revoked = False
            finally:
                credential['store'].seal()
        return {'revoked': True, 'remote_revoked': remote_revoked}

    def close_session(self, session_id):
        with self.lock:
            for aid, job in self.authorizations.items():
                if job['session_id'] == session_id:
                    self.cancel(job['connection_id'], aid)
            for key in [k for k in self.credentials if k[0] == session_id]:
                self.credentials.pop(key)['store'].seal()
            lease = self.sessions.get(session_id)
            if lease:
                lease['pool'].close(_owner=session_id)
                if not lease['pool'].shutdown_pending():
                    lease['pool'].release(session_id)
                    self.sessions.pop(session_id)
                else:
                    return True
            return False

    def begin_shutdown(self):
        # No registry/pool/network lock: a management worker may be stuck while
        # the service deliberately retains its resources until process exit.
        self.closed = True
        for job in list(self.authorizations.values()):
            if job['status'] in {'pending', 'authorizing'}:
                job.update(status='cancelled', url='')
                try:
                    job['queue'].put_nowait(None)
                except queue.Full:
                    pass
            job['store'].seal()
        for credential in list(self.credentials.values()):
            credential['store'].seal()

    def close(self):
        self.begin_shutdown()
        with self.lock:
            ids = {j['session_id'] for j in self.authorizations.values()} | set(self.sessions)
        for sid in ids:
            self.close_session(sid)
