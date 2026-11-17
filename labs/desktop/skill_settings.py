"""Application-wide, exact-name skill choices with atomic cross-process updates."""
from __future__ import annotations
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from contextlib import closing
from .preferences import PreferenceConflict
from xiaoyu.session_log import SessionWriteLock, SessionLockedError

MAX_BYTES = 256 * 1024


def names(value):
    if (not isinstance(value, list) or len(value) > 2000
            or any(not isinstance(n, str) or not n or len(n) > 300 or any(ord(c) < 32 for c in n) for n in value)
            or len(set(value)) != len(value)):
        raise ValueError('技能名称列表不正确')
    return sorted(value)


class SkillSettings:
    def __init__(self, path, write_guard=lambda: None):
        self.path = Path(path)
        self.write_guard = write_guard
        self.lock = threading.RLock()

    def read(self):
        try:
            fd = os.open(self.path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        except FileNotFoundError:
            return {'revision': 0, 'disabled': []}
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('技能设置不是独立普通文件')
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('技能设置超过容量限制')
        value = json.loads(raw.decode('utf-8', errors='replace'))
        if (not isinstance(value, dict) or set(value) != {'revision', 'disabled'}
                or type(value['revision']) is not int or value['revision'] < 0):
            raise ValueError('技能设置损坏，请核对原文件')
        return {**value, 'disabled': names(value['disabled'])}

    def disabled_names(self):
        return tuple(self.read()['disabled'])

    def save(self, revision, ids, enabled):
        ids = names(ids)
        if not ids or type(enabled) is not bool or type(revision) is not int:
            raise ValueError('技能开关请求不正确')
        with self.lock:
            self.write_guard()
            try:
                lease = SessionWriteLock(self.path)
            except SessionLockedError:
                raise PreferenceConflict() from None
            with closing(lease):
                current = self.read()
                if revision != current['revision']:
                    raise PreferenceConflict()
                disabled = set(current['disabled'])
                disabled.difference_update(ids) if enabled else disabled.update(ids)
                value = {'revision': revision + 1, 'disabled': sorted(disabled)}
                raw = json.dumps(value, ensure_ascii=False).encode('utf-8')
                if len(raw) > MAX_BYTES:
                    raise ValueError('技能设置超过容量限制')
                fd, temporary = tempfile.mkstemp(prefix='.skills-', dir=self.path.parent)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                    self.write_guard()
                    os.replace(temporary, self.path)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
                return value
