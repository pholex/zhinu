"""sudo 密码通道（askpass.py）与 bash 工具的接线。

真 sudo 不进测试：helper 是 sudo 要执行的那个程序，测试直接起它、扮演 sudo。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from xiaoyu import askpass
from xiaoyu.config import Config
from xiaoyu.tools import Toolbox, _wait_bounded

POSIX = os.name == "posix"


def _drive(bridge: askpass.AskpassBridge, proc: subprocess.Popen, prompt) -> None:
    """扮演等待循环：helper 还活着就反复 service，直到它退出。"""
    deadline = time.monotonic() + 20
    while proc.poll() is None and time.monotonic() < deadline:
        if bridge.service(prompt) == 0:
            time.sleep(0.02)


@unittest.skipUnless(POSIX, "askpass 只在 posix 上有（Windows 没有 sudo）")
class TestBridge(unittest.TestCase):
    def setUp(self) -> None:
        bridge = askpass.AskpassBridge.create()
        self.assertIsNotNone(bridge, "本机建不起 unix socket 通道")
        self.bridge = bridge
        self.addCleanup(self.bridge.close)

    def test_helper_round_trip(self) -> None:
        """helper 把 sudo 的提示交过来、把前端给的密码原样打到 stdout（带换行给 sudo 读行）。"""
        seen: list[str] = []

        def prompt(text: str) -> str | None:
            seen.append(text)
            return "s3cret"

        proc = subprocess.Popen(
            [self.bridge.helper, "[sudo] password for me:"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        _drive(self.bridge, proc, prompt)
        out, err = proc.communicate(timeout=5)
        self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(out, b"s3cret\n")
        self.assertEqual(seen, ["[sudo] password for me:"])

    def test_cancel_makes_helper_fail_without_output(self) -> None:
        """前端返回 None（用户取消）：什么都不发，helper 非零退出，sudo 那头拿不到密码。"""
        proc = subprocess.Popen(
            [self.bridge.helper, "Password:"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        _drive(self.bridge, proc, lambda _text: None)
        out, _err = proc.communicate(timeout=5)
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(out, b"")

    def test_prompt_text_is_bounded_and_printable(self) -> None:
        """提示文案来自外部进程：控制字符剥掉、超长截断，上屏前就得是干净的。"""
        seen: list[str] = []
        proc = subprocess.Popen(
            [self.bridge.helper, "a\x1b[31mb\x07" + "x" * 10000],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        _drive(self.bridge, proc, lambda text: seen.append(text) or "p")
        proc.communicate(timeout=5)
        self.assertEqual(len(seen), 1)
        self.assertNotIn("\x1b", seen[0])
        self.assertNotIn("\x07", seen[0])
        self.assertTrue(seen[0].startswith("a[31mb"))
        self.assertLessEqual(len(seen[0]), askpass._MAX_PROMPT_BYTES)

    def test_service_without_request_returns_immediately(self) -> None:
        started = time.monotonic()
        self.assertEqual(self.bridge.service(lambda _t: "never"), 0.0)
        self.assertLess(time.monotonic() - started, 0.5)

    def test_env_sets_askpass_and_display_placeholder(self) -> None:
        env = self.bridge.env({"PATH": "/bin"})
        self.assertEqual(env["SUDO_ASKPASS"], self.bridge.helper)
        #  sudo 没有 tty 时只在 DISPLAY 非空才走 askpass；用户自己设了就不动
        self.assertTrue(env["DISPLAY"])
        self.assertEqual(self.bridge.env({"DISPLAY": ":1"})["DISPLAY"], ":1")

    def test_private_directory_and_cleanup(self) -> None:
        helper = Path(self.bridge.helper)
        self.assertEqual(helper.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(helper.stat().st_mode & 0o777, 0o700)
        self.bridge.close()
        self.assertFalse(helper.parent.exists())
        #  关了之后 service 是空操作，不炸
        self.assertEqual(self.bridge.service(lambda _t: "x"), 0.0)


@unittest.skipUnless(POSIX, "askpass 只在 posix 上有（Windows 没有 sudo）")
class TestBashWiring(unittest.TestCase):
    """bash 工具只在"有人能问 + 开关开着 + 命令里有提权入口"时装通道。"""

    #  `true || sudo x`：sudo 永远不会真跑（短路），但命令扫描认得出提权入口；
    #  后半段直接起 helper，扮演 sudo 拿密码
    PRIVILEGED = 'true || sudo x; "${SUDO_ASKPASS:-false}" "Password:"; echo "display=${DISPLAY:-unset}"'
    PLAIN = 'echo "askpass=${SUDO_ASKPASS:-unset} display=${DISPLAY:-unset}"'

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(
            base_url="http://unused",
            model="unused",
            workspace=Path(self.tmp.name).resolve(),
            enable_plugins=False,
            #  通道只在沙箱外有意义（沙箱里 sudo 起不来）；测试断言的是接线，不是沙箱
            sandbox=False,
        )
        self.box = Toolbox(self.config)
        self.prompts: list[tuple[str, str]] = []

    def _prompt(self, text: str, command: str) -> str | None:
        self.prompts.append((text, command))
        return "hunter2"

    def _env_without_display(self) -> dict[str, str]:
        env = dict(os.environ)
        env.pop("DISPLAY", None)
        return env

    def test_privileged_command_gets_channel_and_prompt_reaches_front(self) -> None:
        from unittest import mock

        self.box.secret_prompt = self._prompt
        with mock.patch.dict(os.environ, self._env_without_display(), clear=True):
            out = self.box.run("bash", {"command": self.PRIVILEGED})
        self.assertIn("exit_status: 0", out)
        self.assertIn("hunter2", out, "helper 该把前端给的密码打出来（这里扮演 sudo 读它）")
        self.assertIn("display=xiaoyu-askpass", out)
        self.assertEqual(len(self.prompts), 1)
        self.assertEqual(self.prompts[0][0], "Password:")
        self.assertEqual(self.prompts[0][1], self.PRIVILEGED)

    def test_plain_command_is_untouched(self) -> None:
        from unittest import mock

        self.box.secret_prompt = self._prompt
        with mock.patch.dict(os.environ, self._env_without_display(), clear=True):
            out = self.box.run("bash", {"command": self.PLAIN})
        self.assertIn("askpass=unset display=unset", out)
        self.assertEqual(self.prompts, [])

    def test_no_front_means_no_channel(self) -> None:
        """无人值守（-p / serve / ACP / 子 agent）：secret_prompt 为 None，sudo 照旧没有通道。"""
        from unittest import mock

        with mock.patch.dict(os.environ, self._env_without_display(), clear=True):
            out = self.box.run("bash", {"command": self.PRIVILEGED})
        #  "${SUDO_ASKPASS:-false}" 退化成 false：没有密码，DISPLAY 也没被动过
        self.assertNotIn("hunter2", out)
        self.assertIn("display=unset", out)
        self.assertIsNone(self.box._askpass_bridge)

    def test_switch_off(self) -> None:
        from unittest import mock

        self.config.enable_askpass = False
        self.box.secret_prompt = self._prompt
        with mock.patch.dict(os.environ, self._env_without_display(), clear=True):
            out = self.box.run("bash", {"command": self.PRIVILEGED})
        self.assertNotIn("hunter2", out)
        self.assertEqual(self.prompts, [])

    def test_background_task_has_no_channel(self) -> None:
        """后台任务没人在等：helper 连上来无人应答只会挂住，所以根本不装。"""
        from unittest import mock

        self.box.secret_prompt = self._prompt
        with mock.patch.dict(os.environ, self._env_without_display(), clear=True):
            out = self.box.run(
                "bash",
                {"command": 'true || sudo x; echo "askpass=${SUDO_ASKPASS:-unset}"', "run_in_background": True},
            )
            match = re.search(r"后台任务已启动：([\w-]+)", out)
            self.assertIsNotNone(match, out)
            result = self.box.run("task_output", {"task_ids": [match.group(1)], "timeout": 10})
        self.assertIn("askpass=unset", result)
        self.assertEqual(self.prompts, [])

    def test_config_env_switch(self) -> None:
        from unittest import mock

        workspace = Path(self.tmp.name)
        with mock.patch.dict(os.environ, {"XIAOYU_ENABLE_ASKPASS": "0"}):
            self.assertFalse(Config.from_env(workspace).enable_askpass)
        with mock.patch.dict(os.environ, {"XIAOYU_ENABLE_ASKPASS": "1"}):
            self.assertTrue(Config.from_env(workspace).enable_askpass)


class TestWaitBoundedService(unittest.TestCase):
    @staticmethod
    def _reap(proc: subprocess.Popen) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)

    def test_service_time_is_not_charged_to_the_command(self) -> None:
        """问密码的那段时间加回 deadline：命令本身跑得完就不该被判超时。"""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.6)"])
        self.addCleanup(self._reap, proc)
        calls = 0

        def service() -> float:
            nonlocal calls
            calls += 1
            #  第一片就"问了 0.5 秒"（这里不真睡：返回值才是协议）
            return 0.5 if calls == 1 else 0.0

        #  0.4 秒的预算本来不够 0.6 秒的命令；加回 0.5 秒后够了
        self.assertTrue(_wait_bounded(proc, [], time.monotonic() + 0.4, None, service))
        self.assertGreaterEqual(calls, 1)

    def test_without_service_timeout_still_bites(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
        self.addCleanup(self._reap, proc)
        self.assertFalse(_wait_bounded(proc, [], time.monotonic() + 0.3, None, lambda: 0.0))


if __name__ == "__main__":
    unittest.main()
