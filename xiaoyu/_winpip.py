"""Windows 上"给自己动手术"的落地执行器：等调用方退干净了再跑 pip。

为什么非得另起一个进程：Windows 锁住正在运行的镜像文件，pip 卸旧版时移不走
`Scripts\\xiaoyu.exe`，`xiaoyu update` / `xiaoyu uninstall` 必然断在
WinError 32。**改名挪开也不行**——那个 exe 还兼作 zip 载体（启动器 stub +
附加的 `__main__.py`），读它的句柄不带 `FILE_SHARE_DELETE`，`os.rename` 同样
被拒（2026-08-14 在 Windows 11 + Python 3.14 真机上实测确认，pip 自己的
`shutil.move` 就是先 rename 失败、再 unlink 失败）。等本体退出是唯一干净的
出路：那时启动器和所有已加载的原生扩展（.pyd）全部释放，整类锁一起消失。

由 cli 用 `python -P -m xiaoyu._winpip <pid> <ppid> <mode> <spec> <旧版本号>`
拉起。子进程继承同一个控制台，所以输出仍落在用户眼前，只是排在 shell 提示符
之后——这点 UX 折损换的是"能跑通"。

`-P` 不能省：不然 CWD 会被塞进 sys.path，在小羽源码目录里跑就会 import 到工作
树而不是装好的那份。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

#  等调用方退出的上限。正常是毫秒级，给到 60 秒纯粹是防它卡死时别把 pip 一起赔进去
_WAIT_SECONDS = 60.0

#  等"调用方的父进程"（启动器 stub）的上限要短得多：万一 getppid() 拿到的不是
#  stub 而是 cmd.exe，它得等用户关窗口才退，长上限就把升级白白挂在那儿了。
#  等不到也没关系——stub 残留的一瞬由下面的 pip 重试兜着。
_PARENT_WAIT_SECONDS = 5.0

#  启动器 stub 比 python.exe 晚一步退，pid 等完仍可能残留一瞬的句柄；
#  与其猜要等多久，不如让 pip 自己重试。
_ATTEMPTS = 4
_RETRY_SLEEP = 2.0


def _wait_for_exit(pid: int, seconds: float) -> None:
    """阻塞到 pid 退出（或超时）。非 Windows / 拿不到句柄都当作已退出。

    句柄探测的路子与 mcp_watchdog._parent_alive 相同：Windows 没有 POSIX 那种
    收养机制，pid 是否还在只能问内核要句柄。这里要的是阻塞等待，所以直接把超时
    交给 WaitForSingleObject，不自己轮询。
    """
    if os.name != "nt" or pid <= 0:
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
    kernel32.WaitForSingleObject.argtypes = (ctypes.c_void_p, ctypes.c_uint32)
    kernel32.CloseHandle.argtypes = (ctypes.c_void_p,)

    SYNCHRONIZE = 0x00100000
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:  # 打不开＝已经退了（或没权限，那也等不到，别耗着）
        return
    try:
        kernel32.WaitForSingleObject(handle, int(seconds * 1000))
    finally:
        kernel32.CloseHandle(handle)


def _pip(*args: str) -> int:
    """走 sys.executable -m pip——PATH 里的 pip 可能属于另一个解释器。"""
    return subprocess.run([sys.executable, "-m", "pip", *args]).returncode


def _installed_version() -> str:
    """装完之后的版本号。本进程载着的是旧代码，得开个新解释器去读。"""
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "from importlib.metadata import version; print(version('xiaoyu-agent'))",
        ],
        capture_output=True,
        text=True,
        #  输出只是版本号，但 text=True 按 locale 严格解码，Windows 上一个意外
        #  字节就能把流程炸在最后一步——显式 UTF-8 + replace
        encoding="utf-8",
        errors="replace",
    )
    return probe.stdout.strip() if probe.returncode == 0 else ""


def run(mode: str, spec: str, old_version: str) -> int:
    """跑 pip 并播报结果。调用方已经退出，此时不再有任何自锁。"""
    args = ("install", "--upgrade", spec) if mode == "update" else ("uninstall", "-y", spec)
    code = 1
    for attempt in range(_ATTEMPTS):
        code = _pip(*args)
        if code == 0:
            break
        if attempt < _ATTEMPTS - 1:
            #  多半是启动器 stub 还没退干净，隔一会儿再来
            print(f"[xiaoyu] pip 未成功，{_RETRY_SLEEP:.0f} 秒后重试……", flush=True)
            time.sleep(_RETRY_SLEEP)
    if code != 0:
        print("[xiaoyu] 失败，原因见上方 pip 输出。", file=sys.stderr, flush=True)
    elif mode == "update":
        new_version = _installed_version()
        if new_version and new_version != old_version:
            print(f"[xiaoyu] 已升级：{old_version} → {new_version}", flush=True)
        else:
            print(f"[xiaoyu] 已是最新版本（{new_version or old_version}）", flush=True)
    else:
        print("[xiaoyu] 小羽已卸载。后会有期。", flush=True)
    #  成败都得补这一句：调起我们的那个 xiaoyu 早退了，cmd 也早把提示符打出来
    #  并停在那儿等输入——我们只是往同一块屏幕缓冲上叠字，它不会重画。不说的话
    #  用户对着一行没有提示符的光标发愣（真实反馈）。
    #  刻意不去 WriteConsoleInput 注入一个回车：用户若在这期间敲了半条命令，
    #  那一下会把它直接执行掉，比多按一次 Enter 糟得多。
    print("\n[xiaoyu] 按一次 Enter 回到命令提示符。", flush=True)
    return code


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 5:  # 内部接口，参数对不上就是调用方写错了
        print(f"用法：{__spec__.name} <pid> <ppid> <mode> <spec> <version>", file=sys.stderr)
        return 2
    raw_pid, raw_ppid, mode, spec, old_version = argv
    if mode not in ("update", "uninstall"):
        print(f"未知模式：{mode}", file=sys.stderr)
        return 2
    #  python.exe 与它上面的启动器 stub 都得退掉，两个都等（上限不同，见常量注释）
    for raw, seconds in ((raw_pid, _WAIT_SECONDS), (raw_ppid, _PARENT_WAIT_SECONDS)):
        try:
            pid = int(raw)
        except ValueError:
            continue
        _wait_for_exit(pid, seconds)
    return run(mode, spec, old_version)


if __name__ == "__main__":
    sys.exit(main())
