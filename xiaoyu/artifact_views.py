"""Bounded delivery snapshots and descriptor-based reads of registered artifacts."""
from __future__ import annotations

import base64
import hashlib
import os
import stat
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

TEXT_BYTES = 64 * 1024
READ_BYTES = 8 * 1024 * 1024
CAPTURE_BYTES = 256 * 1024


def read_file(workspace: Path, relative: str) -> tuple[bytes, str]:
    """Never follow links, read special files, or accept caller-provided roots."""
    if not hasattr(os, "O_NOFOLLOW") or os.open not in os.supports_dir_fd:
        raise OSError("此平台暂不支持安全的成果文件读取。")
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or Path(relative).is_absolute() or any(p in {"", ".", "..", ".git"} for p in relative.split("/"))):
        raise ValueError("成果路径不在可查看范围内。")
    root = Path(workspace)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("成果工作目录不可用。")
    descriptors: list[int] = []
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(root.anchor, flags)
        descriptors.append(parent)
        for part in (*root.parts[1:], *Path(relative).parts[:-1]):
            parent = os.open(part, flags, dir_fd=parent)
            descriptors.append(parent)
        fd = os.open(Path(relative).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        descriptors.append(fd)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ValueError("仅可查看独立的普通成果文件。")
        if before.st_size > READ_BYTES:
            raise ValueError("文件超过 8 MiB，当前无法校验或预览。")
        content = bytearray()
        while len(content) < before.st_size:
            block = os.read(fd, min(64 * 1024, before.st_size - len(content)))
            if not block:
                raise OSError("读取时文件发生变化，请刷新。")
            content.extend(block)
        after = os.fstat(fd)
        current = os.stat(Path(relative).name, dir_fd=parent, follow_symlinks=False)
        version = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if version(before) != version(after) or version(before) != version(current):
            raise OSError("读取时文件发生变化，请刷新。")
        data = bytes(content)
        return data, hashlib.sha256(data).hexdigest()
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def is_text(data: bytes) -> bool:
    if b"\x00" in data:
        return False
    return not any(byte < 32 and byte not in (9, 10, 12, 13) for byte in data)


def _encoded(data: bytes | None, note: str = "") -> dict[str, Any]:
    if data is None:
        return {"available": False, "note": note or "没有保存此版本。"}
    if len(data) > TEXT_BYTES:
        return {"available": False, "note": "文件超过 64 KiB，未保存文本快照。"}
    if not is_text(data):
        return {"available": False, "note": "二进制文件不提供文本快照。"}
    return {"available": True, "base64": base64.b64encode(data).decode("ascii"), "note": note}


def capture(workspace: Path, artifacts: Any, point: Any = None) -> list[dict[str, Any]]:
    snapshots = []
    used = 0
    for value in list(artifacts or ())[:16]:
        artifact = asdict(value) if is_dataclass(value) else value
        if not isinstance(artifact, dict):
            continue
        relative = artifact.get("path", "")
        record = {"id": artifact.get("id"), "path": relative, "workspace": str(workspace),
                  "delivered": _encoded(None), "before": _encoded(None), "note": ""}
        try:
            data, fingerprint = read_file(workspace, relative)
            if fingerprint == artifact.get("sha256"):
                record["delivered"] = _encoded(data)
            else:
                record["delivered"] = _encoded(None, "保存快照时文件已变化，未冒充交付版本。")
            absolute = str(workspace / relative)
            if point is not None and point.after.get(absolute) == artifact.get("sha256") and absolute in point.files:
                record["before"] = _encoded(point.files[absolute] or b"")
            elif artifact.get("operation") == "created":
                record["before"] = _encoded(b"")
        except (OSError, ValueError, TypeError) as exc:
            record["note"] = str(exc)
        size = sum(len(record[key].get("base64", "")) for key in ("before", "delivered"))
        if used + size > CAPTURE_BYTES:
            record["before"] = record["delivered"] = _encoded(None, "本次成果快照达到保存上限。")
        else:
            used += size
        snapshots.append(record)
    return snapshots


def capture_session(session: Any, run: dict[str, Any]) -> None:
    artifacts = (run.get("result") or {}).get("artifacts") or []
    if not artifacts:
        return
    store = getattr(session.agent.toolbox, "rewind", None)
    points = store.points() if store is not None else []
    run["artifact_previews"] = capture(session.workspace, artifacts, points[-1] if points else None)
