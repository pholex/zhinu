"""Desktop preferences: explicit defaults for future sessions, never live grants."""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
import stat
import tempfile
import threading
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_BYTES = 512 * 1024
NAV = ['projects', 'library', 'capabilities', 'spaces', 'automations']
SHORTCUTS = {'new_task': 'mod+n', 'search': 'mod+k', 'settings': 'mod+,', 'sidebar': 'mod+b', 'panel': 'mod+shift+b'}
RESERVED_KEYS = frozenset('acvxyzqwhmrltsfp')
DEFAULTS = {
    'default_model': '', 'default_mode': '', 'timezone': 'UTC',
    'notifications': {k: True for k in ('completed', 'failed', 'approval', 'question', 'automation')},
    'ui': {'theme': 'system', 'font_size': 14, 'send_key': 'enter', 'reduced_motion': False,
           'show_planned': True, 'sidebar_hidden': False, 'panel_width': 420, 'nav_order': NAV,
           'hidden_nav': [], 'default_workflow': 'auto', 'session_sort': 'updated', 'session_meta': {}, 'shortcuts': SHORTCUTS},
}


class PreferenceConflict(Exception):
    pass



def validate_shortcuts(value):
    if not isinstance(value, dict) or set(value) != set(SHORTCUTS):
        raise ValueError('快捷键动作列表不正确')
    normalized = {}
    seen = set()
    for action, binding in value.items():
        if not isinstance(binding, str) or len(binding) > 40:
            raise ValueError('快捷键格式不正确')
        binding = binding.strip().lower()
        if binding:
            if not re.fullmatch(r'mod\+(?:shift\+)?[a-z0-9,./]', binding):
                raise ValueError('快捷键须为 Mod(+Shift)+字母、数字、逗号、句点或斜杠')
            if binding[-1] in RESERVED_KEYS:
                raise ValueError('此组合由系统或文本编辑保留，请选择其他快捷键')
            if binding in seen:
                raise ValueError('快捷键重复，请为每个动作选择不同组合')
            seen.add(binding)
        normalized[action] = binding
    return normalized


def validate_values(value):
    from xiaoyu.modes import BY_NAME
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('偏好字段不正确')
    if not isinstance(value['default_model'], str) or len(value['default_model']) > 300:
        raise ValueError('默认模型名称过长或格式不正确')
    if not isinstance(value['default_mode'], str) or value['default_mode'] not in {'', *BY_NAME}:
        raise ValueError('默认权限模式不正确')
    try:
        if not isinstance(value['timezone'], str) or len(value['timezone']) > 100:
            raise ValueError()
        ZoneInfo(value['timezone'])
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError('请选择有效的 IANA 时区') from None
    notice = value['notifications']
    if not isinstance(notice, dict) or set(notice) != set(DEFAULTS['notifications']) or any(type(x) is not bool for x in notice.values()):
        raise ValueError('通知偏好不正确')
    value = copy.deepcopy(value)
    ui = value['ui']
    if isinstance(ui, dict) and 'shortcuts' not in ui:
        ui['shortcuts'] = copy.deepcopy(SHORTCUTS)
    if not isinstance(ui, dict) or set(ui) != set(DEFAULTS['ui']):
        raise ValueError('界面偏好字段不正确')
    for field, options in {'theme': {'system', 'light', 'dark'}, 'send_key': {'mod-enter', 'enter'},
            'default_workflow': {'auto', 'chat', 'create', 'code', 'weave', 'contest', 'fleet'},
            'session_sort': {'updated', 'created', 'title'}}.items():
        if not isinstance(ui[field], str) or ui[field] not in options:
            raise ValueError('界面偏好取值不正确')
    for field, lower, upper in (('font_size', 12, 20), ('panel_width', 280, 1000)):
        if type(ui[field]) is not int or not lower <= ui[field] <= upper:
            raise ValueError('界面尺寸超出范围')
    if any(type(ui[k]) is not bool for k in ('reduced_motion', 'show_planned', 'sidebar_hidden')):
        raise ValueError('界面开关不正确')
    for field in ('nav_order', 'hidden_nav'):
        items = ui[field]
        if not isinstance(items, list) or any(not isinstance(x, str) or x not in NAV for x in items) or len(set(items)) != len(items):
            raise ValueError('导航列表不正确')
    if set(ui['nav_order']) != set(NAV):
        raise ValueError('导航顺序须包含所有页面')
    ui['shortcuts'] = validate_shortcuts(ui['shortcuts'])
    meta = ui['session_meta']
    if not isinstance(meta, dict) or len(meta) > 500:
        raise ValueError('会话整理数量超出限制')
    for sid, item in meta.items():
        if (not isinstance(sid, str) or not 1 <= len(sid) <= 200 or not isinstance(item, dict)
                or set(item) != {'group', 'read_seq'} or not isinstance(item['group'], str)
                or len(item['group']) > 100 or type(item['read_seq']) is not int or item['read_seq'] < 0):
            raise ValueError('会话分组或阅读进度不正确')
    return copy.deepcopy(value)


class PreferenceStore:
    def __init__(self, path: Path, *, write_guard=None):
        self.path = Path(path)
        self.keyboard_migration = self.path.with_name(self.path.name + '.keyboard-v1')
        self.write_guard = write_guard or (lambda: None)
        self.lock = threading.RLock()

    def read(self):
        with self.lock:
            try:
                fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
                with os.fdopen(fd, 'rb') as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError('偏好文件必须是独立普通文件')
                    raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise ValueError('偏好文件过大')
                state = json.loads(raw.decode('utf-8', errors='replace'))
                if not isinstance(state, dict) or set(state) != {'revision', 'values'} or type(state['revision']) is not int or state['revision'] < 0:
                    raise ValueError('偏好版本格式不正确')
                state['values'] = validate_values(state['values'])
                # Keep migration metadata separate so older apps can still read preferences.
                if not self.keyboard_migration.exists():
                    state['values']['ui']['send_key'] = 'enter'
                return state
            except FileNotFoundError:
                return {'revision': 0, 'values': copy.deepcopy(DEFAULTS)}

    def save(self, revision, values):
        values = validate_values(values)
        with self.lock:
            old = self.read()
            if type(revision) is not int or revision != old['revision']:
                if old['values'] == values:
                    return old
                raise PreferenceConflict()
            state = {'revision': revision + 1, 'values': values}
            encoded = json.dumps(state, ensure_ascii=False).encode('utf-8')
            if len(encoded) > MAX_BYTES:
                raise ValueError('偏好记录超出容量限制')
            self.write_guard()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix='.preferences-', dir=self.path.parent)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    self.write_guard()
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                self.write_guard()
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._record_keyboard_migration()
            return state

    def _record_keyboard_migration(self):
        if self.keyboard_migration.exists():
            return
        self.write_guard()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.keyboard_migration, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
