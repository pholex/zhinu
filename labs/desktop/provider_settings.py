"""Edit core user provider declarations; credentials are write-only in the GUI."""
from __future__ import annotations
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import threading
from urllib.parse import urlsplit

from .preferences import PreferenceConflict
from xiaoyu.session_log import SessionWriteLock, SessionLockedError

LIMIT = 1024 * 1024


class ProviderSettings:
    def __init__(self, *, write_guard=None, path=None):
        from xiaoyu.config import user_env_path
        self.path = Path(path) if path is not None else user_env_path()
        self.write_guard = write_guard or (lambda: None)
        self.lock = threading.RLock()

    def _read(self):
        from xiaoyu.config import dotenv_value
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError('核心配置必须是独立普通文件')
                raw = stream.read(LIMIT + 1)
            if len(raw) > LIMIT:
                raise ValueError('核心配置超过编辑大小限制')
        except FileNotFoundError:
            raw = b''
        values = {}
        for line in raw.decode('utf-8', errors='replace').splitlines():
            line = line.strip()
            if line.startswith('export '): line = line[7:]
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = dotenv_value(value)
        return hashlib.sha256(raw).hexdigest(), values

    def read(self):
        with self.lock:
            revision, values = self._read()
            effective = {**values, **os.environ}
            names = sorted({m.group(1) for k in effective if (m := re.fullmatch(r'XIAOYU_PROVIDER_([A-Z][A-Z0-9_]*)_BASE_URL', k))})
            rows = []
            for name in names:
                prefix = 'XIAOYU_PROVIDER_' + name + '_'
                url = effective.get(prefix+'BASE_URL', '')
                try:
                    parsed = urlsplit(url)
                    safe = bool(parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment)
                except ValueError:
                    safe = False
                rows.append({'id': name.lower(), 'base_url': url if safe else '',
                    'endpoint_hidden': not safe, 'models': [x.strip() for x in effective.get(prefix+'MODELS','').split(',') if x.strip()],
                    'protocol': effective.get(prefix+'PROTOCOL','chat') or 'chat',
                    'has_key': bool(effective.get(prefix+'API_KEY','')), 'saved_has_key': bool(values.get(prefix+'API_KEY','')),
                    'source': 'process_environment' if any(k.startswith(prefix) for k in os.environ) else 'user_configuration',
                    'saved': prefix+'BASE_URL' in values})
            return {'revision': revision, 'providers': rows, 'write_target': 'core_user_configuration',
                    'scope': 'after_service_restart', 'warning': '写入核心用户配置；重启服务后新会话生效。进程环境、工作区配置或显式服务选项可能优先，保存不代表当前路由已改变。密钥不回显。'}

    def save(self, revision, value):
        from xiaoyu.config import dotenv_line
        if not isinstance(value, dict) or set(value) - {'id','base_url','models','protocol','api_key_action','api_key'} or not {'id','base_url','models','protocol','api_key_action'} <= set(value):
            raise ValueError('服务商配置字段不正确')
        name = value['id']
        if not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', name):
            raise ValueError('服务商标识仅支持小写字母、数字和下划线')
        from xiaoyu.providers import PRESETS, GATEWAY, SCRIPTED_PROVIDER
        if name in PRESETS or name in {GATEWAY, SCRIPTED_PROVIDER}:
            raise ValueError('内置服务商使用核心配置入口；此表单用于自定义服务商')
        url = value['base_url']
        if not isinstance(url, str) or len(url) > 2000:
            raise ValueError('端点格式不正确')
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {'https','http'} and parsed.hostname and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
        except ValueError:
            valid = False
        if not valid or any(c.isspace() for c in url):
            raise ValueError('端点须为无用户信息、查询凭据或片段的 HTTP(S) 地址')
        models = value['models']
        if (not isinstance(models, list) or not 1 <= len(models) <= 200
                or any(not isinstance(m,str) or not m.strip() or len(m)>200 or ',' in m or '\n' in m or m.strip().lower()=='auto' for m in models)):
            raise ValueError('请显式填写 1–200 个模型名称，不自动发现模型')
        if not isinstance(value['protocol'], str) or value['protocol'] not in {'chat','responses','anthropic'}:
            raise ValueError('协议不正确')
        action = value['api_key_action']
        if not isinstance(action, str) or action not in {'keep','set','clear'} or (action=='set' and (not isinstance(value.get('api_key'),str) or not value['api_key'].strip() or len(value['api_key'])>16000)):
            raise ValueError('密钥操作不正确')
        if action != 'set' and value.get('api_key'):
            raise ValueError('保留或清除密钥时不接受新密钥')
        with self.lock:
            self.write_guard()
            try:
                lease = SessionWriteLock(self.path)
            except SessionLockedError:
                raise PreferenceConflict() from None
            with closing(lease):
                current, values = self._read()
                if not isinstance(revision,str) or revision != current:
                    raise PreferenceConflict()
                prefix = 'XIAOYU_PROVIDER_' + name.upper() + '_'
                values.update({prefix+'BASE_URL':url,prefix+'MODELS':','.join(m.strip() for m in models),prefix+'PROTOCOL':value['protocol']})
                if action != 'keep': values[prefix+'API_KEY'] = value.get('api_key','') if action=='set' else ''
                body = ('\n'.join(dotenv_line(k,v) for k,v in values.items())+'\n').encode('utf-8')
                if len(body)>LIMIT: raise ValueError('配置超过大小限制')
                self.write_guard();self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                fd, temporary = tempfile.mkstemp(prefix='.provider-',dir=self.path.parent)
                try:
                    with os.fdopen(fd,'wb') as stream:
                        self.write_guard();stream.write(body);stream.flush();os.fsync(stream.fileno())
                    if self._read()[0] != revision: raise PreferenceConflict()
                    self.write_guard();os.replace(temporary,self.path)
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
                return self.read()
