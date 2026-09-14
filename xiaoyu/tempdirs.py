"""本进程临时目录的生命周期：谁创建谁清理 + 启动期清扫陈旧遗留。

两道闸，各管一种死法：

- **退出时删自己建的**（`make_dir` 登记 → atexit `cleanup_owned`）：正常退出与
  SIGTERM（session_log 把它转成 SystemExit，atexit 照跑）都覆盖。有内容的也删——
  spill 落盘与后台任务日志只在本进程内可寻址：recall 的短 id 与 task_output 的
  task id 都查内存里的表，resume 是新进程、新表，旧 id 查不到；消息历史里残留
  的绝对路径没有任何通道承诺跨进程可读。留着只是给 $TMPDIR 攒垃圾。
- **启动期清扫**（`sweep_in_background`）：kill -9、断电、Windows 删不掉被占用
  的文件——这些 atexit 救不了，靠下一个进程顺手扫。

清扫的安全边界（宁可漏删，绝不误删另一个在跑的会话）：

1. 只认本工具前缀（`STALE_PREFIXES`），不跟随符号链接，POSIX 上只动自己 uid 的；
2. 新目录名里带创建者 pid：**pid 还活着一律跳过**，不看 mtime——长会话（serve
   一跑几周、spill 目录一周没新文件）靠这条不被误删。pid 复用只会让判断偏向
   "活着"（少删），不会误删；
3. 另加年龄门槛（目录与其直接子项的最新 mtime 超过 `STALE_SECONDS`）：pid 判不准
   的情况（旧格式名字不带 pid、容器间共享 /tmp 看不到对方 pid）由它兜底；
4. 任何失败一律吞掉：清扫是顺手的事，不能影响启动。
"""

from __future__ import annotations

import atexit
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

#  超过这么久没动静才算陈旧（pid 活着的永远不算）
STALE_SECONDS = 7 * 86400
#  清扫只认这些前缀。xiaoyu-assert- / xiaoyu-probe- / xiaoyu-test-config- 是测试与
#  eval 探针的，一并收——历史遗留大头就是它们。
STALE_PREFIXES = (
    "xiaoyu-bg-",
    "xiaoyu-spill-",
    "xiaoyu-probe-",
    "xiaoyu-assert-",
    "xiaoyu-eval-",
    "xiaoyu-plugin-",
    "xiaoyu-test-config-",
)
#  make_dir 的命名：<前缀><pid>-<mkdtemp 随机串>。随机串字符集不含 "-"，
#  所以锚定到结尾就不会把旧格式的随机串误读成 pid。
_PID_NAME = re.compile(r"^(\d+)-[a-z0-9_]+$")

_owned: set[Path] = set()
_lock = threading.Lock()
_sweep_started = False


def make_dir(prefix: str) -> Path:
    """建一个本进程拥有的临时目录：名字带 pid，退出时自动删。"""
    path = Path(tempfile.mkdtemp(prefix=f"{prefix}{os.getpid()}-"))
    with _lock:
        _owned.add(path)
    return path


def discard(path: Path) -> None:
    """提前删掉一个自己的目录（幂等；删不掉就留给启动清扫）。"""
    with _lock:
        _owned.discard(path)
    _remove(path)


def cleanup_owned() -> None:
    """进程退出时删掉本进程建的全部目录（atexit 调，失败吞掉）。"""
    with _lock:
        paths = list(_owned)
        _owned.clear()
    for path in paths:
        _remove(path)


def _remove(path: Path) -> None:
    #  Windows 上刚被杀的进程可能还握着日志句柄：删不掉就算了
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:  # noqa: BLE001 - 清理绝不能让退出流程出异常
        pass


#  在 import 时注册而不是首次 make_dir 时：atexit 后注册先执行，这样本函数排在
#  后台任务收割、前台命令收割（它们都在导入本模块之后才注册）之后——先杀进程
#  再删目录，Windows 上删文件才不撞占用。
atexit.register(cleanup_owned)


# ---------- 启动期清扫 ----------


def sweep_in_background() -> None:
    """每进程至多一次，后台 daemon 线程里扫，不拖慢启动。"""
    global _sweep_started
    with _lock:
        if _sweep_started:
            return
        _sweep_started = True
    threading.Thread(target=_sweep_quietly, daemon=True, name="xiaoyu-tmp-sweep").start()


def _sweep_quietly() -> None:
    try:
        sweep_stale()
    except Exception:  # noqa: BLE001 - daemon 线程带着异常死掉会往 stderr 打栈
        pass


def sweep_stale(root: Path | None = None, *, max_age: float = STALE_SECONDS) -> list[Path]:
    """删掉 root（默认系统临时目录）下陈旧的本工具遗留，返回删掉的路径。"""
    import time

    base = Path(root) if root is not None else Path(tempfile.gettempdir())
    removed: list[Path] = []
    now = time.time()
    try:
        entries = list(os.scandir(base))
    except OSError:
        return removed
    for entry in entries:
        try:
            if _stale(entry, now, max_age):
                path = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(path)
                else:
                    path.unlink()
                removed.append(path)
        except Exception:  # noqa: BLE001 - 单个条目失败不影响其余
            continue
    return removed


def _stale(entry: os.DirEntry, now: float, max_age: float) -> bool:
    name = entry.name
    prefix = next((p for p in STALE_PREFIXES if name.startswith(p)), None)
    if prefix is None or entry.is_symlink():
        return False
    stat = entry.stat(follow_symlinks=False)
    if hasattr(os, "getuid") and stat.st_uid != os.getuid():
        return False
    if match := _PID_NAME.match(name[len(prefix):]):
        if _pid_alive(int(match.group(1))):
            return False
    newest = stat.st_mtime
    if entry.is_dir(follow_symlinks=False):
        #  目录 mtime 只随增删条目变；往已有日志里追加写只动文件 mtime
        with os.scandir(entry.path) as children:
            for child in children:
                newest = max(newest, child.stat(follow_symlinks=False).st_mtime)
    return now - newest > max_age


def _pid_alive(pid: int) -> bool:
    """pid 是否还活着；判不准一律当活着（少删不误删）。"""
    if pid <= 0:
        return True
    if pid == os.getpid():
        return True
    if os.name == "nt":
        #  Windows 的 os.kill(pid, 0) 会直接 TerminateProcess——绝不能拿来探活
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        #  PermissionError 等：进程存在但不归我们管
        return True
    return True


def _pid_alive_windows(pid: int) -> bool:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        process_query_limited_information = 0x1000
        still_active = 259
        error_invalid_parameter = 87
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            #  87 = pid 不存在；拒绝访问等其它错误说明进程在
            return kernel32.GetLastError() != error_invalid_parameter
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return True
