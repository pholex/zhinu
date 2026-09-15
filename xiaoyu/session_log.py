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
- 写锁：一个会话文件同一时刻只允许一个写句柄（见 SessionLog 的 docstring）。
  锁在旁车 `<日志>.lock` 上，读日志不受影响；抢不到直接抛 SessionLockedError，
  由续写入口（CLI --session-id / ACP session/load / serve 重启恢复）各自报错
- 读回：只有最后一行解析失败静默跳过（崩溃留下的半行）；中段坏行计入
  load_messages 返回值的 corrupt_lines，Agent.restore 据此提示用户
- 只由 CLI 注入；eval 和 explore 子 agent 不落盘
- 退出约定：能记录的退出路径（正常退出、SIGTERM/SIGHUP、未捕获异常）都会
  写一条 exit / error 事件；断电、kill -9、Windows 直接关终端窗口无法记录。
  因此**文件末尾没有 exit 事件 = 异常终止**——事后拿到用户日志，
  可据此区分"模型没答"和"进程死了"。
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import signal
import threading
import weakref
from dataclasses import dataclass, field
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


# ---------- 写锁：一个会话文件同一时刻只有一个写句柄 ----------
#
#  为什么要锁：写入是每条记录一次 open("a") 追加，>8KB 的记录会分多次 write。
#  两个进程续写同一个会话（编辑器经 ACP 与终端 CLI 同时接上同名会话）时，
#  两边的分片会交错成坏行——读回时丢消息，丢的若是带 tool_calls 的 assistant，
#  发请求前的历史修复还会顺手删掉对应的 tool 结果，历史被悄悄改写。
#
#  怎么锁：对日志旁边单独的 `<日志>.lock` 加内核排他锁（POSIX flock /
#  Windows msvcrt.locking 锁第 0 字节）。
#  - 锁旁车而不是日志本身：Windows 的字节锁是强制锁，锁日志会挡住读的一方
#    （resume 列表、digest、另一个进程抢锁失败前后的读）；
#  - 内核锁随进程死亡自动释放，不需要 TTL / 续约 / 陈旧锁判定；
#  - 释放时**不删**锁文件：删除与另一进程"打开旧 inode 并加锁"之间有竞态，
#    两边会各自锁住不同的 inode 而都以为自己独占；留一个几十字节的文件无害；
#  - 锁文件里写持有者 pid，只为报错信息可读。pid 从第 1 字节写起——第 0 字节
#    在 Windows 上被锁着，别的进程读不了那一段。

_WINDOWS = os.name == "nt"
#  pid 定宽写入：锁文件不截断（Windows 上截断被锁区域的行为不可靠），定宽覆盖即可
_PID_WIDTH = 20


#  会话文件与目录的权限：JSONL 里有工具输出，可能带着密钥——POSIX 上仅本人可读写。
#  umask 只会再收紧、不会放宽，所以创建时直接给 0600 / 0700 即可生效；
#  Windows 上 mode 参数与 chmod 基本无语义，失败一律忽略（与用户级 .env 同一纪律）。
_FILE_MODE = 0o600
_DIR_MODE = 0o700


def _ensure_private_dir(directory: Path) -> None:
    """建会话目录：本进程新建出来的每一层都给 0700。

    已存在的层不动——可能是嵌入宿主显式传的目录，替宿主改权限是越界。
    建不出来原样抛 OSError，由写入端的停写兜底。
    """
    missing: list[Path] = []
    probe = directory
    while not probe.exists() and probe.parent != probe:
        missing.append(probe)
        probe = probe.parent
    for level in reversed(missing):
        #  并发创建（两个会话同时首写同一工作区子目录）：别人先建了就用别人的
        with contextlib.suppress(FileExistsError):
            level.mkdir(mode=_DIR_MODE)


def _open_private(path: Path, flags: int) -> int:
    """按 0600 打开（不存在即创建）会话相关文件，返回 fd。

    已存在但权限比 0600 宽的（旧版本按 umask 建出来的 0644）顺手收紧；
    收紧失败（文件不归本 uid 等）不影响写入。
    """
    fd = os.open(path, flags | os.O_CREAT | getattr(os, "O_BINARY", 0), _FILE_MODE)
    if not _WINDOWS:
        with contextlib.suppress(OSError):
            if os.fstat(fd).st_mode & 0o077:
                os.fchmod(fd, _FILE_MODE)
    return fd


def lock_path(path: Path) -> Path:
    """会话文件对应的锁文件：`<名字>.jsonl.lock`（不匹配 `*.jsonl`，列举不受影响）。"""
    return path.with_name(path.name + ".lock")


def _session_label(path: Path) -> str:
    """报错里用的会话称呼：命名会话取名字，匿名会话取文件名。

    文件名是 <时间戳>-<pid>[-id-<名字>]，前两段不含 `-id-`，第一次出现就是分隔符。
    """
    _, mark, name = path.stem.partition(_NAMED_MARK)
    return name if mark else path.name


class SessionLockedError(RuntimeError):
    """会话文件正被另一个写句柄持有（多半是另一个进程）。消息面向用户。"""

    def __init__(self, path: Path, pid: int | None) -> None:
        self.path = path
        self.pid = pid
        if pid is None:
            who = "另一个进程"
        elif pid == os.getpid():
            who = f"本进程（pid {pid}）里另一个写句柄"
        else:
            who = f"另一个进程（pid {pid}）"
        super().__init__(
            f"会话 {_session_label(path)} 正被{who}写入；"
            f"先结束那边再续写（锁文件 {lock_path(path)}）"
        )


def _try_lock(fd: int) -> bool:
    """非阻塞抢排他锁：抢到 True，被别人持有 False；其余 OSError 原样抛出。"""
    if _WINDOWS:
        import msvcrt

        #  msvcrt.locking 从当前文件位置起锁 nbytes 字节：先回到第 0 字节
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        except OSError:
            #  被占时报 EACCES/EDEADLOCK；locking 没有别的常见失败形态，一律按被占处理
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _write_pid(fd: int, pid: int | None) -> None:
    """第 0 字节占位，pid 从第 1 字节起定宽写；None = 清空（放锁前抹掉，免得
    下一个抢锁失败的人读到已退出进程的 pid）。"""
    body = b"" if pid is None else str(pid).encode("ascii")
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, b"\n" + body.ljust(_PID_WIDTH) + b"\n")


def _release_lock_fd(fd: int) -> None:
    """清掉 pid、解锁、关 fd。给 weakref.finalize 用，所以是模块函数且不引用 SessionLog。"""
    with contextlib.suppress(OSError):
        _write_pid(fd, None)
    with contextlib.suppress(OSError):
        if _WINDOWS:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    with contextlib.suppress(OSError):
        os.close(fd)


def _holder_pid(lock: Path) -> int | None:
    """读锁文件里的持有者 pid；读不到/没写完/已清空都回 None（只用于报错文案）。"""
    try:
        with lock.open("rb") as handle:
            handle.seek(1)
            raw = handle.read(_PID_WIDTH + 1).strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


class SessionLog:
    """一个会话文件的写句柄。

    **构造即抢写锁**（见上方"写锁"一节），抢不到抛 SessionLockedError——
    续写同一个文件的入口（open_named / ACP session/load / serve 重启恢复）都经
    这里，谁先拿到谁写，后来者明确报错，不会两边交错写坏文件。
    锁的持有期 = 句柄的写入期：
    - close()：写 exit 事件后放锁（正常退出、信号、atexit、ACP 断连、serve 关会话）；
    - release()：只放锁不写 exit（同进程重载顶替旧句柄、serve 停机后由清单接回）；
    - 句柄被丢弃（装配中途失败没人 close）：回收时由 finalizer 放锁；
    - 进程死亡：内核放锁。
    放锁之后的写入一律丢弃：锁一放别的进程随时可能接手，迟到的记录不能再落进去。
    子 agent（explore / 委托）不落盘，不持锁。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._broken = False
        self._closed = False
        #  写入与放锁互斥：serve 关会话（事件循环线程）可能与工作线程的收尾写入并发
        self._mutex = threading.Lock()
        self._released = False
        self._lock_finalizer: weakref.finalize | None = None
        self.acquire()

    def acquire(self) -> None:
        """抢写锁（构造时自动调用；release 之后可再调一次重新拿回）。

        抢不到抛 SessionLockedError。锁文件建不出来、或文件系统不支持加锁（部分
        网络盘）时**无锁降级**照常写：锁是防交错的护栏，不是写日志的前提——
        与"写失败绝不影响会话"同一纪律。
        """
        with self._mutex:
            if self._lock_finalizer is not None and self._lock_finalizer.alive:
                return
            lock = lock_path(self.path)
            try:
                _ensure_private_dir(lock.parent)
                fd = _open_private(lock, os.O_RDWR)
            except OSError:
                self._released = False
                return
            try:
                locked = _try_lock(fd)
            except OSError:
                os.close(fd)
                self._released = False
                return
            if not locked:
                os.close(fd)
                raise SessionLockedError(self.path, _holder_pid(lock))
            with contextlib.suppress(OSError):
                _write_pid(fd, os.getpid())
            self._lock_finalizer = weakref.finalize(self, _release_lock_fd, fd)
            #  进程退出时不必跑：内核会放锁；跑了反而可能抢在 atexit 的 exit 事件之前
            self._lock_finalizer.atexit = False
            self._released = False
        self._seal_torn_tail()

    def release(self) -> None:
        """放掉写锁并停写（幂等），不写 exit 事件。"""
        with self._mutex:
            self._released = True
            if self._lock_finalizer is not None:
                self._lock_finalizer()

    def _seal_torn_tail(self) -> None:
        """续写前给崩溃留下的半行补上换行，并记一条 torn_tail 事件。

        不补的话下一条记录会接在半行后面、跟着一起解析失败。事件是给读回用的
        标记：紧挨在它前面的坏行是已知的崩溃尾巴，不算中段损坏（见 load_messages）。
        只在持锁后做——此时没有别的写者，文件末尾就是真的末尾。
        """
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    return
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) == b"\n":
                    return
            fd = _open_private(self.path, os.O_WRONLY | os.O_APPEND)
            try:
                os.write(fd, b"\n")
            finally:
                os.close(fd)
        except OSError:
            return  # 新会话（文件还不存在）或读写不了：交给 _write 的停写兜底
        self.event("torn_tail")

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
        self.release()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _write(self, record: dict[str, Any]) -> None:
        with self._mutex:
            if self._broken or self._released:
                return
            try:
                _ensure_private_dir(self.path.parent)
                fd = _open_private(self.path, os.O_WRONLY | os.O_APPEND)
                try:
                    handle = os.fdopen(fd, "a", encoding="utf-8")
                except BaseException:
                    os.close(fd)
                    raise
                with handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                #  磁盘满/权限问题不能影响会话，停写即可；写不进去的句柄也没理由占着锁
                self._broken = True
                if self._lock_finalizer is not None:
                    self._lock_finalizer()


# ---------- 退出事件：进程级钩子 ----------


#  在册的会话日志，按**日志文件**索引（不是按会话对象）。覆盖登记的理由见
#  install_exit_logging 的 docstring。
_exit_logs: dict[Path, SessionLog] = {}
_exit_hooks_installed = False


def _close_registered(reason: str) -> None:
    """关掉在册的全部日志，用同一个 reason。close 幂等，重复触发无害。"""
    for log in list(_exit_logs.values()):
        log.close(reason)


def install_exit_logging(log: SessionLog | None) -> None:
    """把会话日志挂上进程级退出钩子，退出时给它落一条 exit 事件。

    可记录：正常退出（atexit）、SIGTERM / SIGHUP（POSIX 的 kill、关终端窗口）、
    SIGBREAK（Windows Ctrl-Break）。不可记录：断电、kill -9、Windows 直接
    关闭终端窗口——约定为「文件末尾没有 exit 事件 = 异常终止」（见模块
    docstring），事后拿到用户日志可据此区分"模型没答"和"进程死了"。

    **钩子全进程只装一次，触发时用同一个 reason 关掉在册全部**：多会话进程
    （acp / serve）里每建一个会话就 register 一次 atexit、重装一遍信号处理器
    的话，信号处理器的闭包只认最后装的那个 log——SIGTERM 到达时只有最后一个
    会话被记成 signal:SIGTERM，其余在随后的 atexit 里被记成 normal。判据本身
    不破（close 幂等，不会写重），坏的是 reason：真实结局是被信号打断，日志
    里却写着正常退出，而 reason 正是事后区分"进程被掐"和"用户自己退"的依据。

    **按日志文件覆盖登记**（而不是无脑 add）：session/load 会在同一个文件上
    新建 SessionLog 并把旧的挤掉（acp.py 的 _sessions 直接覆盖），被挤掉的
    对象此后再也不会被写。两个都留在册里的话，退出时同一文件会写出两条
    exit 事件、第一条落在 reload 之后那段对话的**前面**——判据就从"末尾有
    没有 exit"劣化成"末尾这条是不是最后一条"，比没有钩子更难读。
    """
    global _exit_hooks_installed
    if log is None:
        return
    _exit_logs[log.path] = log
    if _exit_hooks_installed:
        return
    _exit_hooks_installed = True
    atexit.register(_close_registered, "normal")

    def _on_signal(signum: int, frame: Any) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        _close_registered(f"signal:{name}")
        #  记完照常退出：128+signum 是 shell 的信号退出码约定
        raise SystemExit(128 + signum)

    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        #  非主线程 / 受限环境注册不了就算了，不能影响启动
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            signal.signal(sig, _on_signal)


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


# ---------- digest：跨会话的用量账本 ----------
#
#  数据源是每个会话文件里的 `usage` 事件——agent 轮末写入的**累计**快照
#  （见 Agent._log_usage），所以每个文件只需取最后一条，不用重放求和。
#  没有 usage 事件的文件（旧版本记录的、或一次调用都没发生的）单独计数，
#  绝不静默略过——沉默会暗示"全都算进来了"。


@dataclass
class WorkspaceUsage:
    """一个工作区的累计用量。by_model 的 key 是 provider/model 全限定名。"""

    sessions: int = 0
    #  model -> [calls, prompt_tokens, completion_tokens]（可变，聚合时原地加）
    by_model: dict[str, list[int]] = field(default_factory=dict)

    def add(self, by_model: dict[str, Any]) -> None:
        self.sessions += 1
        for model, entry in by_model.items():
            if not isinstance(entry, dict):
                continue
            bucket = self.by_model.setdefault(str(model), [0, 0, 0])
            for slot, key in enumerate(("calls", "prompt_tokens", "completion_tokens")):
                value = entry.get(key)
                if isinstance(value, int):
                    bucket[slot] += value

    @property
    def prompt_tokens(self) -> int:
        return sum(entry[1] for entry in self.by_model.values())

    @property
    def completion_tokens(self) -> int:
        return sum(entry[2] for entry in self.by_model.values())


@dataclass
class UsageDigest:
    by_workspace: dict[str, WorkspaceUsage] = field(default_factory=dict)
    no_usage: int = 0  # 没有 usage 事件的会话文件数（旧版本记录 / 零调用）
    corrupt: int = 0  # 疑似 usage 行但解析失败、被跳过的行数


#  usage 事件行的预筛子串（json.dumps 的 `": "` 分隔符是稳定的）：
#  digest 要扫所有会话文件的每一行，先按子串筛掉消息行，命中的才 json 解析。
_USAGE_MARK = '"event": "usage"'


def _tail_usage(path: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int]:
    """单次遍历取 (meta, 最后一条 usage 事件, 疑似 usage 行损坏数)。

    损坏计数的口径：含 usage 标记但解析不出来的行（典型是断电截断的尾部
    半行）。截断发生在标记之前的行探测不到——这属于观察边界，digest 的
    输出文案只声称"跳过了 N 行疑似用量记录"，不声称抓到了所有损坏。
    """
    meta: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    corrupt = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for index, line in enumerate(handle):
                is_candidate = _USAGE_MARK in line
                if meta is None and index < _HEAD_SCAN_LINES:
                    pass  # 开头几行照常解析找 meta
                elif not is_candidate:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    if is_candidate:
                        corrupt += 1
                    continue
                kind = record.get("event")
                if kind == "meta" and meta is None:
                    meta = record
                elif kind == "usage":
                    usage = record
    except OSError:
        return None, None, 0
    return meta, usage, corrupt


def usage_digest(workspace: str | None = None) -> UsageDigest:
    """扫全部会话文件，按工作区聚合 token 用量（"配额花在哪了"）。

    文件收集规则与 list_sessions 一致（工作区子目录 + 分区前的存量平铺），
    但不设条数上限——账本要的就是全量。workspace 过滤以 meta 为准。
    """
    digest = UsageDigest()
    directory = sessions_dir()
    if not directory.is_dir():
        return digest
    candidates = list(directory.glob("*.jsonl"))
    if workspace:
        candidates += list((directory / _workspace_slug(workspace)).glob("*.jsonl"))
    else:
        candidates += list(directory.glob("*/*.jsonl"))
    for path in candidates:
        meta, usage, corrupt = _tail_usage(path)
        digest.corrupt += corrupt
        if meta is None:
            continue  # 没有 meta 的文件连会话都算不上，与 list_sessions 同口径
        ws = str(meta.get("workspace", ""))
        if workspace and ws != workspace:
            continue
        by_model = usage.get("by_model") if usage else None
        if not isinstance(by_model, dict) or not by_model:
            digest.no_usage += 1
            continue
        digest.by_workspace.setdefault(ws, WorkspaceUsage()).add(by_model)
    return digest


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

    会话正被别的写句柄持有时抛 SessionLockedError。先抢锁再读历史：读到的
    就是此刻完整的文件，不会是另一个进程写到一半的样子。
    """
    existing = find_named(session_id, workspace, directory)
    if existing is None:
        return SessionLog.create(model, workspace, directory, session_id=session_id), []
    log = SessionLog(existing)
    try:
        messages = load_messages(existing)
    except BaseException:
        log.release()  # 格式太新 / 读失败：锁不能跟着泄漏到进程退出
        raise
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
        #  harness 注入的说明（plan mode 进出、环境差分等）不算一轮，判据全仓一份
        if media.is_injected_user_text(text, exclude_texts):
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


def last_world_state(path: Path) -> dict[str, Any] | None:
    """会话文件里最后记录的环境播报基线；没有回 None（= 未知，下一步全量播报）。

    Agent 在基线变化时写 world_state 事件（见 world_state 模块）。顺序遍历、
    最后一条说了算；compact/clear 不影响它——基线描述的是环境，不是历史。
    """
    baseline: dict[str, Any] | None = None
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
                if record.get("event") == "world_state" and isinstance(
                    record.get("baseline"), dict
                ):
                    baseline = record["baseline"]
    except OSError:
        return None
    return baseline


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


class LoadedMessages(list):
    """load_messages 的返回值：就是消息列表，外加读回时发现的中段损坏。

    corrupt_lines：解析失败被跳过、且影响接回内容的行号（从 1 起）。做成 list
    子类而不是改返回类型：公开契约里 load_messages 返回 list，嵌入宿主
    `agent.restore(load_messages(path))` 一行不改就能在 restore 时收到提示。
    切片会退化成普通 list、丢掉这个属性——要切片的调用方自己带上。
    """

    def __init__(self, messages: Any = (), corrupt_lines: Any = ()) -> None:
        super().__init__(messages)
        self.corrupt_lines: list[int] = list(corrupt_lines)


def load_messages(path: Path) -> LoadedMessages:
    """重放一个会话文件，返回可直接接到 system prompt 之后的消息列表。

    重放规则（顺序遍历，语义在写入端已经定死）：
    - 消息行（有 role）→ append；
    - compact 事件带 replacement → 整体替换为压缩后的历史；
    - clear 事件 → 清空；
    - 其余事件（meta / microcompact / 旧格式 compact）→ 跳过。
    遇到比当前实现新的格式版本直接拒绝——静默错乱比失败更糟。

    坏行（解析失败、或解析出来不是对象）一律跳过，但分两种：
    - **最后一行**、或紧跟着 torn_tail 事件的那一行：崩溃留下的半行，正常形态，静默；
    - 其余（中段）：多半是并发写交错或磁盘损坏，丢的可能是带 tool_calls 的
      assistant——发请求前的历史修复会连带删掉对应 tool 结果，用户毫无感知。
      所以记进返回值的 corrupt_lines，由 Agent.restore 提示出来。
    compact / clear 会整体重建历史，之前的坏行不影响接回内容，随之清零。
    """
    messages: list[dict[str, Any]] = []
    corrupt: list[int] = []
    #  最近一条还没定性的坏行：看下一条有效记录（或文件结束）才知道它是不是尾巴
    pending_bad = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                record = None
            if not isinstance(record, dict):
                if pending_bad:
                    corrupt.append(pending_bad)
                pending_bad = number
                continue
            if pending_bad:
                if record.get("event") != "torn_tail":
                    corrupt.append(pending_bad)
                pending_bad = 0
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
                    corrupt = []
                elif kind == "clear":
                    messages = []
                    corrupt = []
                continue
            if "role" in record:
                messages.append({key: value for key, value in record.items() if key != "ts"})
    #  循环结束还挂着的 pending_bad 就是最后一行：尾部半行，静默
    return LoadedMessages(messages, corrupt)
