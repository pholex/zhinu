"""Private strict serve ownership. Ordinary CLI log policy is unchanged."""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from .session_log import SessionLog, SessionWriteLock, lock_path

RETAINED_REASON = "执行线程曾被中断，无法证明全部收尾；所有权保留到进程退出。请重启服务进程后接续。"
_RETAINED: list[Any] = []


def _valid(finalizer, path: Path) -> bool:
    if finalizer is None or not finalizer.alive:
        return False
    try:
        state = finalizer.peek()
        fd = state[2][0]
        held, current = os.fstat(fd), os.stat(lock_path(path), follow_symlinks=False)
        return (stat.S_ISREG(held.st_mode) and held.st_nlink == 1
                and held.st_dev == current.st_dev and held.st_ino == current.st_ino)
    except (OSError, TypeError, IndexError):
        return False


class StrictSessionLog(SessionLog):
    def delete_history(self) -> None:
        """Remove this owned log before releasing its lock; never unlink the lock file."""
        with self._mutex:
            if not self.locked:
                raise OSError("会话日志未持有独占写锁，不能删除。")
            self.path.unlink(missing_ok=True)
            self._closed = True
            self._pending = None
            self._release_locked()

    @property
    def locked(self) -> bool:
        return _valid(self._lock_finalizer, self.path)

    def _acquire_locked(self) -> bool:
        acquired = super()._acquire_locked()
        if not self.locked:
            self._released = True
            raise OSError("服务会话无法取得严格写锁，禁止无锁执行。")
        return acquired

    def _write_locked(self, record):
        if not self.locked:
            self._broken = True
            self.broken_reason = "服务会话已失去独占写锁，停止记录。"
            return
        super()._write_locked(record)

    @classmethod
    def create(cls, *args, **kwargs):
        if kwargs.get("defer"):
            raise ValueError("服务会话不能延迟取得写锁。")
        log = super().create(*args, **kwargs)
        if not log.locked or not log.complete:
            log.release()
            raise OSError("服务会话日志无法可靠创建。")
        return log


class StateOwner:
    def __init__(self, directory: Path, persistent: bool):
        self.path = directory / "serve-owner"
        self.persistent = persistent
        self.lease = SessionWriteLock(self.path) if persistent else None
        self.closed = False
        self.retained = False
        self.logs: list[Any] = []

    @property
    def held(self) -> bool:
        return not self.closed and (not self.persistent or _valid(self.lease._finalizer, self.path))

    def retain(self, log=None):
        if not self.retained:
            self.retained = True
            _RETAINED.append(self)
        if log is not None and log not in self.logs:
            self.logs.append(log)

    def close(self):
        if self.retained:
            return
        self.closed = True
        if self.lease is not None:
            self.lease.close()


def reason(session: Any) -> str:
    if getattr(session, "management_busy", False):
        return "连接管理操作仍在执行，请等待完成后再提交任务。"
    if getattr(session, "closing", False):
        return "会话正在关闭，不能开始新的写入。"
    if getattr(session, "owner_uncertain", False):
        return RETAINED_REASON
    owner = getattr(session, "execution_owner", None)
    if owner is not None and not owner.held:
        return "服务状态目录已失去执行所有权，请重启服务进程后核对。"
    log = session.agent.session_log
    if log is None or not getattr(log, "locked", False):
        return "会话日志已失去独占写锁，不能继续执行；请重启服务进程后核对。"
    if not log.complete or getattr(session, "history_warning", ""):
        return "会话记录不完整，不能继续写入；请先核对并恢复服务。"
    return ""


def require_write(session: Any) -> None:
    from fastapi import HTTPException
    if problem := reason(session):
        raise HTTPException(503, {"code": "execution_owner_unavailable", "message": problem})
