"""rewind：按轮次给文件打快照，/rewind 把工作区（和对话）回滚到某轮之前。

checkpoint 设计，按小羽体量裁剪：

- **事件驱动的懒惰首写捕获**：不扫全目录。write_file / str_replace 改文件前把
  "改前内容"记进当前轮的快照（每轮每文件只记第一次——恢复目标是"本轮开始
  前"的状态）；写入后立即确认内容指纹（after），专供外部改动检测。
- **只追踪工具改过的文件**：bash 里改的、git 操作、workspace 外的路径都不在
  快照范围——/rewind 的提示里明说，不装作全能。
- **恢复语义**："回到第 N 轮开始前"。从 ≥N 的所有点里取每个文件**最早**的
  before 快照写回；before 是 None（当时不存在）就删除该文件。全成或全不动：
  全部成功才丢弃 ≥N 的点；中途失败则把已写回的还原，快照整批保留供重试。
- **外部改动检测**：恢复前把当前磁盘内容与最近一份 after 快照比对，不一致
  说明轮次之外有人改过（用户手改/别的进程），列出来让用户确认再动手。
- **快照存原始字节，不解码**：编码（GBK 等）与换行（CRLF）原样恢复——存解码
  后的文本再按 UTF-8 写回，恢复本身就会改掉文件编码。
- **特殊文件不碰**：追踪的路径在轮次之外被换成 FIFO / 设备时，快照读取先 stat
  拦下（不 open，免得永久阻塞），冲突检测把它列为外部改动，恢复时原样跳过并
  报失败——不往 FIFO 里写，也不删别人建的节点。
- 对话与文件可分开回滚（conversation_only / files_only / all 三态）。
  只回对话时快照点保留——文件没动，之后仍可单独回滚文件。

磁盘镜像、git HEAD/index 域、hunk delta 三类更重的链路刻意不做：
有会话日志时保存首写前原始字节和写后指纹，重放时接回最近 MAX_POINTS 轮。
日志没有确认过的写入不能猜成成功；不挂日志时保持纯内存行为。
"""

from __future__ import annotations

import base64
import copy
import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from . import fsguard
from .artifacts import fingerprint_file

#  最多保留多少个快照点（超出淘汰最旧）
MAX_POINTS = 64
#  单文件超过这个字节数不快照（该文件从此轮起不可回滚，点上有标记）
MAX_FILE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class RewindResult:
    """Outcome of a rewind; failed file writes may require manual reconciliation."""

    checkpoint: int
    status: Literal["completed", "partial", "conflict", "failed", "unavailable", "noop"]
    conversation_rewound: bool = False
    files_rewound: bool = False
    restored_files: tuple[str, ...] = ()
    removed_files: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    skipped_files: tuple[str, ...] = ()
    uncertain_files: tuple[str, ...] = ()
    summary: str = ""
    #  被截掉区间里第一条真实用户消息的文本（媒体部件只取文本）；只在对话真的
    #  回退了才有。前端拿它预填下一轮输入行：回退多半是为了换个说法重来，
    #  原话填回去改两个字就能发，不必凭记忆重敲
    prompt_text: str = ""


@dataclass
class RewindPoint:
    index: int  # 第几轮（从 1 开始，随会话递增）
    prompt_text: str  # 该轮用户输入原文（对话回滚按它定位截断点）
    started_at: float = field(default_factory=time.time)
    #  路径（绝对） → 本轮首次改动前的内容；None = 当时不存在（新建文件）
    files: dict[str, bytes | None] = field(default_factory=dict)
    #  写入后确认的内容指纹（只覆盖本轮碰过的文件），外部改动检测用
    after: dict[str, str | None] = field(default_factory=dict)
    #  超限没拍下的文件：这些文件无法经本点回滚
    skipped: list[str] = field(default_factory=list)
    pending: set[str] = field(default_factory=set)
    conversation_available: bool = True

    @property
    def preview(self) -> str:
        line = next(
            (part.strip() for part in self.prompt_text.splitlines() if part.strip()), ""
        )
        return line if len(line) <= 60 else line[:57] + "…"


class RewindStore:
    """一个会话的快照点序列。线程模型：全部调用都发生在会话自己的执行线程。"""

    def __init__(self, workspace: Path | None = None) -> None:
        self._points: list[RewindPoint] = []
        self._counter = 0
        self._current: RewindPoint | None = None
        self.workspace = workspace.resolve() if workspace is not None else None
        self.journal: Callable[..., None] | None = None
        self.problem = ""

    def _event(self, action: str, **fields: Any) -> None:
        if self.journal is not None:
            self.journal("checkpoint", version=1, action=action, **fields)

    def _relative(self, raw: str) -> str:
        if self.workspace is None:
            raise ValueError("Checkpoint workspace is unknown")
        target = Path(raw)
        # 恢复仅接受原工作区内未被替换为符号链接的路径。
        if target.resolve() != target or not target.is_relative_to(self.workspace):
            raise ValueError("Checkpoint path is outside its original workspace")
        relative = target.relative_to(self.workspace).as_posix()
        if relative == "." or ".git" in Path(relative).parts:
            raise ValueError("Checkpoint path is not a user file")
        return relative

    # ---------- 轮次边界（Agent.send 调用） ----------

    def begin(self, prompt_text: str) -> None:
        self._counter += 1
        self._current = RewindPoint(index=self._counter, prompt_text=prompt_text)
        self._event("begin", index=self._counter, prompt=prompt_text,
                    workspace=str(self.workspace), started_at=self._current.started_at)

    def finish(self) -> None:
        point, self._current = self._current, None
        if point is None:
            return
        #  after 快照：只读本轮碰过的文件的当前内容
        for raw in point.files:
            # 没有挂日志的直接调用者沿用 finish 捕获；正式文件工具会在写后
            # 立即确认精确写入的字节，不能把用户之后的编辑当成本轮写入。
            if raw not in point.after and self.journal is None:
                content = _read_or_none(Path(raw))
                point.after[raw] = _fingerprint(content)
                point.pending.discard(raw)
        self._points.append(point)
        if len(self._points) > MAX_POINTS:
            del self._points[0]
        self._event("finish", index=point.index)

    # ---------- 捕获（tools 的写路径调用） ----------

    def record(self, path: Path, before: bytes | None) -> None:
        """记一份"改前内容"。不在轮次内（工具被 REPL 外壳直接调）就静默跳过；
        每轮每文件只记第一次（首写获胜——目标是本轮开始前的状态）。"""
        if self._current is None:
            return
        raw = str(path)
        if self.workspace is not None:
            try:
                self._relative(raw)
            except ValueError:
                return
        if raw in self._current.skipped:
            return
        if before is not None and len(before) > MAX_FILE_BYTES:
            if raw not in self._current.files:
                self._current.skipped.append(raw)
            self._current.pending.add(raw)
            if self.journal is not None:
                self._event("skip", path=self._relative(raw))
            return
        fields: dict[str, Any] = {}
        if raw not in self._current.files:
            self._current.files[raw] = before
            fields["before"] = base64.b64encode(before).decode("ascii") if before is not None else None
        self._current.pending.add(raw)
        if self.journal is not None:
            self._event("prepare", path=self._relative(raw), **fields)

    def written(self, path: Path, content: bytes) -> None:
        """紧接实际写入后确认版本；进程若死在两者之间，该路径保持未知。"""
        raw = str(path)
        if self._current is None or raw not in self._current.files:
            return
        fingerprint = _fingerprint(content)
        self._current.after[raw] = fingerprint
        self._current.pending.discard(raw)
        if self.journal is not None:
            self._event("written", path=self._relative(raw), fingerprint=fingerprint)

    # ---------- 查询 ----------

    def points(self) -> list[RewindPoint]:
        return list(self._points)

    def get(self, index: int) -> RewindPoint | None:
        return next((p for p in self._points if p.index == index), None)

    def files_from(self, index: int) -> dict[str, bytes | None]:
        """≥index 的所有点里，每个文件**最早**的 before 快照（恢复用）。"""
        merged: dict[str, bytes | None] = {}
        for point in self._points:
            if point.index < index:
                continue
            for raw, before in point.files.items():
                merged.setdefault(raw, before)
        return merged

    def conflicts(self, index: int) -> list[str]:
        """恢复目标里"当前磁盘内容 ≠ 最近一份 after 快照"的文件（外部改过）。"""
        latest_after: dict[str, str | None] = {}
        pending: set[str] = set()
        for point in self._points:
            if point.index < index:
                continue
            latest_after.update(point.after)
            pending.update(point.pending)
        clashing = []
        for raw in self.files_from(index):
            if raw in pending or raw not in latest_after:
                clashing.append(raw)
                continue
            #  被换成特殊文件的一律算外部改动：工具只会写出普通文件
            try:
                if self.workspace is not None:
                    self._relative(raw)
                _, current = fingerprint_file(Path(raw))
            except FileNotFoundError:
                current = None
            except (OSError, ValueError):
                clashing.append(raw)
                continue
            if current != latest_after[raw]:
                clashing.append(raw)
        return sorted(clashing)

    def skipped_from(self, index: int) -> list[str]:
        seen: set[str] = set()
        for point in self._points:
            if point.index >= index:
                seen.update(point.skipped)
        return sorted(seen)

    # ---------- 恢复 ----------

    def rewind_files(self, index: int) -> tuple[bool, str]:
        """把 ≥index 各点覆盖的文件恢复到第 index 轮开始前。返回 (成功?, 摘要)。

        全成或全不动：全部成功才丢弃 ≥index 的点；中途有一个写不回去，已经写回的
        那些按原样还原，工作区停在动手之前的样子，快照整批保留供重试。停在半截
        （一半文件是旧版、一半是新版）的工作区比不回滚更糟——而且重试时已回滚的
        文件会被冲突检测当成"外部改动"报出来。
        """
        targets = self.files_from(index)
        if not targets:
            return True, "该点之后没有工具改动过的文件，文件无需恢复。"
        if self.workspace is not None:
            try:
                for raw in targets:
                    self._relative(raw)
            except ValueError:
                return False, "文件路径已改变或离开工作区，未恢复。"
        #  先看全再动手：特殊文件在动任何文件之前就拦下
        blocked = [
            #  写 FIFO 会阻塞到有读者、删掉又可能误伤外部建的节点：原样不动，
            #  用户移走它之后可重试
            f"{raw}: 现在是{kind}，不是普通文件，未恢复（移走后可重试）"
            for raw in targets
            if (kind := _special_kind(Path(raw))) is not None
        ]
        if blocked:
            return False, "文件恢复没有开始（快照保留，可重试）：" + "；".join(blocked[:5])

        #  写盘前持久化不确定状态。进程在恢复中途退出时，接回不能再把这些
        # 路径当成可靠的写后版本；普通异常仍走原有逆序回退。
        self._event("restore_started", index=index)

        restored, removed = 0, 0
        undo: list[tuple[Path, bytes | None]] = []  # 动手前的样子，失败时逆序还原
        failure = ""
        for raw, before in targets.items():
            path = Path(raw)
            try:
                if self.workspace is not None:
                    self._relative(raw)
                if path.exists():
                    fsguard.require_regular(path)
                existed = path.exists()
                current = path.read_bytes() if existed else None
                if before is None:
                    if existed:
                        undo.append((path, current))
                        path.unlink()
                        removed += 1
                else:
                    undo.append((path, current))
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(before)
                    restored += 1
            except (OSError, ValueError) as exc:
                failure = f"{raw}: {exc}"
                break
        if failure:
            stuck = _undo(undo)
            if stuck:
                self._mark_uncertain(index)
                return False, (
                    f"文件恢复失败（{failure}），而且已写回的文件有 {len(stuck)} 个没能还原——"
                    f"工作区现在是半回滚状态，请逐个核对：{'；'.join(stuck[:5])}（快照保留）"
                )
            self._event("restore_cancelled", index=index)
            return False, f"文件恢复失败，工作区保持原样（快照保留，可重试）：{failure}"
        self._points = [p for p in self._points if p.index < index]
        self._event("drop", index=index)
        parts = []
        if restored:
            parts.append(f"恢复 {restored} 个文件")
        if removed:
            parts.append(f"删除 {removed} 个本轮新建的文件")
        return True, "、".join(parts) + "。"

    def drop_all(self, *, persist: bool = True) -> None:
        self._points.clear()
        self._current = None
        if persist:
            self._event("clear")

    def forget_conversation(self, index: int) -> None:
        for point in self._points:
            if point.index >= index:
                point.conversation_available = False
        self._event("conversation", index=index)

    def _mark_uncertain(self, index: int) -> None:
        for point in self._points:
            if point.index >= index:
                point.pending.update(point.files)

    def adopt(self, source: RewindStore, *, persist: bool) -> None:
        """接回同工作区的活动快照；新日志复制自己的记录，不依赖来源文件存续。"""
        if source.problem:
            self.problem = source.problem
            return
        if source.workspace != self.workspace:
            return
        self._points = copy.deepcopy(source.points())
        self._counter = source._counter
        self._current = None
        if not persist or self.journal is None:
            return
        self._event("clear", reset_counter=True)
        for point in self._points:
            self._event("begin", index=point.index, prompt=point.prompt_text,
                        workspace=str(self.workspace), started_at=point.started_at)
            for raw, before in point.files.items():
                # 加载时只做词法边界检查；外部链接变化留到恢复预检时报冲突。
                relative = Path(raw).relative_to(self.workspace).as_posix()
                self._event("prepare", path=relative,
                            before=base64.b64encode(before).decode("ascii") if before is not None else None)
                if raw in point.after and raw not in point.pending:
                    self._event("written", path=relative, fingerprint=point.after[raw])
            for raw in point.skipped:
                self._event("skip", path=Path(raw).relative_to(self.workspace).as_posix())
            self._event("finish", index=point.index)
            if not point.conversation_available:
                self._event("conversation", index=point.index)
        if self._counter:
            self._event("counter", index=self._counter)


class CheckpointReplay:
    """随会话日志顺序重放。快照元数据始终留在模型消息之外。"""

    def __init__(self) -> None:
        self.store = RewindStore()
        self.restoring: int | None = None
        self.seen = False

    def accept(self, record: dict[str, Any]) -> None:
        self.seen = self.seen or record.get("event") == "checkpoint"
        if record.get("event") == "clear" or (
            record.get("event") == "checkpoint" and record.get("version") == 1
            and record.get("action") == "clear"
        ):
            self.store.problem = ""
        if self.store.problem:
            return
        try:
            self._accept(record)
        except (KeyError, TypeError, ValueError, OSError):
            self.invalidate()

    def invalidate(self) -> None:
        self.store._points.clear()
        self.store._current = None
        self.store.problem = "文件快照记录损坏，已停用这份历史的文件回滚。"

    def _accept(self, record: dict[str, Any]) -> None:
        store = self.store
        if record.get("event") == "meta":
            raw = record.get("workspace")
            if isinstance(raw, str) and Path(raw).is_absolute():
                store.workspace = Path(raw).resolve()
            return
        if record.get("event") == "clear":
            store.drop_all()
            return
        if record.get("event") != "checkpoint":
            return
        if type(record.get("version")) is not int or record["version"] != 1:
            raise ValueError("Unsupported checkpoint version")
        action = record["action"]
        if action == "clear":
            store.drop_all()
            if record.get("reset_counter") is True:
                store._counter = 0
            self.restoring = None
            return
        if action in {"begin", "finish", "drop", "conversation", "counter", "restore_started", "restore_cancelled"}:
            index = record["index"]
            if type(index) is not int or index < 1:
                raise ValueError("Invalid checkpoint index")
            if action == "begin":
                if store.workspace is None or record["workspace"] != str(store.workspace):
                    raise ValueError("Checkpoint workspace mismatch")
                if not isinstance(record["prompt"], str) or index <= store._counter:
                    raise ValueError("Invalid checkpoint turn")
                if store._current is not None:
                    self._finish()
                store._counter = index
                store._current = RewindPoint(index, record["prompt"], float(record["started_at"]))
            elif action == "finish":
                if store._current is None or store._current.index != index:
                    raise ValueError("Invalid checkpoint completion")
                self._finish()
            elif action == "drop":
                store._points = [p for p in store._points if p.index < index]
                self.restoring = None
            elif action == "conversation":
                store.forget_conversation(index)
            elif action == "counter":
                store._counter = max(store._counter, index)
            elif action == "restore_started":
                self.restoring = index
            elif action == "restore_cancelled":
                self.restoring = None
            return
        point = store._current
        if point is None or store.workspace is None:
            raise ValueError("Checkpoint has no active turn")
        relative = record["path"]
        if not isinstance(relative, str) or (os.name == "nt" and "\\" in relative):
            raise ValueError("Invalid checkpoint path")
        path = Path(relative)
        if path.is_absolute() or path.drive or not path.parts or any(p in {"..", ".git"} for p in path.parts):
            raise ValueError("Invalid checkpoint path")
        raw = str(store.workspace / path)
        if action == "prepare":
            if raw not in point.files:
                before = record["before"]
                if before is not None:
                    if not isinstance(before, str) or len(before) > (MAX_FILE_BYTES + 2) // 3 * 4:
                        raise ValueError("Oversized checkpoint")
                    before = base64.b64decode(before, validate=True)
                    if len(before) > MAX_FILE_BYTES:
                        raise ValueError("Oversized checkpoint")
                point.files[raw] = before
            point.pending.add(raw)
        elif action == "written":
            fingerprint = record["fingerprint"]
            if raw not in point.files or not isinstance(fingerprint, str) or len(fingerprint) != 64:
                raise ValueError("Invalid checkpoint fingerprint")
            int(fingerprint, 16)
            point.after[raw] = fingerprint
            point.pending.discard(raw)
        elif action == "skip":
            if raw in point.files:
                point.pending.add(raw)
            elif raw not in point.skipped:
                point.skipped.append(raw)
        else:
            raise ValueError("Unknown checkpoint action")

    def _finish(self) -> None:
        if self.store._current is not None:
            self.store._points.append(self.store._current)
            self.store._points = self.store._points[-MAX_POINTS:]
            self.store._current = None

    def finish(self) -> RewindStore:
        self._finish()
        if not self.seen:
            self.store.problem = ""
        if self.restoring is not None:
            self.store._mark_uncertain(self.restoring)
        return self.store


def _fingerprint(content: bytes | None) -> str | None:
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _undo(journal: list[tuple[Path, bytes | None]]) -> list[str]:
    """按逆序把动过的文件还原成动手前的样子，返回没能还原的。"""
    stuck: list[str] = []
    for path, content in reversed(journal):
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        except OSError as exc:
            stuck.append(f"{path}: {exc}")
    return stuck


def _special_kind(path: Path) -> str | None:
    """路径当前若是 FIFO / 设备这类非普通文件，返回类别名；普通文件、不存在都给 None。"""
    try:
        fsguard.require_regular(path)
    except fsguard.NotRegularFile as exc:
        return exc.kind
    except OSError:
        return None
    return None


def _read_or_none(path: Path) -> bytes | None:
    """读当前内容；读不了给 None。

    先 stat 再 open：工具写过的路径在轮次之外可能被换成 FIFO（或指向设备的
    链接），直接 read_bytes 会把轮次收尾 / 冲突检测永久挂死、或读不到头。
    """
    try:
        fsguard.require_regular(path)
        return path.read_bytes()
    except OSError:
        return None
