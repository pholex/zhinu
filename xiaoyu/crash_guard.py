"""崩溃面包屑：进程非正常退出时留下可事后诊断的痕迹。

为什么要它：交互 CLI 崩了 traceback 打在终端上、用户看得见；但 `xiaoyu serve`
守护进程、后台任务线程、以及嵌入宿主里，未捕获异常和**原生崩溃**
（llama_cpp 嵌入 / playwright 这类 C 扩展可能 segfault，根本没有 Python
traceback）常常无声无息——事后拿到"它昨晚死了"却没有任何线索。

三层，各覆盖一类退出，互不重叠：
1. `sys.excepthook`：未捕获的 Python 异常，写盘**并**照常交给原 hook（终端仍
   打 traceback，不改变现有可见行为）。
2. `faulthandler`：C 层崩溃（segfault/abort）→ 转储所有线程栈到 stderr，
   服务管理器的 journal 能收到。这是 Python 异常抓不到的那类。
3. `atexit`：只在**带着活跃异常**退出时补一条——覆盖某些 `excepthook` 没走到
   的退出路径。无异常的正常退出不写。

只在 CLI 入口（`cli.main`）显式安装，**绝不在 import 时装**：嵌入宿主有自己的
excepthook/日志，库被 import 就篡改全局 hook 是越界。写盘一律 best-effort，
崩溃日志本身绝不能让退出路径再崩一次。
"""

from __future__ import annotations

import contextlib
import faulthandler
import sys
import traceback
from datetime import datetime
from pathlib import Path
from types import TracebackType

#  单文件上限：超了截断保留尾部（最近的崩溃最相关）。崩溃日志无界增长没意义。
_MAX_BYTES = 256 * 1024

_installed = False
_log_path: Path | None = None
_prev_excepthook = None


def _resolve_path() -> Path:
    from .config import user_config_dir

    return user_config_dir() / "crash.log"


def _write(header: str, exc: BaseException | None) -> None:
    """把一条崩溃记录追加到 crash.log。绝不抛——写日志失败不能掀翻退出路径。"""
    if _log_path is None:
        return
    with contextlib.suppress(Exception):
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [f"\n===== {stamp} · {header} · pid={_pid()} ====="]
        if exc is not None:
            parts.append("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
        body = "\n".join(parts) + "\n"
        _log_path.parent.mkdir(parents=True, exist_ok=True)
        #  超限时保留尾部：读旧内容拼新内容再截断。文件不大，一次读写可接受。
        old = ""
        with contextlib.suppress(OSError):
            old = _log_path.read_text(encoding="utf-8", errors="replace")
        merged = old + body
        if len(merged) > _MAX_BYTES:
            merged = "[……早期崩溃记录已截断……]\n" + merged[-_MAX_BYTES:]
        _log_path.write_text(merged, encoding="utf-8")


def _pid() -> int:
    import os

    return os.getpid()


def _excepthook(
    exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
) -> None:
    #  KeyboardInterrupt 是正常中断，不当崩溃记
    if not issubclass(exc_type, KeyboardInterrupt):
        _write("未捕获异常", exc)
    #  链回原 hook：终端仍照常打 traceback，现有可见行为不变
    if _prev_excepthook is not None:
        _prev_excepthook(exc_type, exc, tb)
    else:
        sys.__excepthook__(exc_type, exc, tb)


def _atexit() -> None:
    #  只在带着活跃异常退出时补记（覆盖 excepthook 没走到的退出路径）
    exc = sys.exc_info()[1]
    if exc is not None and not isinstance(exc, (KeyboardInterrupt, SystemExit)):
        _write("退出时仍有活跃异常", exc)


def install(log_path: Path | None = None) -> None:
    """安装崩溃面包屑（幂等）。只该在 CLI 入口调，别在 import 时调。"""
    global _installed, _log_path, _prev_excepthook
    if _installed:
        return
    _installed = True
    _log_path = log_path or _resolve_path()
    #  faulthandler 抓 C 层崩溃 → stderr。装不上（罕见的无 stderr 环境）不致命。
    with contextlib.suppress(Exception):
        faulthandler.enable()
    _prev_excepthook = sys.excepthook
    sys.excepthook = _excepthook
    import atexit

    atexit.register(_atexit)
