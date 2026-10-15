"""本轮明确交付的工作区文件；不扫描目录，不从模型正文猜路径。"""

from __future__ import annotations

import hashlib
import mimetypes
import os
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from . import fsguard


@dataclass(frozen=True)
class FileArtifact:
    """文件工具或业务工具明确产出的文件；status 只表示轮次是否正常结束。"""

    id: str
    path: str
    size: int
    sha256: str
    operation: Literal["created", "updated", "reported"]
    source: Literal["file_tool", "tool"]
    status: Literal["complete", "incomplete"] = "complete"
    mime_type: str | None = None


class ArtifactCollector:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.active = False
        self._items: dict[str, FileArtifact] = {}
        self._before: dict[str, str | None] = {}

    def begin(self) -> None:
        self.active = True
        self._items.clear()
        self._before.clear()

    def _path(self, path: str | Path) -> tuple[Path, str]:
        target = Path(path)
        if not target.is_absolute():
            target = self.workspace / target
        target = target.resolve()
        relative = target.relative_to(self.workspace).as_posix()
        if relative == "." or ".git" in Path(relative).parts:
            raise ValueError("Artifact must be a workspace file outside Git internals")
        return target, relative

    def written(self, path: Path, before: bytes | None, content: bytes) -> None:
        if not self.active:
            return
        try:
            _, relative = self._path(path)
        except (ValueError, OSError):
            return  # 工作区外的已授权编辑不作为可交付文件广告
        self._before.setdefault(relative, _digest(before) if before is not None else None)
        fingerprint = _digest(content)
        if fingerprint == self._before[relative]:
            self._items.pop(relative, None)
            return
        previous = self._items.get(relative)
        self._items[relative] = FileArtifact(
            id=previous.id if previous else uuid.uuid4().hex,
            path=relative, size=len(content), sha256=fingerprint,
            operation="created" if self._before[relative] is None else "updated",
            source="file_tool", mime_type=mimetypes.guess_type(relative)[0],
        )

    def report(self, path: str | Path) -> None:
        """业务工具显式交付文件。越界、特殊文件和失效路径均拒绝。"""
        if not self.active:
            raise ValueError("Artifacts can only be reported during a turn")
        target, relative = self._path(path)
        size, fingerprint = fingerprint_file(target)
        previous = self._items.get(relative)
        self._items[relative] = FileArtifact(
            id=previous.id if previous else uuid.uuid4().hex,
            path=relative, size=size, sha256=fingerprint,
            operation=previous.operation if previous else "reported",
            source=previous.source if previous else "tool",
            mime_type=mimetypes.guess_type(relative)[0],
        )

    def report_many(self, paths: tuple[str, ...]) -> None:
        previous = dict(self._items)
        try:
            for path in paths:
                self.report(path)
        except (ValueError, OSError, TypeError):
            self._items = previous
            raise

    def finish(self, complete: bool) -> tuple[FileArtifact, ...]:
        self.active = False
        result = []
        for item in self._items.values():
            try:
                target, _ = self._path(item.path)
                size, fingerprint = fingerprint_file(target)
            except (ValueError, OSError):
                continue
            # 工具交付后被删除或改写的文件不再广告为这次交付的版本。
            if (size, fingerprint) == (item.size, item.sha256):
                result.append(replace(item, status="complete" if complete else "incomplete"))
        self._items.clear()
        self._before.clear()
        return tuple(result)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def fingerprint_file(path: Path) -> tuple[int, str]:
    fsguard.require_regular(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise OSError("Artifact is not a regular file")
        digest = hashlib.sha256()
        size = 0
        while size < before.st_size:
            block = stream.read(min(1024 * 1024, before.st_size - size))
            if not block:
                raise OSError("Artifact was truncated while being read")
            digest.update(block)
            size += len(block)
        after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise OSError("Artifact changed while being read")
    return size, digest.hexdigest()
