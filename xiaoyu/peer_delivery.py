"""跨会话可靠留言：持久化投递，日志提交后确认，显式重投不重放已接纳消息。"""

from __future__ import annotations

import json
import contextlib
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import fsguard
from .session_log import SessionWriteLock, SessionLockedError

logger = logging.getLogger(__name__)


def directory() -> Path:
    from .peers import peers_dir
    return peers_dir().parent / "peer-deliveries"


def _path(message_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{32}", message_id):
        raise ValueError("消息 ID 必须是 32 位十六进制字符串")
    return directory() / f"{message_id}.json"


def _save(record: dict[str, Any]) -> None:
    fsguard.write_atomic(_path(record["id"]), json.dumps(record, ensure_ascii=False), private=True)


def _recipient(target: str) -> tuple[str, str]:
    from .peers import peers_dir, resolve
    peer = resolve(target)
    meta = json.loads((peers_dir() / peer.ref / "meta.json").read_text(encoding="utf-8", errors="replace"))
    journal = meta.get("delivery_journal")
    if not isinstance(journal, str) or not journal:
        raise ValueError("收件会话未启用可靠消息日志，请使用普通留言")
    return peer.ref, journal


def send(target: str, text: str, sender: str = "") -> dict[str, Any]:
    if not text.strip():
        raise ValueError("消息不能为空")
    ref, journal = _recipient(target)
    record = {"id": uuid.uuid4().hex, "target_ref": ref, "journal": journal,
              "sender": sender or "命令行", "text": text, "created_at": time.time(),
              "status": "pending"}
    _save(record)
    return record


@contextlib.contextmanager
def _locked(message_id: str):
    try:
        lock = SessionWriteLock(_path(message_id))
    except SessionLockedError as exc:
        raise OSError("消息正在接纳或重投，请稍后重试") from exc
    try:
        yield
    finally:
        lock.close()


def status(message_id: str) -> dict[str, Any]:
    with _locked(message_id):
        return _status(message_id)


def _status(message_id: str) -> dict[str, Any]:
    """日志是确认事实来源，修复提交成功但回执写入失败的窗口。"""
    record = json.loads(_path(message_id).read_text(encoding="utf-8", errors="replace"))
    if (not isinstance(record, dict) or record.get("id") != message_id
            or record.get("status") not in {"pending", "accepted"}
            or not all(isinstance(record.get(k), str) and record[k]
                       for k in ("target_ref", "journal", "sender", "text"))
            or not isinstance(record.get("created_at"), (int, float))):
        raise ValueError("消息记录损坏")
    if record.get("status") == "accepted":
        return record
    journal = Path(record["journal"])
    try:
        with journal.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError as exc:
                    if record.get("attempted"):
                        raise OSError("收件日志损坏，无法确定消息是否接纳；请先修复日志") from exc
                    continue
                if (isinstance(event, dict) and event.get("event") == "peer.delivery"
                        and event.get("message_id") == message_id
                        and isinstance(event.get("message"), dict)
                        and event["message"].get("_peer_delivery_id") == message_id):
                    record["status"] = "accepted"
                    _save(record)
                    break
    except FileNotFoundError:
        if record.get("attempted"):
            raise OSError("收件日志缺失，无法确定消息是否已接纳；拒绝盲目重投")
    return record


def retry(message_id: str, target: str) -> dict[str, Any]:
    """接回新会话后显式重投；保留 ID，已接纳的消息不再次投递。"""
    with _locked(message_id):
        record = _status(message_id)
        if record["status"] == "accepted":
            return record
        ref, journal = _recipient(target)
        record.update(target_ref=ref, journal=journal, status="pending", attempted=False)
        _save(record)
        return record


def pending(ref: str, journal: str) -> list[dict[str, Any]]:
    """命名会话接回同一日志可继续接收；普通 resume 由发件方显式重投。"""
    records = []
    for path in directory().glob("*.json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
            if not isinstance(raw, dict) or raw.get("status") != "pending":
                continue
            if raw.get("target_ref") != ref and raw.get("journal") != journal:
                continue
            record = status(path.stem)
            if record["status"] == "pending":
                records.append(record)
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("可靠消息暂时无法读取：%s", path)
    return sorted(records, key=lambda r: (r["created_at"], r["id"]))


def accept(message_id: str, ref: str, journal: Any, message: dict, consumed: Any) -> bool:
    """锁住单条投递，防止重投与接纳竞态；确认事实和正文同一条日志提交。"""
    with _locked(message_id):
        record = _status(message_id)
        if record["status"] == "accepted":
            return False
        if record["target_ref"] != ref and record["journal"] != str(journal.path):
            return False
        record.update(journal=str(journal.path), attempted=True)
        _save(record)
        journal.commit_event("peer.delivery", message_id=message_id, message=message)
        consumed(message)
        record["status"] = "accepted"
        _save(record)
        return True
