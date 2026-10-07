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


class NotificationChannelTest(unittest.TestCase):
    """XIAOYU_BELL 的各取值：生成的字节序列、auto 的终端判定、tmux 透传。"""

    def tearDown(self) -> None:
        attention.clear_title(io.StringIO())

    def _env(self, **extra: str) -> dict[str, str]:
        base = {attention.TITLE_ENV: "0"}
        base.update(extra)
        return base

    def _ring(self, value: str, state: str = attention.WAITING_INPUT, **extra: str) -> str:
        stream = _Tty()
        with mock.patch.dict(os.environ, self._env(**{attention.BELL_ENV: value, **extra}), clear=False):
            os.environ.pop("TMUX", None)
            for name in ("TERM_PROGRAM", "KITTY_WINDOW_ID", "TERM"):
                os.environ.pop(name, None)
            os.environ.update(extra)
            attention.set_title(Path("/work/zhinu"), stream)
            attention.ring(stream, state)
        return stream.getvalue()

    def test_bel_aliases(self) -> None:
        self.assertEqual(self._ring("1"), "\x07")
        self.assertEqual(self._ring("bel"), "\x07")
        self.assertEqual(self._ring("true"), "\x07")

    def test_off_values(self) -> None:
        for value in ("", "0", "off", "no"):
            self.assertEqual(self._ring(value), "", value)

    def test_osc9_carries_state_and_name(self) -> None:
        self.assertEqual(self._ring("osc9", attention.WAITING_APPROVAL), "\x1b]9;小羽 · zhinu · 等审批\x07")

    def test_osc777_splits_title_and_body(self) -> None:
        self.assertEqual(self._ring("osc777"), "\x1b]777;notify;小羽 · zhinu;等输入\x07")

    def test_osc99_sends_title_then_body(self) -> None:
        self.assertEqual(
            self._ring("osc99"),
            "\x1b]99;i=xiaoyu:d=0;小羽 · zhinu\x1b\\\x1b]99;i=xiaoyu:d=1:p=body;等输入\x1b\\",
        )

    def test_auto_picks_by_terminal(self) -> None:
        self.assertTrue(self._ring("auto", KITTY_WINDOW_ID="3").startswith("\x1b]99;"))
        self.assertTrue(self._ring("auto", TERM="xterm-kitty").startswith("\x1b]99;"))
        self.assertTrue(self._ring("auto", TERM_PROGRAM="iTerm.app").startswith("\x1b]9;"))
        self.assertTrue(self._ring("auto", TERM_PROGRAM="WezTerm").startswith("\x1b]9;"))
        self.assertTrue(self._ring("auto", TERM_PROGRAM="ghostty").startswith("\x1b]9;"))
        self.assertEqual(self._ring("auto", TERM_PROGRAM="Apple_Terminal"), "\x07")
        self.assertEqual(self._ring("auto"), "\x07")

    def test_unknown_value_still_rings(self) -> None:
        self.assertEqual(self._ring("ding"), "\x07")

    def test_tmux_wraps_in_dcs_and_doubles_esc(self) -> None:
        out = self._ring("osc9", TMUX="/tmp/tmux-501/default,1,0")
        self.assertEqual(out, "\x1bPtmux;\x1b\x1b]9;小羽 · zhinu · 等输入\x07\x1b\\")
        #  BEL 不是 ESC 序列，tmux 本来就透传，不包
        self.assertEqual(self._ring("bel", TMUX="/tmp/tmux-501/default,1,0"), "\x07")

    def test_name_in_notification_is_sanitized(self) -> None:
        stream = _Tty()
        with mock.patch.dict(os.environ, self._env(**{attention.BELL_ENV: "osc9"}), clear=False):
            os.environ.pop("TMUX", None)
            attention.set_title(Path("/work/evil\x1b]0;x\x07dir"), stream)
            attention.ring(stream, attention.WAITING_INPUT)
        self.assertEqual(stream.getvalue(), "\x1b]9;小羽 · evildir · 等输入\x07")


class TitleTest(unittest.TestCase):
    def tearDown(self) -> None:
        #  模块记着"当前会话名"，用例之间别串
        attention.clear_title(io.StringIO())

    def test_title_strips_control_sequences_from_directory_name(self) -> None:
        text = attention.title_text(Path("/tmp/proj\x1b]0;evil\x07\nname"))
        self.assertEqual(text, "就绪 · proj name · xiaoyu")
        self.assertNotIn("\x1b", text)

    def test_title_has_state_then_name_then_app(self) -> None:
        self.assertEqual(
            attention.title_text(Path("/work/zhinu"), attention.WAITING_APPROVAL),
            "等审批 · zhinu · xiaoyu",
        )
        #  具名会话用名字而不是目录名；名字同样去控制字符
        self.assertEqual(
            attention.title_text(Path("/work/zhinu"), attention.RUNNING, "deploy\x1b[31m"),
            "运行中 · deploy · xiaoyu",
        )

    def test_set_pushes_then_sets_and_clear_restores(self) -> None:
        stream = _Tty()
        with mock.patch.dict(os.environ, {attention.TITLE_ENV: "1"}):
            attention.set_title(Path("/work/zhinu"), stream)
            attention.clear_title(stream)
        out = stream.getvalue()
        #  先 push 旧标题，再 OSC 0 设新标题；退出先清空（不认标题栈的终端）再 pop
        self.assertTrue(out.startswith("\x1b[22;0t\x1b]0;就绪 · zhinu · xiaoyu\x07"))
        self.assertTrue(out.endswith("\x1b]0;\x07\x1b[23;0t"))

    def test_state_changes_only_rewrite_the_title_without_pushing_again(self) -> None:
        stream = _Tty()
        with mock.patch.dict(os.environ, {attention.TITLE_ENV: "1"}):
            os.environ.pop(attention.BELL_ENV, None)
            os.environ.pop(attention.HOOK_ENV, None)
            attention.set_title(Path("/work/zhinu"), stream, session="deploy")
            attention.running(stream)
            attention.waiting(attention.WAITING_APPROVAL, stream)
            attention.waiting(attention.WAITING_INPUT, stream)
            attention.clear_title(stream)
        out = stream.getvalue()
        self.assertEqual(out.count("\x1b[22;0t"), 1)  # 标题栈只 push 一次
        self.assertEqual(out.count("\x1b[23;0t"), 1)  # 退出 pop 一次，配平
        self.assertIn("\x1b]0;就绪 · deploy · xiaoyu\x07", out)
        self.assertIn("\x1b]0;运行中 · deploy · xiaoyu\x07", out)
        self.assertIn("\x1b]0;等审批 · deploy · xiaoyu\x07", out)
        self.assertIn("\x1b]0;等输入 · deploy · xiaoyu\x07", out)

    def test_state_change_without_a_title_writes_nothing(self) -> None:
        """没进交互前端（没 set_title）就没有标题可改：-p 等路径一个字节不写。"""
        stream = _Tty()
        with mock.patch.dict(os.environ, {attention.TITLE_ENV: "1"}):
            os.environ.pop(attention.BELL_ENV, None)
            os.environ.pop(attention.HOOK_ENV, None)
            attention.running(stream)
            attention.waiting(attention.WAITING_INPUT, stream)
        self.assertEqual(stream.getvalue(), "")

    def test_session_label_reads_the_named_session(self) -> None:
        named = mock.Mock(path=Path("/s/20260101-120000-42-id-deploy.jsonl"))
        anonymous = mock.Mock(path=Path("/s/20260101-120000-42.jsonl"))
        self.assertEqual(attention.session_label(named), "deploy")
        self.assertEqual(attention.session_label(anonymous), "")
        self.assertEqual(attention.session_label(None), "")

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
