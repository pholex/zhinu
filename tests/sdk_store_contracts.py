"""Reusable storage contracts. Run against an isolated, disposable adapter."""
from __future__ import annotations

import uuid
from pathlib import Path

from xiaoyu.session_log import SessionLockedError
from xiaoyu_agent_sdk.storage import SessionStore, session_metadata
from xiaoyu_agent_sdk.types import SessionStorageError


def check_session_store(store: SessionStore, workspace: Path) -> None:
    """Check ordered persistence, detached reads, idempotency and exclusive ownership.

    Creates two random sessions and retains them. This is a functional contract,
    not process-crash, distributed-fencing or durability certification.
    """
    key, other = uuid.uuid4().hex, uuid.uuid4().hex
    meta = session_metadata(key, "contract-model", workspace)
    writer = store.open(key, metadata=meta)
    try:
        if writer.read() != [meta]:
            raise AssertionError("New session metadata differs")
        try:
            unexpected = store.open(key, metadata=meta, resume=True)
        except SessionLockedError:
            pass
        else:
            unexpected.close()
            raise AssertionError("Duplicate ownership was allowed")
        record = {"role": "user", "content": "one"}
        writer.append("first", record)
        writer.append("first", record)
        record["content"] = "changed"
        if writer.read() != [meta, {"role": "user", "content": "one"}]:
            raise AssertionError("Write was duplicated or retained mutable input")
        detached = writer.read()
        detached[-1]["content"] = "mutated-read"
        if writer.read()[-1]["content"] != "one":
            raise AssertionError("Read is not detached")
        try:
            writer.append("first", {"role": "user", "content": "conflict"})
        except SessionStorageError:
            pass
        else:
            raise AssertionError("Conflicting record identity was accepted")
        with_other = store.open(other, metadata=session_metadata(other, "contract-model", workspace))
        try:
            with_other.append("first", {"role": "user", "content": "other"})
            if with_other.read()[-1]["content"] != "other" or writer.read()[-1]["content"] != "one":
                raise AssertionError("Sessions are not isolated")
        finally:
            with_other.close()
        writer.append("second", {"role": "assistant", "content": "two"})
    finally:
        writer.close()
    writer.close()
    try:
        writer.append("late", {"role": "user", "content": "late"})
    except SessionStorageError:
        pass
    else:
        raise AssertionError("Closed writer accepted writes")
    resumed = store.open(key, metadata=meta, resume=True)
    try:
        if resumed.read() != [meta, {"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]:
            raise AssertionError("Persistence or ordering differs")
    finally:
        resumed.close()
    try:
        unexpected = store.open(uuid.uuid4().hex, metadata=meta, resume=True)
    except SessionStorageError:
        pass
    else:
        unexpected.close()
        raise AssertionError("Missing session resumed")
