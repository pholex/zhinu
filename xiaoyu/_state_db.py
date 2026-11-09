"""Local SQLite state with explicit format identity and transactional writes."""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
from typing import Iterator

from . import fsguard


class StateError(RuntimeError):
    """State could not be read or committed; never replace it with defaults."""


class StateDB:
    def __init__(self, path: Path, kind: str, schema: tuple[str, ...]) -> None:
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fsguard.require_regular(self.path)
        except FileNotFoundError:
            if self.path.is_symlink():
                raise StateError("Broken state database symlink")
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        self.kind, self.schema = kind, schema
        with self.connect(write=True) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not tables:
                db.execute("CREATE TABLE state_format (kind TEXT NOT NULL, version INTEGER NOT NULL)")
                db.execute("INSERT INTO state_format VALUES (?, 1)", (kind,))
                for statement in schema:
                    db.execute(statement)
            elif "state_format" not in tables or db.execute("SELECT kind, version FROM state_format").fetchall() != [(kind, 1)]:
                raise StateError("Unknown state database format; original file retained")

    @contextmanager
    def connect(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            db = sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=5)
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except sqlite3.Error as exc:
            raise StateError("State database operation failed; original file retained") from exc
        finally:
            if db is not None:
                db.close()
