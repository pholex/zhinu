"""sudo 的密码提示通道（askpass）。

bash 工具起的命令没有控制终端（stdin 接 /dev/null、独立会话），sudo 要密码时
读不到 tty 就直接失败。sudo 自带的出路是 askpass：`SUDO_ASKPASS` 指向一个
helper 程序，没有 tty 时 sudo 就执行它、把它的 stdout 当密码（这条退路 sudo
只在 `DISPLAY` 非空时走——它把"没有 tty 但有 DISPLAY"当作图形环境）。

这里的 helper 不自己问人：它连回本进程的 unix socket，把 sudo 给的提示文案
交过来；本进程在前端（TUI 的遮罩输入框 / 明文 REPL 的 getpass）向用户要密码，
再原样回给 helper。密码只在内存里走一趟交给 sudo：不落盘、不进会话记录、
不出现在工具输出里，模型从头到尾看不见。

为什么不做"密码转发"（小羽持有密码再喂给 `sudo -S`）：那要求小羽拿着密码——
进配置、进环境或进会话文件，任何一处外流都等于整机 root。askpass 让持有方
始终是用户自己和 sudo。

边界：
- 沙箱里 setuid 程序起不来（Seatbelt 拒绝 exec，bubblewrap 的用户命名空间里
  sudo 不是 root），所以这条通道实际只在升权（danger-full-access）或关掉沙箱
  （`XIAOYU_SANDBOX=0`）时起作用；升权本身仍要用户确认。
- 只给**前台** bash 调用装：后台任务没人在等，helper 连上来无人应答只会挂住。
- 只给命令里认得出提权入口（`command_check.privileged_command`）的调用装：
  `DISPLAY` 这个副作用不该落到每条普通命令上（Linux 上不少程序看到 DISPLAY
  就去找图形环境）。
- 无人值守（`-p`、serve、ACP）没有前端可问，不装；sudo 照旧报"需要终端"。
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shlex
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable

#  前端的密码提问函数：(sudo 的提示文案, 正在跑的命令) -> 密码；None = 用户取消。
SecretPrompt = Callable[[str, str], "str | None"]

#  helper 交过来的提示文案上限：它来自 sudo（或任何能连上 socket 的同用户进程），
#  只用来给人看一眼，没必要收一整篇
_MAX_PROMPT_BYTES = 4096
#  helper 发提示、本进程读完的时限：正常是毫秒级，读不到就当它死了，别挂住等待循环
_HANDSHAKE_SECONDS = 5.0

#  helper 本体：不 import xiaoyu（起得快，也不依赖安装布局），只靠标准库。
#  socket 路径从自己所在目录推：sudo 执行 helper 时环境不一定原样传过来。
_HELPER_SOURCE = '''\
import os
import socket
import sys

path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sock")
sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
sock.connect(path)
prompt = " ".join(sys.argv[1:])
sock.sendall(prompt.encode("utf-8", "replace")[:%(max_prompt)d])
sock.shutdown(socket.SHUT_WR)
chunks = []
while True:
    data = sock.recv(4096)
    if not data:
        break
    chunks.append(data)
answer = b"".join(chunks)
if not answer:
    sys.exit(1)
sys.stdout.buffer.write(answer)
sys.stdout.flush()
''' % {"max_prompt": _MAX_PROMPT_BYTES}


def supported() -> bool:
    """本平台能不能装这条通道：要 unix socket、要有解释器给 helper 用。"""
    return os.name == "posix" and bool(sys.executable) and hasattr(socket, "AF_UNIX")


class AskpassBridge:
    """一条 askpass 通道：私有目录（0700）里放 helper 与 unix socket，本进程持听端。

    `service()` 是非阻塞的：等待循环每片问一次，有 helper 连上来才问人。问人
    期间 agent 线程就停在这里——sudo 那头同样在等，不冲突。
    """

    def __init__(self, directory: Path, listener: socket.socket) -> None:
        self._dir: Path | None = directory
        self._listener: socket.socket | None = listener
        self.helper = str(directory / "ask")

    @classmethod
    def create(cls) -> AskpassBridge | None:
        """建通道；本平台不支持或建不起来（临时目录不可写、socket 路径过长）返回 None。"""
        if not supported():
            return None
        directory: Path | None = None
        listener: socket.socket | None = None
        try:
            directory = Path(tempfile.mkdtemp(prefix="xiaoyu-askpass-"))
            (directory / "helper.py").write_text(_HELPER_SOURCE, encoding="utf-8")
            #  sudo 直接 exec 这个文件：要可执行、要 shebang。解释器路径带空格或
            #  引号时 shebang 不可靠，所以套一层 /bin/sh 再 exec，路径按 shell 规则引用。
            #  -I：隔离模式，不从 cwd / 环境里找模块——helper 跑在模型选的目录下。
            script = "#!/bin/sh\nexec {python} -I {helper} \"$@\"\n".format(
                python=shlex.quote(sys.executable),
                helper=shlex.quote(str(directory / "helper.py")),
            )
            helper = directory / "ask"
            helper.write_text(script, encoding="utf-8")
            helper.chmod(0o700)
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            listener.bind(str(directory / "sock"))
            listener.listen(4)
            listener.setblocking(False)
        except OSError:
            if listener is not None:
                with contextlib.suppress(OSError):
                    listener.close()
            if directory is not None:
                shutil.rmtree(directory, ignore_errors=True)
            return None
        bridge = cls(directory, listener)
        atexit.register(bridge.close)
        return bridge

    def env(self, base: dict[str, str]) -> dict[str, str]:
        """给子进程环境装上通道：`SUDO_ASKPASS` 指向 helper；`DISPLAY` 用户没设就补一个
        占位值——sudo 没有 tty 时只在 DISPLAY 非空的情况下才去调 askpass。"""
        env = dict(base)
        env["SUDO_ASKPASS"] = self.helper
        env.setdefault("DISPLAY", "xiaoyu-askpass")
        return env

    def service(self, prompt: Callable[[str], str | None]) -> float:
        """收一次 helper 的请求并答复，没有请求立即返回。

        返回花在问人上的秒数：这段时间是用户在敲密码，不该算进命令的超时。
        用户取消（prompt 返回 None）就什么都不发、直接关连接：helper 退出码非零，
        sudo 报"没有提供密码"，一次性失败、不重试。
        """
        if self._listener is None:
            return 0.0
        try:
            conn, _ = self._listener.accept()
        except (BlockingIOError, InterruptedError):
            return 0.0
        except OSError:
            return 0.0
        started = time.monotonic()
        with conn:
            text = self._read_prompt(conn)
            answer = prompt(text)
            if answer is not None:
                with contextlib.suppress(OSError):
                    conn.sendall(answer.encode("utf-8", "replace") + b"\n")
        return time.monotonic() - started

    @staticmethod
    def _read_prompt(conn: socket.socket) -> str:
        """读到 helper 关写端为止，有界；只留可打印字符——这段文案要上屏给人看。"""
        conn.settimeout(_HANDSHAKE_SECONDS)
        chunks: list[bytes] = []
        size = 0
        with contextlib.suppress(OSError):
            while size < _MAX_PROMPT_BYTES:
                data = conn.recv(_MAX_PROMPT_BYTES - size)
                if not data:
                    break
                chunks.append(data)
                size += len(data)
        text = b"".join(chunks).decode("utf-8", errors="replace")
        return "".join(ch for ch in text if ch.isprintable()).strip()

    def close(self) -> None:
        if self._listener is not None:
            with contextlib.suppress(OSError):
                self._listener.close()
            self._listener = None
        if self._dir is not None:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
