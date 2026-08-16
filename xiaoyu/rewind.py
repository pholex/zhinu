"""rewind：按轮次给文件打快照，/rewind 把工作区（和对话）回滚到某轮之前。

checkpoint 设计，按小羽体量裁剪：

- **事件驱动的懒惰首写捕获**：不扫全目录。write_file / str_replace 改文件前把
  "改前内容"记进当前轮的快照（每轮每文件只记第一次——恢复目标是"本轮开始
  前"的状态）；轮次结束再补一份"改后内容"（after），专供外部改动检测。
- **只追踪工具改过的文件**：bash 里改的、git 操作、workspace 外的路径都不在
  快照范围——/rewind 的提示里明说，不装作全能。
- **恢复语义**："回到第 N 轮开始前"。从 ≥N 的所有点里取每个文件**最早**的
  before 快照写回；before 是 None（当时不存在）就删除该文件。全部成功才丢弃
  ≥N 的点，出错则整批保留供重试。
- **外部改动检测**：恢复前把当前磁盘内容与最近一份 after 快照比对，不一致
  说明轮次之外有人改过（用户手改/别的进程），列出来让用户确认再动手。
- 对话与文件可分开回滚（conversation_only / files_only / all 三态）。
  只回对话时快照点保留——文件没动，之后仍可单独回滚文件。

磁盘镜像、git HEAD/index 域、hunk delta 三类更重的链路刻意不做：
快照只活在会话进程内存里，体量由 MAX_POINTS / MAX_FILE_BYTES 兜底。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

#  最多保留多少个快照点（超出淘汰最旧）
MAX_POINTS = 64
#  单文件超过这个字节数不快照（该文件从此轮起不可回滚，点上有标记）
MAX_FILE_BYTES = 5 * 1024 * 1024


@dataclass
class RewindPoint:
    index: int  # 第几轮（从 1 开始，随会话递增）
    prompt_text: str  # 该轮用户输入原文（对话回滚按它定位截断点）
    started_at: float = field(default_factory=time.time)
    #  路径（绝对） → 本轮首次改动前的内容；None = 当时不存在（新建文件）
    files: dict[str, str | None] = field(default_factory=dict)
    #  轮次结束时的内容（只覆盖本轮碰过的文件），外部改动检测用
    after: dict[str, str | None] = field(default_factory=dict)
    #  超限没拍下的文件：这些文件无法经本点回滚
    skipped: list[str] = field(default_factory=list)

    @property
    def preview(self) -> str:
        line = next(
            (part.strip() for part in self.prompt_text.splitlines() if part.strip()), ""
        )
        return line if len(line) <= 60 else line[:57] + "…"


class RewindStore:
    """一个会话的快照点序列。线程模型：全部调用都发生在会话自己的执行线程。"""

    def __init__(self) -> None:
        self._points: list[RewindPoint] = []
        self._counter = 0
        self._current: RewindPoint | None = None

    # ---------- 轮次边界（Agent.send 调用） ----------

    def begin(self, prompt_text: str) -> None:
        self._counter += 1
        self._current = RewindPoint(index=self._counter, prompt_text=prompt_text)

    def finish(self) -> None:
        point, self._current = self._current, None
        if point is None:
            return
        #  after 快照：只读本轮碰过的文件的当前内容
        for raw in point.files:
            point.after[raw] = _read_or_none(Path(raw))
        self._points.append(point)
        if len(self._points) > MAX_POINTS:
            del self._points[0]

    # ---------- 捕获（tools 的写路径调用） ----------

    def record(self, path: Path, before: str | None) -> None:
        """记一份"改前内容"。不在轮次内（工具被 REPL 外壳直接调）就静默跳过；
        每轮每文件只记第一次（首写获胜——目标是本轮开始前的状态）。"""
        if self._current is None:
            return
        raw = str(path)
        if raw in self._current.files or raw in self._current.skipped:
            return
        if before is not None and len(before.encode("utf-8", "ignore")) > MAX_FILE_BYTES:
            self._current.skipped.append(raw)
            return
        self._current.files[raw] = before

    # ---------- 查询 ----------

    def points(self) -> list[RewindPoint]:
        return list(self._points)

    def get(self, index: int) -> RewindPoint | None:
        return next((p for p in self._points if p.index == index), None)

    def files_from(self, index: int) -> dict[str, str | None]:
        """≥index 的所有点里，每个文件**最早**的 before 快照（恢复用）。"""
        merged: dict[str, str | None] = {}
        for point in self._points:
            if point.index < index:
                continue
            for raw, before in point.files.items():
                merged.setdefault(raw, before)
        return merged

    def conflicts(self, index: int) -> list[str]:
        """恢复目标里"当前磁盘内容 ≠ 最近一份 after 快照"的文件（外部改过）。"""
        latest_after: dict[str, str | None] = {}
        for point in self._points:
            if point.index < index:
                continue
            latest_after.update(point.after)
        clashing = []
        for raw in self.files_from(index):
            if raw not in latest_after:
                continue  # 本轮还没收尾（不应发生）——没有 after 可比，不报
            if _read_or_none(Path(raw)) != latest_after[raw]:
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

        全部成功才丢弃 ≥index 的点；有错整批保留（可修复后重试）。
        """
        targets = self.files_from(index)
        if not targets:
            return True, "该点之后没有工具改动过的文件，文件无需恢复。"
        restored, removed, errors = 0, 0, []
        for raw, before in targets.items():
            path = Path(raw)
            try:
                if before is None:
                    if path.exists():
                        path.unlink()
                        removed += 1
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(before, encoding="utf-8")
                    restored += 1
            except OSError as exc:
                errors.append(f"{raw}: {exc}")
        if errors:
            listed = "；".join(errors[:5])
            return False, f"部分文件恢复失败（快照保留，可重试）：{listed}"
        self._points = [p for p in self._points if p.index < index]
        parts = []
        if restored:
            parts.append(f"恢复 {restored} 个文件")
        if removed:
            parts.append(f"删除 {removed} 个本轮新建的文件")
        return True, "、".join(parts) + "。"

    def drop_all(self) -> None:
        self._points.clear()
        self._current = None


def _read_or_none(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
