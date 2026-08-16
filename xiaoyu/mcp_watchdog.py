"""MCP stdio server 的父进程死亡看门狗。

用法（由 mcp.py 自动包装，不手动调用）：
    python -m xiaoyu.mcp_watchdog --ppid <小羽的pid> [--poll 秒] -- <真实命令...>

为什么需要它：MCP 子进程用 start_new_session 隔离进程组（防 REPL 的 Ctrl-C
连带杀掉 server），副作用是小羽被 `kill -9` 时这些子进程收不到任何信号，
npx/node 变成永久孤儿。macOS 没有 Linux 的 PR_SET_PDEATHSIG，只能用户态轮询：
看门狗每 poll 秒探测 --ppid 是否存活（POSIX 比对 os.getppid()——孤儿会被
init/launchd 收养；Windows 无收养机制，用 OpenProcess 句柄探测），
父进程一死就 TERM → 宽限 → KILL 整个子进程组（Windows 上硬杀直接子进程）。

三条纪律：
- **stdio 全程透传**：MCP 协议直接跑在继承的 stdin/stdout 上，看门狗自己
  绝不写 stdout（会污染协议流），诊断只进 stderr（已被 mcp.py 接到日志文件）。
- **转发自己收到的 SIGTERM/SIGINT**：真实 server 在独立进程组里，不转发的话
  "上游 terminate 看门狗"反而杀不掉 server——看门狗包装就反过来制造了
  它要修的 bug。
- 子进程正常退出时看门狗跟着退出并透传退出码。
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

_DEFAULT_POLL = 2.0
_TERM_GRACE = 3.0


def _signal_child(child: subprocess.Popen, sig: int) -> None:
    """给子进程（POSIX 上是整个子进程组）发信号，失败静默。"""
    try:
        if os.name == "nt":
            #  Windows 没有 SIGKILL 也没有进程组信号（引用 signal.SIGKILL 会直接
            #  AttributeError）：terminate/kill 同为 TerminateProcess 硬杀，
            #  孙进程（npx.cmd 再拉起的 node）收不到——那需要 Job Object，暂不做
            child.kill()
        else:
            os.killpg(child.pid, sig)
    except (OSError, ProcessLookupError):
        pass


def _parent_alive(ppid: int) -> bool:
    """父进程是否还活着。

    POSIX：父死后本进程被 init/launchd 收养，getppid 不再等于记录值。
    Windows 没有收养机制（getppid 恒不变），改用句柄探测：OpenProcess 打不开
    或已发终止信号即视为死亡。WAIT_TIMEOUT(0x102) = 仍在运行。
    """
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, ppid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel32.CloseHandle(handle)
    return os.getppid() == ppid


def _shutdown_child(child: subprocess.Popen) -> None:
    """体面关停：TERM → 宽限 → KILL。"""
    _signal_child(child, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE
    while time.monotonic() < deadline:
        if child.poll() is not None:
            return
        time.sleep(0.05)
    _signal_child(child, getattr(signal, "SIGKILL", signal.SIGTERM))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="xiaoyu.mcp_watchdog", add_help=False)
    parser.add_argument("--ppid", type=int, required=True)
    parser.add_argument("--poll", type=float, default=_DEFAULT_POLL)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("[mcp_watchdog] 缺少要执行的命令", file=sys.stderr)
        return 2

    #  子进程放进独立会话/进程组：孤儿清理时能 killpg 一锅端（包括它再 fork 的）。
    #  stdin/stdout/stderr 全部继承——协议流不经手，看门狗死了也不断流。
    popen_kwargs: dict = {}
    if os.name != "nt":
        popen_kwargs["start_new_session"] = True
    try:
        child = subprocess.Popen(command, **popen_kwargs)
    except OSError as exc:
        print(f"[mcp_watchdog] 无法启动 {command[0]}：{exc}", file=sys.stderr)
        return 127

    #  转发上游信号：不转发的话 terminate(看门狗) 杀不到独立组里的真 server
    def forward(signum: int, frame: object) -> None:  # noqa: ARG001
        threading.Thread(target=_shutdown_child, args=(child,), daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, forward)
        except (OSError, ValueError):
            pass

    stop = threading.Event()

    def watch_parent() -> None:
        while not stop.wait(max(0.1, args.poll)):
            if not _parent_alive(args.ppid):
                print("[mcp_watchdog] 父进程已退出，回收 server", file=sys.stderr)
                _shutdown_child(child)
                return

    threading.Thread(target=watch_parent, daemon=True).start()
    code = child.wait()
    stop.set()
    return code


if __name__ == "__main__":
    sys.exit(main())
