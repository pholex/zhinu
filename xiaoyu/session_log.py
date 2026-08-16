"""会话落盘：每条消息 append 成 JSONL，支撑事后诊断与 `xiaoyu resume`。

设计（按小羽体量简化的会话持久化 + 重放重建）：
- 位置：<用户配置目录>/sessions/<工作区slug>/<时间戳>-<pid>.jsonl，一次会话一个
  文件。按工作区分子目录：不做"逐次传目录"的
  接口负担，路径转义即目录名；嵌入宿主要完全隔离时 `create(directory=...)`
  显式传目录。根目录下的平铺文件是分区之前的存量，列举时兼容
- 首行是 meta（格式版本、模型、工作区、开始时间），之后每行一条消息或事件
- compact 事件携带压缩后的完整历史（replacement）——resume 不必理解
  任何压缩语义，重放时撞到它就整体替换
- 写失败绝不影响会话本身：所有写入 try/except 全包，坏了就静默停写
- 只由 CLI 注入；eval 和 explore 子 agent 不落盘
- 退出约定：能记录的退出路径（正常退出、SIGTERM/SIGHUP、未捕获异常）都会
  写一条 exit / error 事件；断电、kill -9、Windows 直接关终端窗口无法记录。
  因此**文件末尾没有 exit 事件 = 异常终止**——事后拿到用户日志，
  可据此区分"模型没答"和"进程死了"。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__, media
from .config import user_config_dir

#  会话文件格式版本：resume 遇到比自己新的版本要明确拒绝，不能静默错乱。
#  v1 = 没有 format 字段的旧文件（消息结构相同，仍可重放）。
SESSION_FORMAT = 2


#  命名会话（`--session-id`）在文件名里的标记段：<时间戳>-<pid>-id-<会话名>.jsonl。
#  **时间戳前缀必须保留**——list_sessions 与 fork 全都靠"按文件名排序即时间序"，
#  改成 `<会话名>.jsonl` 会让命名会话永远排在列表最前（'n' > '2'）。
_NAMED_MARK = "-id-"

#  会话名字符集：它要当文件名用，所以只收这一撮明确安全的字符。
#  **不合规就报错，绝不悄悄改写**——把 `a/b` 和 `a-b` 洗成同一个名字，
#  等于让两个脚本以为各写各的、实际共用一个会话。
_SESSION_ID_EXTRA = "._-"
MAX_SESSION_ID = 64


def sessions_dir() -> Path:
    return user_config_dir() / "sessions"


def check_session_id(raw: str) -> str:
    """校验 `--session-id` 的取值，原样返回；不合规抛 ValueError（消息面向用户）。"""
    name = raw.strip()
    if not name:
        raise ValueError("会话名不能为空")
    if len(name) > MAX_SESSION_ID:
        raise ValueError(f"会话名最长 {MAX_SESSION_ID} 个字符，收到 {len(name)} 个")
    bad = {ch for ch in name if not (ch.isascii() and (ch.isalnum() or ch in _SESSION_ID_EXTRA))}
    if bad:
        raise ValueError(
            f"会话名只能用 ASCII 字母、数字和 {_SESSION_ID_EXTRA}，"
            f"不能有 {' '.join(sorted(bad))}（它要当文件名用）"
        )
    if not any(ch.isalnum() for ch in name):
        #  `.` `..` `-` 这类：过了字符集但仍是危险或无意义的文件名
        raise ValueError("会话名至少要有一个字母或数字")
    return name


def _workspace_slug(workspace: str) -> str:
    """工作区路径 → 子目录名（/Users/a/b → -Users-a-b）。

    非字母数字一律换成 `-`，天然覆盖 Windows 盘符与反斜杠。转义可能撞名
    （两个路径转出同一 slug），所以列举过滤仍以 meta 里的原始 workspace 为准，
    slug 只负责"同工作区的会话落在同一目录"这一分区职责。
    """
    slug = "".join(ch if ch.isalnum() else "-" for ch in workspace)
    #  文件系统单层目录名有长度上限（255 字节）：超长时保留更有区分度的尾部。
    #  80 字符按 CJK 三字节算也在限内；截断撞名同样由 meta 过滤兜底。
    if len(slug) > 80:
        slug = slug[-80:]
    return slug or "-"


class SessionLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._broken = False
        self._closed = False

    @classmethod
    def create(
        cls,
        model: str,
        workspace: str,
        directory: Path | None = None,
        session_id: str = "",
    ) -> "SessionLog":
        """directory 缺省时按工作区分子目录；嵌入宿主显式传目录即可完全隔离
        （常驻宿主的会话不再与 CLI 手动会话混在一起）。

        session_id 非空 = 命名会话（`--session-id`）：名字进文件名也进 meta。
        进文件名是为了免读文件就能定位，进 meta 是因为文件名的 `-id-` 段会被
        名字里的 `-id-` 骗到（`a-id-b` 与 `b`），最终认定以 meta 为准。
        """
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if directory is None:
            directory = sessions_dir() / _workspace_slug(workspace)
        mark = f"{_NAMED_MARK}{session_id}" if session_id else ""
        log = cls(directory / f"{stamp}-{os.getpid()}{mark}.jsonl")
        fields: dict[str, Any] = {"session_id": session_id} if session_id else {}
        log.event(
            "meta",
            format=SESSION_FORMAT,
            version=__version__,
            model=model,
            workspace=workspace,
            started_at=datetime.now().isoformat(timespec="seconds"),
            **fields,
        )
        return log

    def append(self, message: dict[str, Any]) -> None:
        """记一条对话消息（user / assistant / tool）。"""
        self._write({"ts": self._now(), **message})

    def event(self, kind: str, **fields: Any) -> None:
        """记一条非消息事件（meta / compact / clear …）。"""
        self._write({"ts": self._now(), "event": kind, **fields})

    def close(self, reason: str = "normal") -> None:
        """写退出事件（幂等：信号处理器和 atexit 可能先后都到，只记第一个）。

        见模块 docstring 的退出约定：没有 exit 事件的会话文件即异常终止。
        """
        if self._closed:
            return
        self._closed = True
        self.event("exit", reason=reason)

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _write(self, record: dict[str, Any]) -> None:
        if self._broken:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            #  磁盘满/权限问题不能影响会话，停写即可
            self._broken = True


# ---------- resume：列出与重放 ----------


@dataclass
class SessionInfo:
    path: Path
    started_at: str
    model: str
    workspace: str
    preview: str  # 首条用户消息的开头，作为"标题"
    session_id: str = ""  # `--session-id` 起的名字；匿名会话为空


#  列表时每个文件最多读这么多行：
#  meta 和首条用户消息都在文件开头，不必解析整个文件
_HEAD_SCAN_LINES = 10


def list_sessions(limit: int = 20, workspace: str | None = None) -> list[SessionInfo]:
    """按时间倒序列出历史会话（文件名即时间戳，跨目录按文件名排序仍是时间序）。

    会话按工作区分子目录存放；根目录下的平铺文件是分区之前的存量，一并列出。
    workspace 非空时只扫对应 slug 子目录 + 存量平铺文件，但过滤仍以 meta 里的
    原始 workspace 为准（slug 转义可能撞名）。每个文件只读头几行——
    拿 meta + 首条用户消息当标题就够了。
    """
    directory = sessions_dir()
    if not directory.is_dir():
        return []
    candidates = list(directory.glob("*.jsonl"))  # 存量平铺文件
    if workspace:
        candidates += list((directory / _workspace_slug(workspace)).glob("*.jsonl"))
    else:
        candidates += list(directory.glob("*/*.jsonl"))
    infos: list[SessionInfo] = []
    for path in sorted(candidates, key=lambda p: p.name, reverse=True):
        info = _head_info(path)
        if info is None:
            continue
        if workspace and info.workspace != workspace:
            continue
        infos.append(info)
        if len(infos) >= limit:
            break
    return infos


def _head_info(path: Path) -> SessionInfo | None:
    meta: dict[str, Any] = {}
    preview = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                if index >= _HEAD_SCAN_LINES:
                    break
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event") == "meta" and not meta:
                    meta = record
                elif record.get("role") == "user" and not preview:
                    preview = " ".join(media.text_of(record.get("content")).split())[:60]
                    break
    except OSError:
        return None
    if not meta:
        return None
    return SessionInfo(
        path=path,
        started_at=str(meta.get("started_at", "")),
        model=str(meta.get("model", "")),
        workspace=str(meta.get("workspace", "")),
        preview=preview or "（无用户消息）",
        session_id=str(meta.get("session_id", "")),
    )


# ---------- 命名会话（--session-id）：有则续、无则建 ----------


def find_named(session_id: str, workspace: str, directory: Path | None = None) -> Path | None:
    """按名字找命名会话文件；找不到回 None。

    先按文件名 glob 收窄（免得读整个目录的头几行），再拿 meta 里的 session_id
    逐个核对——文件名的 `-id-` 段不是无歧义的分隔符（会话名自己也能含 `-id-`），
    只有 meta 说了算。同名多个文件只可能来自并发新建，取最新的那个。
    """
    if directory is None:
        directory = sessions_dir() / _workspace_slug(workspace)
    if not directory.is_dir():
        return None
    candidates = sorted(
        directory.glob(f"*{_NAMED_MARK}{session_id}.jsonl"), key=lambda p: p.name, reverse=True
    )
    for path in candidates:
        info = _head_info(path)
        if info is not None and info.session_id == session_id:
            return path
    return None


def open_named(
    session_id: str, model: str, workspace: str, directory: Path | None = None
) -> tuple["SessionLog", list[dict[str, Any]]]:
    """`--session-id` 的落点：返回 (会话日志, 要接回的历史消息)。

    没找到同名会话就新建一个，历史为空；找到就**接着往同一个文件写**，
    并回放它的历史。这是与 `resume` 刻意不同的一处：resume 每次开新文件
    （每份自包含），命名会话则是"一个名字一个文件、反复续写"——脚本按固定
    名字调 N 次不该在盘上留下 N 份越滚越大的副本，那是 O(N²) 的写入量。
    因此接回的历史**不能再抄一遍进文件**（见 Agent.restore 的 copy=False）。
    """
    existing = find_named(session_id, workspace, directory)
    if existing is None:
        return SessionLog.create(model, workspace, directory, session_id=session_id), []
    messages = load_messages(existing)
    log = SessionLog(existing)
    #  续写点留痕：这一轮用的是哪个模型、哪个版本（meta 记的是首次创建时的）
    log.event("reopened", version=__version__, model=model, messages=len(messages))
    return log, messages


def turn_starts(
    messages: list[dict[str, Any]], exclude_texts: frozenset[str] = frozenset()
) -> list[int]:
    """会话消息里"一轮开头"的下标列表（session fork 按 turn 枚举分叉的
    截断点）。

    小羽的会话文件没有显式 turn 标记，按 user 消息近似：harness 注入的
    已知文案（nudge / 收尾指令 / plan mode 说明）用 exclude_texts 剔除；
    steer 插话与真轮次无法机械区分，会被当成一轮列出——列表带预览，
    由用户看着选，误差可接受。压缩 replacement 里备份的用户原话同理。
    """
    starts: list[int] = []
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        text = media.text_of(message.get("content"))
        if not text.strip() or text in exclude_texts:
            continue
        #  "[系统提示]" 开头 = harness 注入的说明（plan mode 进出等）。有些注入
        #  文案按会话格式化（含路径），exclude_texts 的精确匹配够不着，按前缀兜底
        if text.startswith("[系统提示]"):
            continue
        starts.append(index)
    return starts


def last_model(path: Path) -> str:
    """会话文件里最后生效的模型；读不出来回空串（调用方保持现配置即可）。

    meta 起底（创建时的模型），之后被每条 model 事件（Agent.switch_model 的
    留痕：/model、ACP 下拉框、降级链粘性切换）与 reopened 事件（历次续写
    实际用的模型，覆盖本机制落地前的存量文件）依次覆盖——顺序遍历，最后
    写的说了算。ACP session/load"恢复时跟随旧模型"靠它。
    """
    model = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event") in ("meta", "model", "reopened"):
                    value = record.get("model")
                    if isinstance(value, str) and value.strip():
                        model = value.strip()
    except OSError:
        return ""
    return model


def last_mode(path: Path) -> str:
    """会话文件里最后生效的交互模式；没有留痕回空串（调用方保持现配置）。

    只认 mode 事件（set_mode 与 exit_plan_mode 的留痕），顺序遍历、最后写的
    说了算。ACP session/load"恢复时跟随旧模式"靠它——历史里可能带着
    "已开启 plan mode"的注入说明，模式不跟上就是模型以为在规划态而关卡
    全开（或反过来），两边说的不是同一件事。
    """
    mode = ""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if record.get("event") == "mode":
                    value = record.get("value")
                    if isinstance(value, str) and value.strip():
                        mode = value.strip()
    except OSError:
        return ""
    return mode


def has_orphan_compact(path: Path) -> bool:
    """会话文件里是否有未配对的 compact_start（= 死在压缩中途）。

    压缩日志锁（start‖end 括号配对）：
    Agent 在摘要调用**之前**写 compact_start、结束后写 compact_end（成败都写）。
    锁最后释放，崩溃在中途就留下可检测的孤儿 start，而不是一条谎称压缩完成的
    记录。历史本身无损——replacement 没写入就还是原文，本函数只管诊断。
    OSError 由调用方处理（诊断失败不该影响 resume 本身）。
    """
    open_lock = False
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = record.get("event")
            if kind == "compact_start":
                open_lock = True
            elif kind == "compact_end":
                open_lock = False
    return open_lock


def load_messages(path: Path) -> list[dict[str, Any]]:
    """重放一个会话文件，返回可直接接到 system prompt 之后的消息列表。

    重放规则（顺序遍历，语义在写入端已经定死）：
    - 消息行（有 role）→ append；
    - compact 事件带 replacement → 整体替换为压缩后的历史；
    - clear 事件 → 清空；
    - 其余事件（meta / microcompact / 旧格式 compact）→ 跳过。
    遇到比当前实现新的格式版本直接拒绝——静默错乱比失败更糟。
    """
    messages: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                #  尾部半行（写入中断）跳过；resume 的价值在于尽量恢复
                continue
            if "event" in record:
                kind = record["event"]
                if kind == "meta":
                    fmt = record.get("format", 1)
                    if isinstance(fmt, int) and fmt > SESSION_FORMAT:
                        raise ValueError(
                            f"会话文件格式版本 {fmt} 比当前支持的 {SESSION_FORMAT} 新，"
                            "请升级 xiaoyu 后再 resume。"
                        )
                elif kind in ("compact", "rewind") and isinstance(
                    record.get("replacement"), list
                ):
                    #  rewind 与 compact 共用 replacement 机制：重放不需要理解
                    #  任何回滚语义，撞到即整体替换（旧版本会跳过 rewind 事件，
                    #  resume 出来的历史会多出被回滚的轮次——只影响旧版读新文件）
                    messages = list(record["replacement"])
                elif kind == "clear":
                    messages = []
                continue
            if "role" in record:
                messages.append({key: value for key, value in record.items() if key != "ts"})
    return messages
