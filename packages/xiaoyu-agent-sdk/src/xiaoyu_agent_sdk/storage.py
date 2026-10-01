"""Synchronous host storage; adapters own their infrastructure, writers their lease."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import uuid
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Protocol

from xiaoyu.session_log import SESSION_FORMAT, SessionWriteLock, SessionLockedError
from .types import SessionStorageError


@dataclass(frozen=True)
class StoredSessionInfo:
    session_id: str
    metadata: dict[str, Any]


class SessionWriter(Protocol):
    """Exclusive writer. read returns detached records in committed order.

    append is atomic and idempotent for identical record_id and content; reusing
    an ID with different content fails. close is idempotent, releases ownership,
    and never closes a shared adapter. Calls are synchronous and serialized by
    the SDK, but may originate on different threads. Adapters must bound I/O.
    """

    def read(self) -> list[dict[str, Any]]: ...
    def append(self, record_id: str, record: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


class SessionStore(Protocol):
    """open acquires ownership before inspecting or creating a session.

    New sessions reject existing IDs; resume rejects missing IDs and preserves
    original metadata. Every read starts with exactly one meta record. Lock
    contention raises SessionLockedError; all other failures raise
    SessionStorageError. Implement distributed fencing in distributed adapters.
    """

    def open(self, session_id: str, *, metadata: dict[str, Any], resume: bool = False) -> SessionWriter: ...


def _encode(record: dict[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=False, allow_nan=False, sort_keys=True)


class _WriteGate:
    """Bounded FIFO admission prevents local writers starving in SQLite busy waits."""

    def __init__(self, timeout: float) -> None:
        self._timeout = timeout
        self._condition = threading.Condition()
        self._queue: deque[object] = deque()

    @contextmanager
    def enter(self) -> Iterator[None]:
        ticket = object()
        deadline = time.monotonic() + self._timeout
        with self._condition:
            self._queue.append(ticket)
            try:
                while self._queue[0] is not ticket:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise SessionStorageError("Storage write admission timed out")
                    self._condition.wait(remaining)
            except BaseException:
                self._queue.remove(ticket)
                self._condition.notify_all()
                raise
        try:
            yield
        finally:
            with self._condition:
                self._queue.popleft()
                self._condition.notify_all()


class SQLiteSessionStore:
    """Persistent reference adapter for a local filesystem, not a network share.

    Each session has an OS lock; SQLite transactions protect record ordering.
    FULL synchronous rollback-journal transactions persist acknowledged writes.
    This adapter does not promise power-loss durability on every storage device.
    No connection is shared between session writers. The adapter is host-owned.
    """

    def __init__(self, path: Path, *, timeout: float = 5.0) -> None:
        import math
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be positive and finite")
        self.path = Path(path).resolve()
        self.timeout = timeout
        self._write_gate = _WriteGate(timeout)

    def open(self, session_id: str, *, metadata: dict[str, Any], resume: bool = False) -> SessionWriter:
        if not isinstance(session_id, str) or not session_id or len(session_id) > 256:
            raise SessionStorageError("Invalid storage session ID")
        digest = hashlib.sha256(session_id.encode()).hexdigest()
        lock = None
        connection = None
        try:
            lock = SessionWriteLock(self.path.parent / (self.path.name + ".locks") / digest)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
            os.close(fd)
            connection = sqlite3.connect(self.path, timeout=self.timeout, check_same_thread=False)
            with self._write_gate.enter(), connection:
                connection.execute("PRAGMA synchronous=FULL")
                connection.execute("CREATE TABLE IF NOT EXISTS sdk_sessions (id TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
                connection.execute("CREATE TABLE IF NOT EXISTS sdk_records (seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, record_id TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(session_id, record_id))")
                existing = connection.execute("SELECT metadata FROM sdk_sessions WHERE id=?", (session_id,)).fetchone()
                if resume:
                    if existing is None:
                        raise SessionStorageError("Stored session does not exist")
                else:
                    if existing is not None:
                        raise SessionStorageError("Stored session already exists")
                    if metadata.get("event") != "meta" or metadata.get("session_id") != session_id:
                        raise SessionStorageError("Invalid session metadata")
                    connection.execute("INSERT INTO sdk_sessions VALUES (?, ?)", (session_id, _encode(metadata)))
            return _SQLiteWriter(connection, lock, session_id, self._write_gate)
        except BaseException as exc:
            if connection is not None:
                connection.close()
            if lock is not None:
                lock.close()
            if isinstance(exc, (SessionLockedError, SessionStorageError)) or not isinstance(exc, Exception):
                raise
            raise SessionStorageError("Cannot open stored session") from exc

    def list_sessions(self) -> list[StoredSessionInfo]:
        """List metadata without creating an absent database or taking writer locks."""
        if not self.path.exists():
            return []
        try:
            with self._write_gate.enter():
                connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=self.timeout)
                try:
                    rows = connection.execute("SELECT id, metadata FROM sdk_sessions ORDER BY id").fetchall()
                    return [StoredSessionInfo(key, json.loads(body)) for key, body in rows]
                finally:
                    connection.close()
        except Exception as exc:
            raise SessionStorageError("Cannot list stored sessions") from exc


class _SQLiteWriter:
    def __init__(self, connection: sqlite3.Connection, lock: SessionWriteLock, session_id: str, write_gate: _WriteGate) -> None:
        self._connection = connection
        self._lock = lock
        self._id = session_id
        self._mutex = threading.Lock()
        self._closed = False
        self._write_gate = write_gate

    def _check(self) -> None:
        if self._closed:
            raise SessionStorageError("Storage writer is closed")

    def read(self) -> list[dict[str, Any]]:
        with self._mutex, self._write_gate.enter():
            self._check()
            try:
                meta = self._connection.execute("SELECT metadata FROM sdk_sessions WHERE id=?", (self._id,)).fetchone()
                rows = self._connection.execute("SELECT body FROM sdk_records WHERE session_id=? ORDER BY seq", (self._id,)).fetchall()
                records = [json.loads(meta[0])] + [json.loads(row[0]) for row in rows]
                if not all(isinstance(record, dict) for record in records):
                    raise ValueError("Invalid record")
                return records
            except Exception as exc:
                raise SessionStorageError("Cannot read stored session") from exc

    def append(self, record_id: str, record: dict[str, Any]) -> None:
        with self._mutex:
            self._check()
            try:
                if not record_id or record.get("event") == "meta":
                    raise ValueError("Invalid record identity")
                body = _encode(record)
                with self._write_gate.enter(), self._connection:
                    self._connection.execute("INSERT OR IGNORE INTO sdk_records(session_id,record_id,body) VALUES (?,?,?)", (self._id, record_id, body))
                    saved = self._connection.execute("SELECT body FROM sdk_records WHERE session_id=? AND record_id=?", (self._id, record_id)).fetchone()
                    if saved[0] != body:
                        raise ValueError("Conflicting record identity")
            except Exception as exc:
                raise SessionStorageError("Cannot append stored record") from exc

    def close(self) -> None:
        with self._mutex:
            if not self._closed:
                try:
                    self._connection.close()
                    self._lock.close()
                    self._closed = True
                except Exception as exc:
                    raise SessionStorageError("Cannot close storage writer") from exc


class _StoreLog:
    """Adapt the existing kernel journal without changing CLI persistence."""
    path = None
    complete = True

    def __init__(self, writer: SessionWriter) -> None:
        self.writer = writer
        self._mutex = threading.Lock()
        self.broken_reason = ""
        self._write_error: Exception | None = None
        self.locked = True
        self._exit_id = uuid.uuid4().hex
        self._exit_record: dict[str, Any] | None = None

    def _write(self, record: dict[str, Any], record_id: str | None = None) -> None:
        with self._mutex:
            self._write_locked(record, record_id)

    def _write_locked(self, record: dict[str, Any], record_id: str | None = None) -> None:
        if not self.locked or self.broken_reason:
            raise SessionStorageError("Storage writer is unavailable") from self._write_error
        try:
            self.writer.append(record_id or uuid.uuid4().hex, record)
        except Exception as exc:
            self._write_error = exc
            self.broken_reason = "Stored record write failed"
            self.complete = False
            raise SessionStorageError(self.broken_reason) from exc

    def append(self, message: dict[str, Any]) -> None:
        self._write({"ts": datetime.now(timezone.utc).isoformat(), **message})

    def event(self, kind: str, **fields: Any) -> None:
        self._write({"ts": datetime.now(timezone.utc).isoformat(), "event": kind, **fields})

    def preamble(self, kind: str, **fields: Any) -> None:
        self.event(kind, **fields)

    def close(self) -> None:
        if not self.locked:
            return
        # After a failed write, release ownership without pretending it persisted.
        if not self.broken_reason:
            if self._exit_record is None:
                self._exit_record = {"event": "exit", "reason": "normal"}
            self._write(self._exit_record, self._exit_id)
        self.writer.close()
        self.locked = False


def session_metadata(session_id: str, model: str, workspace: Path) -> dict[str, Any]:
    return {"event": "meta", "format": SESSION_FORMAT, "session_id": session_id,
            "model": model, "workspace": str(workspace),
            "started_at": datetime.now(timezone.utc).isoformat()}
