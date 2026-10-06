"""shell 集成的真机 e2e：真的起 zsh / bash，eval `term init` 的脚本，敲几条命令，
再 `@x` 一次——模型是 scripted 桩，不打网络。

证明的是整条链路：脚本在真实 shell 里语法合法且能 eval、preexec/DEBUG 钩子
真的把命令写进了 pending 文件、提示符钩子补上了退出码、`@x` 把它们拼进了 prompt
并落到了会话文件、第二次 `@x` 接上了第一次的历史。交互式 shell 从管道读命令也会跑钩子
（zsh 的 preexec、bash 的 PROMPT_COMMAND/DEBUG 都认 -i），不需要 pty。

`@c` 另有两条：bash 照样从管道驱动（命令进了历史、没被执行）；zsh 要给它一个
真的 pty——`print -z` 把命令压进行编辑器的缓冲区，只有行编辑器在跑才看得到它
出现在提示符上，也只有这样才能证明「回车才执行」。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

from tests.test_e2e_scripted import E2ECase
from xiaoyu import term

#  每次 @x 是一个新进程、从脚本第一轮读起：两问各一份脚本，shell 里在两问之间换文件
_SCRIPT = """text: 收到，看到你的命令了
usage: {"prompt_tokens": 50, "completion_tokens": 8}
"""
_SCRIPT_2 = """text: 接着上次说
usage: {"prompt_tokens": 90, "completion_tokens": 6}
"""

#  @c 的回答：命令里带一个算术展开——屏幕上出现 42 只可能是 shell 真的执行了它，
#  出现原文则只是被放到了提示符 / 历史里
_COMMAND_SCRIPT = """text: {"command": "echo SUGGESTED-$((40+2))", "note": "口算一下"}
"""
_SUGGESTED = "echo SUGGESTED-$((40+2))"

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
                #  半角问号：zsh 默认对不上通配就整行报错，靠 noglob 别名原样传进去
                "@x 刚才怎么了? </dev/null",
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
        #  退出码是提示符钩子在命令跑完后补记的：成功的 0、失败的 1 都要对上号
        self.assertIn("$ ls  → 退出码 0\n", first)
        self.assertIn("$ cat nothing-here-404 2>/dev/null  → 退出码 1\n", first)
        self.assertNotIn("@x", first.split("</untrusted_content>")[0])
        self.assertTrue(first.endswith("\n\n刚才怎么了?"), first)
        #  第二问只带两问之间的那一条，第一问交付过的不重复
        self.assertIn("$ git status --short", second)
        self.assertNotIn("$ ls", second)
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


class BashCommandE2E(E2ECase):
    """bash 里的 `@c`：命令打出来、推进历史（顶掉 `@c …` 那一行），不执行。"""

    def setUp(self) -> None:
        super().setUp()
        if os.name == "nt" or not shutil.which("bash"):
            self.skipTest("需要 POSIX 上的 bash")

    def test_at_c_pushes_the_command_into_history_without_running_it(self) -> None:
        launcher = f"{shlex.quote(sys.executable)} -P -m xiaoyu"
        env = self.scripted_env(_COMMAND_SCRIPT)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        env.pop("XIAOYU_TERM_SESSION", None)
        env.pop("XIAOYU_TERM_PENDING", None)
        env["HISTFILE"] = str(Path(self.tmp) / "bash_history")
        body = "\n".join(
            [
                f'eval "$({launcher} term init bash --launcher {shlex.quote(launcher)})"',
                'echo "PENDING=$XIAOYU_TERM_PENDING"',
                "ls",
                #  </dev/null：不然它会把管道里剩下的脚本当「管道材料」读走
                "@c 算一下 -x 开头的 </dev/null",
                'echo "STATUS=$?"',
                "history 3",
                "exit",
                "",
            ]
        )
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-i"],
            input=body, capture_output=True, text=True, encoding="utf-8",
            env=env, cwd=str(self.workspace), timeout=_TIMEOUT,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        self.assertIn("STATUS=0", proc.stdout)
        #  命令原文打出来了，但没被执行
        self.assertIn(_SUGGESTED, proc.stdout)
        self.assertNotIn("SUGGESTED-42", proc.stdout)
        self.assertIn("口算一下", proc.stderr)
        self.assertIn("按 ↑ 取用", proc.stderr)
        self.assertRegex(proc.stderr, r"已记下本机环境：.*bash \d")
        #  历史里是那条命令，`@c …` 自己被它顶掉了
        self.assertRegex(proc.stdout, rf"(?m)^\s*\d+\s+{re.escape(_SUGGESTED)}$")
        self.assertNotRegex(proc.stdout, r"(?m)^\s*\d+\s+@c ")
        pending = re.search(r"PENDING=(\S+)", proc.stdout)
        self.assertTrue(pending, proc.stdout)
        recorded = Path(pending.group(1)).read_text(encoding="utf-8")
        self.assertIn("\tls\n", recorded)
        self.assertNotIn("@c", recorded)

    def test_declined_request_is_handed_to_at_x(self) -> None:
        """不是一条命令能办的事：同一句需求直接转给 @x，人不必再敲一遍。
        两次请求读的是同一份脚本——@x 收到的回答就是那段 JSON 原文，看得见即证明它跑了。"""
        launcher = f"{shlex.quote(sys.executable)} -P -m xiaoyu"
        env = self.scripted_env('text: {"command": "", "note": "这得用 @x"}\n')
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        env.pop("XIAOYU_TERM_SESSION", None)
        env.pop("XIAOYU_TERM_PENDING", None)
        env["HISTFILE"] = str(Path(self.tmp) / "bash_history")
        body = "\n".join(
            [
                f'eval "$({launcher} term init bash --launcher {shlex.quote(launcher)})"',
                "@c 本机的 ip 是哪块网卡，讲讲 </dev/null",
                'echo "STATUS=$?"',
                "exit",
                "",
            ]
        )
        proc = subprocess.run(
            ["bash", "--noprofile", "--norc", "-i"],
            input=body, capture_output=True, text=True, encoding="utf-8",
            env=env, cwd=str(self.workspace), timeout=_TIMEOUT,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        self.assertIn("STATUS=0", proc.stdout)
        self.assertIn("转给 @x", proc.stderr)
        self.assertRegex(proc.stderr, r"会话 term-[0-9a-f]+")
        self.assertIn("这得用 @x", proc.stdout)
        sessions = sorted((Path(self.tmp) / "config" / "xiaoyu" / "sessions" / "term").glob("*.jsonl"))
        self.assertEqual(len(sessions), 1, sessions)
        self.assertIn("本机的 ip 是哪块网卡，讲讲", sessions[0].read_text(encoding="utf-8"))


class _ZshOnPty:
    """一个跑在 pty 上的交互式 zsh：敲一行、等屏幕上出现某段输出。"""

    def __init__(self, test: unittest.TestCase, env: dict[str, str], cwd: str) -> None:
        import fcntl
        import pty
        import struct
        import termios

        self.test = test
        self.seen = ""
        self.master, slave = pty.openpty()
        #  够宽：提示符 + 命令不折行，屏幕上的命令才是连续的一段
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 200, 0, 0))
        #  新会话：zsh 不该去碰跑测试的那个终端的前台进程组
        self.proc = subprocess.Popen(
            ["zsh", "-f", "-i"], stdin=slave, stdout=slave, stderr=slave,
            env={**env, "TERM": "xterm", "PS1": "READY> "}, cwd=cwd, start_new_session=True,
        )
        os.close(slave)

    def _read(self, wait: float) -> bool:
        """读一块屏幕输出进 seen；pty 关了（shell 退出）回 False。"""
        import select

        if not select.select([self.master], [], [], wait)[0]:
            return True
        try:
            chunk = os.read(self.master, 65536)
        except OSError:
            return False
        self.seen += chunk.decode("utf-8", "replace")
        return bool(chunk)

    def expect(self, pattern: str, timeout: float = 60.0) -> re.Match[str]:
        """读到屏幕输出里出现 pattern（只看上一次匹配之后的部分）为止。"""
        deadline = time.monotonic() + timeout
        while True:
            match = re.search(pattern, self.seen)
            if match:
                self.seen = self.seen[match.end():]
                return match
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.test.fail(f"等不到 {pattern!r}；屏幕尾部：{self.seen[-600:]!r}")
            if not self._read(min(remaining, 1.0)):
                self.test.fail(f"shell 提前退出，没等到 {pattern!r}；屏幕尾部：{self.seen[-600:]!r}")

    def send(self, line: str) -> None:
        os.write(self.master, line.encode() + b"\r")

    def exit(self) -> int:
        #  点名退出码：裸 exit 带的是上一条命令的状态
        self.send("exit 0")
        #  退出前写的东西得有人读走：macOS 上 pty 从端关闭时要等输出排空，
        #  这边不读，zsh 就卡在退出的半路上
        deadline = time.monotonic() + 30
        while self.proc.poll() is None and time.monotonic() < deadline:
            if not self._read(0.2):
                break
        return self.proc.wait(timeout=10)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        os.close(self.master)


@unittest.skipUnless(os.name == "posix" and shutil.which("zsh"), "需要 zsh 与 pty")
class ZshCommandE2E(E2ECase):
    """zsh 里的 `@c`：命令出现在下一个提示符上，回车才执行，执行后照常进 pending。"""

    ASK = "算一下 (括号) 和 ? 都原样"

    def start(self, *init_flags: str, before: tuple[str, ...] = ()) -> tuple[_ZshOnPty, Path]:
        launcher = f"{shlex.quote(sys.executable)} -P -m xiaoyu"
        env = self.scripted_env(_COMMAND_SCRIPT)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        env.pop("XIAOYU_TERM_SESSION", None)
        env.pop("XIAOYU_TERM_PENDING", None)
        env["ZDOTDIR"] = str(Path(self.tmp) / "home")
        shell = _ZshOnPty(self, env, str(self.workspace))
        self.addCleanup(shell.close)
        shell.expect(r"READY> ")
        for line in before:
            shell.send(line)
        flags = " ".join(init_flags)
        shell.send(f'eval "$({launcher} term init zsh {flags} --launcher {shlex.quote(launcher)})"')
        shell.send('echo "PENDING=$XIAOYU_TERM_PENDING"')
        return shell, Path(shell.expect(r"PENDING=(/\S+\.pending)").group(1))

    def suggestion_lands_and_enter_runs_it(self, shell: _ZshOnPty, pending: Path) -> None:
        #  说明走 stderr 直接上屏；随后的新提示符上就是那条命令，还没执行
        shell.expect(r"口算一下")
        shell.expect(r"READY> .*" + re.escape(_SUGGESTED))
        self.assertNotIn("SUGGESTED-42", shell.seen)
        shell.send("")
        shell.expect(r"SUGGESTED-42")
        shell.expect(r"READY> ")
        self.assertEqual(shell.exit(), 0)
        #  回车执行的那条命令被钩子照常记下，退出码也补上了：下一次 @x 带得上它；
        #  `@c …` 自己不进 pending
        recorded = [(e.command, e.status) for e in term.drain_pending(pending)]
        self.assertIn((_SUGGESTED, 0), recorded)
        self.assertFalse([command for command, _ in recorded if command.startswith("@c")], recorded)
        #  这一次的需求与命令记进了上文（追问用），需求是 shell 交过来的原文
        recall = list(pending.parent.glob("*.recall"))
        self.assertEqual(len(recall), 1, recall)
        record = json.loads(recall[0].read_text(encoding="utf-8").splitlines()[-1])
        self.assertEqual(record["command"], _SUGGESTED)
        self.assertEqual(record["ask"], self.ASK)

    def test_at_c_puts_the_command_on_the_prompt_and_enter_runs_it(self) -> None:
        shell, pending = self.start()
        shell.send(f"@c {self.ASK}")
        self.suggestion_lands_and_enter_runs_it(shell, pending)

    def test_natural_line_is_handed_to_at_c_without_the_prefix(self) -> None:
        """--natural：不敲 `@c`。真命令照常执行；一句自然语言在回车时被改写成
        `@c -- '原文'`（屏幕上看得见），之后与手敲 `@c` 完全一样。"""
        shell, pending = self.start("--natural")
        #  第一个词是命令：哪怕参数是中文也原样执行
        shell.send("echo 真命令-$((1+1))-你好")
        shell.expect(r"真命令-2-你好")
        shell.expect(r"READY> ")
        self.assertFalse(list(pending.parent.glob("*.recall")), "真命令不该被转给 @c")
        shell.send(self.ASK)
        shell.expect(r"@c -- '" + re.escape(self.ASK) + "'")
        self.suggestion_lands_and_enter_runs_it(shell, pending)

    def test_natural_keeps_other_accept_line_wrappers_in_the_chain(self) -> None:
        """别的插件也包回车键（自动建议、语法高亮都这么做）：先于我们包的、后于
        我们包的，都得照常被调到，我们的改写也照常生效。"""
        shell, _ = self.start(
            "--natural",
            before=("earlier() { (( EARLIER++ )); zle .accept-line }; zle -N accept-line earlier",),
        )
        shell.send("zle -A accept-line later_next; later() { (( LATER++ )); zle later_next }; zle -N accept-line later")
        shell.send(self.ASK)
        shell.expect(r"@c -- '" + re.escape(self.ASK) + "'")
        shell.expect(r"READY> .*" + re.escape(_SUGGESTED))
        shell.send("")
        shell.expect(r"SUGGESTED-42")
        shell.send('echo "CHAIN=${EARLIER:-0}/${LATER:-0}"')
        chain = shell.expect(r"CHAIN=(\d+)/(\d+)")
        #  每次回车两层都走到：init 之后至少回车了四次
        self.assertGreaterEqual(int(chain.group(1)), 4, chain.group(0))
        self.assertGreaterEqual(int(chain.group(2)), 3, chain.group(0))
        self.assertEqual(shell.exit(), 0)

    def test_without_the_switch_a_natural_line_is_left_to_the_shell(self) -> None:
        shell, pending = self.start()
        shell.send("算一下这个")
        shell.expect(r"command not found: 算一下这个")
        shell.expect(r"READY> ")
        self.assertEqual(shell.exit(), 0)
        self.assertFalse(list(pending.parent.glob("*.recall")))


@unittest.skipUnless(os.name == "posix", "需要 pty")
class RawQuestionE2E(E2ECase):
    """不带问题的 `term run`：stdin 是终端时读一行原样文本当问题。真起子进程、
    真给它一个 pty——问题不经 shell，问号、括号、引号、管道符都得一字不差到模型手里。"""

    def test_bare_run_reads_the_question_from_the_tty(self) -> None:
        import pty

        raw = "为什么失败了? (又一次) | 'x' $HOME *.log"
        env = self.scripted_env(_SCRIPT)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
        env["XIAOYU_TERM_SESSION"] = "term-raw1"
        env.pop("XIAOYU_TERM_PENDING", None)
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-P", "-m", "xiaoyu", "term", "run"],
                stdin=slave,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
                cwd=str(self.workspace),
            )
            #  行规程替子进程攒着这一行：它什么时候调 input() 都读得到
            os.write(master, f"{raw}\n".encode())
            try:
                stdout, stderr = proc.communicate(timeout=_TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                raise
        finally:
            os.close(slave)
            os.close(master)
        self.assertEqual(proc.returncode, 0, stderr[-1500:])
        self.assertIn("问 › ", stderr)
        self.assertIn("收到，看到你的命令了", stdout)
        files = sorted((Path(self.tmp) / "config" / "xiaoyu" / "sessions" / "term").glob("*.jsonl"))
        self.assertEqual(len(files), 1, files)
        users = [
            record["content"]
            for record in map(json.loads, files[0].read_text(encoding="utf-8").splitlines())
            if record.get("role") == "user" and isinstance(record.get("content"), str)
        ]
        self.assertEqual(users, [raw])


if __name__ == "__main__":
    unittest.main()
