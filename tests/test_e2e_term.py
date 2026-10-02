"""shell 集成的真机 e2e：真的起 zsh / bash，eval `term init` 的脚本，敲几条命令，
再 `@x` 一次——模型是 scripted 桩，不打网络。

证明的是整条链路：脚本在真实 shell 里语法合法且能 eval、preexec/DEBUG 钩子
真的把命令写进了 pending 文件、`@x` 把它们拼进了 prompt 并落到了会话文件、
第二次 `@x` 接上了第一次的历史。交互式 shell 从管道读命令也会跑钩子
（zsh 的 preexec、bash 的 PROMPT_COMMAND/DEBUG 都认 -i），不需要 pty。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.test_e2e_scripted import E2ECase

#  每次 @x 是一个新进程、从脚本第一轮读起：两问各一份脚本，shell 里在两问之间换文件
_SCRIPT = """text: 收到，看到你的命令了
usage: {"prompt_tokens": 50, "completion_tokens": 8}
"""
_SCRIPT_2 = """text: 接着上次说
usage: {"prompt_tokens": 90, "completion_tokens": 6}
"""

_TIMEOUT = 180


class TermShellMixin:
    """各 shell 共用的驱动与断言；不继承 TestCase，免得基类自己也被收集去跑。"""

    shell: str = ""

    def setUp(self) -> None:
        super().setUp()  # type: ignore[misc]
        if not shutil.which(self.shell):
            self.skipTest(f"需要 {self.shell}")  # type: ignore[attr-defined]
        if os.name == "nt":
            #  Windows 走 PowerShell；runner 上的 Git Bash 不是目标环境，交互式驱动在那儿退 1
            self.skipTest("sh 系集成只在 POSIX 上跑")  # type: ignore[attr-defined]

    def launcher(self) -> str:
        return f"{shlex.quote(sys.executable)} -P -m xiaoyu"

    def shell_command(self) -> list[str]:
        raise NotImplementedError

    def drive(self, body: str) -> tuple[str, str, int]:
        env = self.scripted_env(_SCRIPT)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        env.pop("XIAOYU_TERM_SESSION", None)
        env.pop("XIAOYU_TERM_PENDING", None)
        #  本机 shell 的启动文件不该掺进来
        env["ZDOTDIR"] = str(Path(self.tmp) / "home")
        proc = subprocess.run(
            self.shell_command(),
            input=body,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=env,
            cwd=str(self.workspace),
            timeout=_TIMEOUT,
        )
        return proc.stdout, proc.stderr, proc.returncode

    def session_files(self) -> list[Path]:
        return sorted((Path(self.tmp) / "config" / "xiaoyu" / "sessions" / "term").glob("*.jsonl"))

    def user_messages(self, path: Path) -> list[str]:
        """会话文件里人发的 user 消息；续写时 harness 注入的 <world_state> 不算。"""
        texts = []
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            if record.get("role") != "user":
                continue
            content = record.get("content")
            text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
            if text.startswith("<world_state>"):
                continue
            texts.append(text)
        return texts

    def test_hooks_record_and_at_x_delivers(self) -> None:
        init = f'eval "$({self.launcher()} term init {self.shell} --launcher {shlex.quote(self.launcher())})"'
        second = Path(self.tmp) / "script2.txt"
        second.write_text(_SCRIPT_2, encoding="utf-8")
        body = "\n".join(
            [
                init,
                'echo "SESSION=$XIAOYU_TERM_SESSION"',
                'echo "PENDING=$XIAOYU_TERM_PENDING"',
                "ls",
                "cat nothing-here-404 2>/dev/null",
                "@x 刚才怎么了 </dev/null",
                "git status --short",
                f"cp {shlex.quote(str(second))} \"$XIAOYU_SCRIPTED_SCRIPTS\"",
                "@x 那现在呢 </dev/null",
                "exit",
                "",
            ]
        )
        stdout, stderr, code = self.drive(body)
        self.assertEqual(code, 0, stderr[-1500:])
        session = re.search(r"SESSION=(\S+)", stdout)
        pending = re.search(r"PENDING=(\S+)", stdout)
        self.assertTrue(session and pending, stdout)
        session_id = session.group(1)
        self.assertRegex(session_id, r"^term-[0-9a-f]{8}$")
        self.assertIn("收到，看到你的命令了", stdout)
        self.assertIn("接着上次说", stdout)
        #  第二问接上了第一问（用户 + 回答两条）
        self.assertIn("接上 2 条消息", stderr)

        files = self.session_files()
        self.assertEqual(len(files), 1, files)
        users = self.user_messages(files[0])
        self.assertEqual(len(users), 2, users)
        first, second = users
        self.assertTrue(first.startswith("[终端上下文]"), first)
        self.assertIn("<untrusted_content>", first)
        self.assertIn("$ ls\n", first)
        self.assertIn("$ cat nothing-here-404 2>/dev/null", first)
        self.assertNotIn("@x", first.split("</untrusted_content>")[0])
        self.assertTrue(first.endswith("\n\n刚才怎么了"), first)
        #  第二问只带两问之间的那一条，第一问交付过的不重复
        self.assertIn("$ git status --short", second)
        self.assertNotIn("$ ls\n", second)
        self.assertTrue(second.endswith("\n\n那现在呢"), second)
        #  `exit` 在第二问之后，留在 pending 里等下次
        pending_path = Path(pending.group(1))
        remaining = pending_path.read_text(encoding="utf-8") if pending_path.exists() else ""
        self.assertNotIn("git status", remaining)

        #  term info 走快路径，能读到会话的模型与用量
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        env.update({k: v for k, v in self.scripted_env(_SCRIPT).items() if k in ("XDG_CONFIG_HOME", "APPDATA", "HOME", "USERPROFILE")})
        env["XIAOYU_TERM_SESSION"] = session_id
        env["XIAOYU_TERM_PENDING"] = str(pending_path)
        proc = subprocess.run(
            [sys.executable, "-P", "-m", "xiaoyu", "term", "info"],
            capture_output=True, text=True, encoding="utf-8", env=env, timeout=_TIMEOUT,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout.strip(), rf"^{session_id} · \S+ · [1-9]\d* tok · \d+ 条待交付$")


class ZshTermE2E(TermShellMixin, E2ECase):
    shell = "zsh"

    def shell_command(self) -> list[str]:
        return ["zsh", "-f", "-i"]


class BashTermE2E(TermShellMixin, E2ECase):
    shell = "bash"

    def shell_command(self) -> list[str]:
        return ["bash", "--noprofile", "--norc", "-i"]


if __name__ == "__main__":
    unittest.main()
