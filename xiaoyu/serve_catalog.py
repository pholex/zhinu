"""Scoped discovery, explicit branching and in-place continuation of local
terminal session journals.

A catalog identifier is not a path capability: every request resolves it against
one configured directory and rechecks the recorded workspace. Branching uses the
same sidecar lock as the CLI, without its best-effort lock fallback.

Three verdicts are computed per journal, because "who may write" and "what may be
copied" are different questions:

- locked: another writer holds the sidecar lock right now. The body stays
  readable; nothing here ever waits for or breaks that lock.
- can_continue: the journal ended cleanly and nobody holds it, so this service
  may reopen the same file and keep appending (see Catalog.adopt).
- can_fork: a consistent snapshot can be copied into a new session. A cleanly
  closed journal qualifies; so does a held one that is resting on a settled turn
  boundary, since copying never touches the source.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from . import media, session_log
from .agent import SYNTHETIC_USER_TEXTS
from .serve_history import page


class CatalogError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _revision(info: os.stat_result) -> str:
    identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
    return hashlib.sha256(repr(identity).encode()).hexdigest()


def _balanced(messages: list[dict[str, Any]]) -> bool:
    pending: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            return False
        if message.get("role") == "assistant":
            if pending:
                return False
            calls = message.get("tool_calls")
            if calls is None:
                calls = []
            if not isinstance(calls, list):
                return False
            for call in calls:
                if not isinstance(call, dict):
                    return False
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id or call_id in pending:
                    return False
                function = call.get("function")
                if (call.get("type") != "function" or not isinstance(function, dict)
                        or not isinstance(function.get("name"), str)
                        or not isinstance(function.get("arguments"), str)):
                    return False
                pending.add(call_id)
        elif message.get("role") == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                return False
            pending.remove(call_id)
        elif pending:
            return False
    return not pending


def _settled(messages: list[dict[str, Any]]) -> bool:
    """A held journal may be copied only while it rests between turns: the last
    replayable message is a finished assistant reply, not a question still being
    answered or a tool round in flight."""
    if not messages:
        return False
    last = messages[-1]
    return last.get("role") == "assistant" and not last.get("tool_calls")


class Catalog:
    def __init__(self, workspace_root: Path, directory: Path | None = None, held=None,
                 everywhere: bool = False):
        """`held` returns {journal path: session id} for journals this service has
        already reopened in place. Their lock is ours, not a terminal's.

        `everywhere` widens discovery only: journals recorded for a workspace
        outside this root are listed and readable, marked in_scope=false, and can
        never be continued or branched here. A local single-user host opts in; the
        default keeps a network-reachable service from exposing unrelated histories.
        """
        self.held = held or (lambda: {})
        self.everywhere = everywhere
        # 解析结果按文件修订缓存：全局清单动辄几百份日志，逐次重读太慢。
        # 写锁状态不进缓存，每次现探。
        self._facts: dict[Path, tuple[str, dict[str, Any]]] = {}
        self.root = workspace_root.resolve()
        self.directory = (directory if directory is not None else session_log.sessions_dir()).resolve()
        # Path traversal through directory descriptors is required; never downgrade
        # a source snapshot to unchecked path opens on an unsupported platform.
        self.supported = os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW")

    def _id(self, relative: Path) -> str:
        return "cli-" + hashlib.sha256(str(self.directory / relative).encode()).hexdigest()[:32]

    def _candidates(self, warnings: list[str] | None = None):
        warnings = warnings if warnings is not None else []
        if not self.supported:
            return
        try:
            entries = list(self.directory.iterdir())
        except FileNotFoundError:
            return
        except OSError:
            warnings.append("会话目录暂不可读取；列表可能不完整。")
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                if entry.is_file() and entry.suffix == ".jsonl":
                    yield entry.relative_to(self.directory)
                elif entry.is_dir():
                    for child in entry.iterdir():
                        if not child.is_symlink() and child.is_file() and child.suffix == ".jsonl":
                            yield child.relative_to(self.directory)
            except OSError:
                warnings.append("部分会话目录在扫描时不可读取；列表可能不完整。")

    def _find(self, catalog_id: str) -> Path:
        if not self.supported:
            raise CatalogError("此平台暂不支持安全的本地会话目录读取。", 503)
        warnings: list[str] = []
        for relative in self._candidates(warnings):
            if self._id(relative) == catalog_id:
                return relative
        if warnings:
            raise CatalogError("会话目录暂不可读取，请刷新后重试。")
        raise CatalogError("未找到当前工作范围内的来源会话。", 404)

    @contextlib.contextmanager
    def _source(self, relative: Path):
        descriptors: list[int] = []
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            parent = os.open(self.directory, flags)
            descriptors.append(parent)
            for component in relative.parts[:-1]:
                parent = os.open(component, flags, dir_fd=parent)
                descriptors.append(parent)
            fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            descriptors.append(fd)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CatalogError("来源不是独立的普通日志文件。", 404)
            yield parent, fd
        except OSError:
            raise CatalogError("来源路径已变化或无法安全读取，请刷新。", 409) from None
        finally:
            for descriptor in reversed(descriptors):
                os.close(descriptor)

    @contextlib.contextmanager
    def _lease(self, parent: int, name: str, create: bool = True):
        """create=False 给只读的列出与查看用：不在别人的会话旁留下锁文件。
        锁文件不存在 = 从没有写者拿过锁，按无人持有处理。"""
        fd = None
        try:
            try:
                fd = os.open(name + ".lock", os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NOFOLLOW | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
            except FileNotFoundError:
                if create:
                    raise
                yield True
                return
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise CatalogError("无法建立来源会话的严格写锁。")
            locked = session_log._try_lock(fd)
            yield locked
        except OSError:
            raise CatalogError("无法建立来源会话的严格写锁。") from None
        finally:
            if fd is not None:
                os.close(fd)

    def _scope(self, raw_workspace: Any) -> tuple[Path, bool]:
        if not isinstance(raw_workspace, str) or not Path(raw_workspace).is_absolute():
            raise CatalogError("来源工作区不可用。", 404)
        workspace = Path(raw_workspace).resolve()
        inside = workspace == self.root or self.root in workspace.parents
        if not (inside or self.everywhere) or not workspace.is_dir():
            raise CatalogError("来源工作区不在当前工作范围内。", 404)
        return workspace, inside

    def _read(self, fd: int, size: int):
        """Parse one journal from an already-validated descriptor."""
        records: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        meta: dict[str, Any] | None = None
        valid = True
        # Duplicate the descriptor, not the path. A rename or symlink swap
        # cannot redirect the body read after workspace validation.
        with os.fdopen(os.dup(fd), "rb") as handle:
            remaining = size
            number = 0
            while remaining > 0:
                raw = handle.readline(remaining)
                if not raw:
                    break
                remaining -= len(raw)
                number += 1
                if not raw.endswith(b"\n"):
                    valid = False
                    continue
                try:
                    decoded = raw.decode("utf-8", errors="replace")
                    if decoded.encode("utf-8") != raw:
                        valid = False
                    record = json.loads(decoded)
                except ValueError:
                    valid = False
                    continue
                if not isinstance(record, dict):
                    valid = False
                    continue
                if meta is None:
                    if record.get("event") != "meta":
                        raise CatalogError("来源缺少有效会话元数据。", 404)
                    meta = record
                    self._scope(meta.get("workspace"))  # 范围外的尽早放弃，不读正文
                records.append(record)
                event = record.get("event")
                message = record.get("message") if event == "sdk.question.delivery" else record
                if event not in (None, "sdk.question.delivery") or not isinstance(message, dict):
                    continue
                role = message.get("role")
                if role not in ("user", "assistant"):
                    continue
                if role == "user" and (media.is_injected_message(message, SYNTHETIC_USER_TEXTS)
                                       or media.is_peer_message(message)):
                    continue
                content = media.text_of(message.get("content"))
                parts = message.get("content")
                attachments = isinstance(parts, list) and any(isinstance(p, dict) and p.get("type") != "text" for p in parts)
                if content or attachments:
                    messages.append({"id": f"log:{number}", "role": role,
                                     "content": content or "[附件]", "created_at": record.get("ts", ""),
                                     "run_id": None, "has_attachments": attachments})
        if meta is None:
            raise CatalogError("来源没有会话元数据。", 404)
        try:
            history = session_log.replay_records(records)
            history = session_log.LoadedMessages(
                [item for item in history if item.get("role") in ("user", "assistant", "tool")],
                history.corrupt_lines,
            )
            usable = _balanced(history)
        except (ValueError, TypeError, KeyError, AttributeError):
            history = []
            usable = False
        final = records[-1] if records else {}
        first = next((item["content"] for item in messages if item["role"] == "user"), "终端会话")
        facts = {
            "title": str(meta.get("title") or " ".join(first.split())[:80]),
            "workspace": meta.get("workspace"), "model": str(meta.get("model") or ""),
            "started_at": str(meta.get("started_at") or ""), "valid": valid,
            "usable": bool(usable and history), "settled": bool(usable and _settled(history)),
            "closed": final.get("event") == "exit" and final.get("reason") in ("normal", "closed"),
            "rewritten": any(record.get("event") in ("compact", "rewind", "clear") for record in records),
        }
        return facts, messages, history

    def _snapshot(self, relative: Path, *, expected: str | None = None, fork: bool = False,
                  adopt: bool = False, summary: bool = False):
        """summary=True 只要清单条目：修订没变就用缓存的解析结果，不重读正文。"""
        owner = self.held().get(self.directory / relative, "")
        with self._source(relative) as (parent, fd), self._lease(parent, relative.name, fork or adopt) as acquired:
            if adopt and owner:
                raise CatalogError("这段历史已在本服务中继续，请打开对应会话。")
            if adopt and not acquired:
                raise CatalogError("来源会话已在终端中打开，请先在那边结束它。", 423)
            before = os.fstat(fd)
            revision = _revision(before)
            if expected is not None and expected != revision:
                raise CatalogError("来源会话已变化，请刷新预览后再操作。")
            cached = self._facts.get(relative)
            messages: list[dict[str, Any]] = []
            history: Any = []
            if summary and cached is not None and cached[0] == revision:
                facts = cached[1]
            else:
                try:
                    facts, messages, history = self._read(fd, before.st_size)
                except CatalogError:
                    self._facts.pop(relative, None)
                    raise
                # Check both the opened inode and the current directory entry. This
                # catches edits, rename replacement and changing symlinks alike.
                try:
                    current = os.stat(relative.name, dir_fd=parent, follow_symlinks=False)
                except OSError:
                    raise CatalogError("来源已变化，请刷新。") from None
                if _revision(os.fstat(fd)) != revision or _revision(current) != revision:
                    raise CatalogError("来源会话正在变化，请刷新后重试。")
                self._facts[relative] = (revision, facts)
            workspace, inside = self._scope(facts["workspace"])
            valid = facts["valid"]
            locked = not acquired and not owner
            if not valid:
                damaged = "来源日志不完整或损坏，请先在终端检查。"
            elif not facts["usable"]:
                damaged = "来源没有可安全接续的完整上下文。"
            else:
                damaged = ""
            unclosed = "" if facts["closed"] else "来源没有正常结束标记；请先在终端恢复、检查并正常退出。"
            if not inside:
                keep = branch = "这场会话属于另一个工作目录；请在那个目录的工作台里继续。"
            elif owner:
                keep = branch = "这段历史已在本服务中继续，请打开对应会话。"
            elif locked:
                keep = "来源会话已在终端中打开，请先在那边结束它。"
                # 写者还在：只在它停在回合边界时取快照，轮中的半截状态不带走。
                # 轮中的日志本就有没配对的工具调用，那不是损坏
                if not valid:
                    branch = damaged
                elif facts["settled"]:
                    branch = ""
                else:
                    branch = "来源会话正在执行一轮；等它停在回合边界后刷新，再另开分支。"
            else:
                keep = branch = damaged or unclosed
            if fork and branch:
                raise CatalogError(branch)
            if adopt and keep:
                raise CatalogError(keep)
            item = {"catalog_id": self._id(relative), "source": "cli",
                    "title": facts["title"], "workspace": str(workspace), "in_scope": inside,
                    "model": facts["model"], "started_at": facts["started_at"],
                    "updated_at": before.st_mtime,
                    "revision": revision, "locked": locked, "session_id": owner,
                    "can_fork": not branch, "fork_reason": branch,
                    "can_continue": not keep, "continue_reason": keep}
            warning = "来源为终端会话的只读正文快照；查看不会取得它的写锁。"
            if not valid:
                warning += " 日志含损坏或不完整记录。"
            if facts["rewritten"]:
                warning += " 原始正文包含上下文压缩或回退之前的内容；分支使用当前可恢复上下文。"
            return item, messages, history, warning, valid

    def list(self, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        items = []
        warnings: list[str] = []
        for relative in self._candidates(warnings):
            try:
                items.append(self._snapshot(relative, summary=True)[0])
            except CatalogError as exc:
                if exc.status != 404:
                    warnings.append("部分会话暂不可读取或正在变化；列表可能不完整，请刷新。")
                continue
        items.sort(key=lambda item: item["updated_at"], reverse=True)
        result = page(items, cursor, limit)
        return {"sessions": result.pop("items"), **result,
                "warning": " ".join(dict.fromkeys(warnings)) if self.supported else
                           "此平台暂不支持安全的本地会话目录读取。"}

    def history(self, catalog_id: str, cursor: int = 0, limit: int = 100) -> dict[str, Any]:
        item, messages, _, warning, valid = self._snapshot(self._find(catalog_id))
        result = page(messages, cursor, limit)
        return {**item, "messages": result.pop("items"), **result, "persisted": valid, "warning": warning}

    def branch(self, reference: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if set(reference) != {"catalog_id", "revision"} or not all(isinstance(v, str) and v for v in reference.values()):
            raise CatalogError("终端分支必须提供 catalog_id 和 revision，且不接受其他字段。", 400)
        item, _, history, _, _ = self._snapshot(self._find(reference["catalog_id"]),
                                              expected=reference["revision"], fork=True)
        # No source system prompt, provider configuration, temporary grants,
        # checkpoints, live child handles or credentials are restored.
        return item, list(history)

    def adopt(self, reference: dict[str, Any]) -> tuple[dict[str, Any], Path]:
        """Validate a journal for in-place continuation and name its file.

        The probe lock is dropped before returning: the caller must take the real
        writer lock itself and then call `confirm`, because another process may
        have reopened or rewritten the journal in between.
        """
        if set(reference) != {"catalog_id", "revision"} or not all(isinstance(v, str) and v for v in reference.values()):
            raise CatalogError("原地继续必须提供 catalog_id 和 revision，且不接受其他字段。", 400)
        relative = self._find(reference["catalog_id"])
        item, _, _, _, _ = self._snapshot(relative, expected=reference["revision"], adopt=True)
        return item, self.directory / relative

    def confirm(self, path: Path, revision: str) -> None:
        """Called with the writer lock held: the file must still be the one previewed."""
        try:
            current = os.stat(path, follow_symlinks=False)
        except OSError:
            raise CatalogError("来源已变化，请刷新。") from None
        if not stat.S_ISREG(current.st_mode) or current.st_nlink != 1 or _revision(current) != revision:
            raise CatalogError("来源会话已变化，请刷新预览后再操作。")
