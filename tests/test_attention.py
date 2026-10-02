"""终端礼仪：注意力铃（opt-in、只对终端）、窗口标题（去控制符、退出还原）、状态钩子
（后台跑、传状态参数、失败静默）。不碰真实终端：流用替身注入。"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import attention


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


class BellTest(unittest.TestCase):
    def test_off_by_default(self) -> None:
        stream = _Tty()
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(attention.BELL_ENV, None)
            attention.ring(stream)
        self.assertEqual(stream.getvalue(), "")

    def test_rings_when_opted_in_on_a_tty(self) -> None:
        stream = _Tty()
        with mock.patch.dict(os.environ, {attention.BELL_ENV: "1"}):
            attention.ring(stream)
        self.assertEqual(stream.getvalue(), "\x07")

    def test_never_writes_into_a_pipe(self) -> None:
        """管道里一个 0x07 只会污染输出：非终端一律不写。"""
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {attention.BELL_ENV: "1"}):
            attention.ring(stream)
        self.assertEqual(stream.getvalue(), "")


class TitleTest(unittest.TestCase):
    def test_title_strips_control_sequences_from_directory_name(self) -> None:
        text = attention.title_text(Path("/tmp/proj\x1b]0;evil\x07\nname"))
        self.assertEqual(text, "xiaoyu · proj name")
        self.assertNotIn("\x1b", text)

    def test_set_pushes_then_sets_and_clear_restores(self) -> None:
        stream = _Tty()
        with mock.patch.dict(os.environ, {attention.TITLE_ENV: "1"}):
            attention.set_title(Path("/work/zhinu"), stream)
            attention.clear_title(stream)
        out = stream.getvalue()
        #  先 push 旧标题，再 OSC 0 设新标题；退出先清空（不认标题栈的终端）再 pop
        self.assertTrue(out.startswith("\x1b[22;0t\x1b]0;xiaoyu · zhinu\x07"))
        self.assertTrue(out.endswith("\x1b]0;\x07\x1b[23;0t"))

    def test_disabled_by_env(self) -> None:
        stream = _Tty()
        with mock.patch.dict(os.environ, {attention.TITLE_ENV: "0"}):
            attention.set_title(Path("/work/zhinu"), stream)
            attention.clear_title(stream)
        self.assertEqual(stream.getvalue(), "")

    def test_not_a_tty_writes_nothing(self) -> None:
        stream = io.StringIO()
        with mock.patch.dict(os.environ, {attention.TITLE_ENV: "1"}):
            attention.set_title(Path("/work/zhinu"), stream)
        self.assertEqual(stream.getvalue(), "")


class StatusHookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "hook.txt"

    def _hook(self) -> str:
        #  用解释器本身当钩子程序：跨平台、不依赖 shell。写最后一个参数与环境变量
        script = (
            "import os,sys,pathlib;"
            "pathlib.Path(sys.argv[1]).write_text(sys.argv[-1]+'|'+os.environ.get('XIAOYU_STATUS',''))"
        )
        return f'"{sys.executable}" -c "{script}" "{self.out}"'

    def test_runs_in_background_with_state_argument(self) -> None:
        with mock.patch.dict(os.environ, {attention.HOOK_ENV: self._hook()}):
            started = time.monotonic()
            attention.status_hook(attention.WAITING_APPROVAL)
            #  不阻塞调用方：起线程就返回
            self.assertLess(time.monotonic() - started, 0.5)
            attention.wait_hooks()
        self.assertEqual(self.out.read_text(), "waiting_approval|waiting_approval")

    def test_missing_command_is_silent(self) -> None:
        with mock.patch.dict(os.environ, {attention.HOOK_ENV: "/definitely/not/here/xyz"}):
            attention.status_hook(attention.WAITING_INPUT)  # 不抛
            attention.wait_hooks()
        self.assertFalse(self.out.exists())

    def test_unset_means_no_subprocess(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(attention.HOOK_ENV, None)
            with mock.patch.object(attention.subprocess, "run") as run:
                attention.status_hook(attention.WAITING_INPUT)
                attention.wait_hooks()
            run.assert_not_called()

    def test_waiting_rings_and_hooks_together(self) -> None:
        stream = _Tty()
        with mock.patch.dict(
            os.environ, {attention.BELL_ENV: "1", attention.HOOK_ENV: self._hook()}
        ):
            attention.waiting(attention.WAITING_INPUT, stream)
            attention.wait_hooks()
        self.assertEqual(stream.getvalue(), "\x07")
        self.assertEqual(self.out.read_text(), "waiting_input|waiting_input")


if __name__ == "__main__":
    unittest.main()
