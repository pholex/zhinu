"""`/copy` 与 `/export`：两个把会话内容拿出 REPL 的斜杠命令。

/copy 复制最后一条回复：先找本机剪贴板命令（mock 子进程），都没有退回终端的
OSC 52；没有回复时提示。/export 把当前会话文件转成 Markdown 落到工作区。
"""

from __future__ import annotations

import contextlib
import io
import os
import unittest
from unittest import mock

from xiaoyu import attention, media
from xiaoyu.cli import SLASH_COMMANDS, handle_slash

from .test_agent_paths import AgentTestCase


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _slash(agent, line: str) -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        handle_slash(agent, line)
    return buffer.getvalue()


class CopyTextTest(unittest.TestCase):
    """media.copy_text：按平台挑命令、喂 stdin、失败返回空串。"""

    def _run_ok(self):
        return mock.Mock(returncode=0)

    def test_macos_uses_pbcopy(self) -> None:
        with mock.patch.object(media.sys, "platform", "darwin"), mock.patch.object(
            media.shutil, "which", return_value="/usr/bin/pbcopy"
        ), mock.patch.object(media.subprocess, "run", return_value=self._run_ok()) as run:
            self.assertEqual(media.copy_text("你好"), "pbcopy")
        self.assertEqual(run.call_args.args[0], ["pbcopy"])
        self.assertEqual(run.call_args.kwargs["input"], "你好".encode("utf-8"))

    def test_linux_prefers_wayland_then_x11_then_wsl(self) -> None:
        def which(name: str):
            return f"/usr/bin/{name}" if name in installed else None

        with mock.patch.object(media.sys, "platform", "linux"), mock.patch.object(
            media.shutil, "which", side_effect=which
        ), mock.patch.object(media.subprocess, "run", return_value=self._run_ok()) as run:
            installed = {"wl-copy", "xclip"}
            with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
                self.assertEqual(media.copy_text("x"), "wl-copy")
            #  没有 Wayland 会话就跳过 wl-copy，哪怕装了
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("WAYLAND_DISPLAY", None)
                self.assertEqual(media.copy_text("x"), "xclip")
            self.assertEqual(run.call_args.args[0], ["xclip", "-selection", "clipboard"])
            installed = {"clip.exe"}
            self.assertEqual(media.copy_text("x"), "clip.exe")

    def test_no_tool_or_failure_returns_empty(self) -> None:
        with mock.patch.object(media.sys, "platform", "linux"), mock.patch.object(
            media.shutil, "which", return_value=None
        ), mock.patch.object(media.subprocess, "run") as run:
            self.assertEqual(media.copy_text("x"), "")
        run.assert_not_called()
        with mock.patch.object(media.sys, "platform", "darwin"), mock.patch.object(
            media.shutil, "which", return_value="/usr/bin/pbcopy"
        ), mock.patch.object(media.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(media.copy_text("x"), "")


class Osc52Test(unittest.TestCase):
    def test_sequence_is_base64_and_tmux_wrapped(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            self.assertEqual(attention.osc52_sequence("hi"), "\x1b]52;c;aGk=\x1b\\")
        with mock.patch.dict(os.environ, {"TMUX": "/tmp/tmux-501/default,1,0"}):
            self.assertEqual(
                attention.osc52_sequence("hi"), "\x1bPtmux;\x1b\x1b]52;c;aGk=\x1b\x1b\\\x1b\\"
            )

    def test_copy_via_terminal_only_on_a_tty(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TMUX", None)
            tty = _Tty()
            self.assertTrue(attention.copy_via_terminal("hi", tty))
            self.assertEqual(tty.getvalue(), "\x1b]52;c;aGk=\x1b\\")
            pipe = io.StringIO()
            self.assertFalse(attention.copy_via_terminal("hi", pipe))
            self.assertEqual(pipe.getvalue(), "")


class SlashCopyTest(AgentTestCase):
    def test_registered(self) -> None:
        self.assertIn("/copy", SLASH_COMMANDS)

    def test_copies_last_assistant_text(self) -> None:
        agent = self.build([])
        agent.messages.append({"role": "assistant", "content": "第一条"})
        agent.messages.append({"role": "user", "content": "再来"})
        agent.messages.append({"role": "assistant", "content": "最后一条"})
        with mock.patch.object(media, "copy_text", return_value="pbcopy") as copy:
            out = _slash(agent, "/copy")
        copy.assert_called_once_with("最后一条")
        self.assertIn("已复制", out)
        self.assertIn("pbcopy", out)

    def test_falls_back_to_osc52(self) -> None:
        agent = self.build([])
        agent.messages.append({"role": "assistant", "content": "答案"})
        with mock.patch.object(media, "copy_text", return_value=""), mock.patch.object(
            attention, "copy_via_terminal", return_value=True
        ) as osc:
            out = _slash(agent, "/copy")
        osc.assert_called_once_with("答案")
        self.assertIn("OSC 52", out)

    def test_nothing_to_copy(self) -> None:
        agent = self.build([])
        with mock.patch.object(media, "copy_text") as copy:
            out = _slash(agent, "/copy")
        copy.assert_not_called()
        self.assertIn("还没有可复制的回复", out)

    def test_no_channel_at_all_says_so(self) -> None:
        agent = self.build([])
        agent.messages.append({"role": "assistant", "content": "答案"})
        with mock.patch.object(media, "copy_text", return_value=""), mock.patch.object(
            attention, "copy_via_terminal", return_value=False
        ):
            out = _slash(agent, "/copy")
        self.assertIn("复制失败", out)


if __name__ == "__main__":
    unittest.main()
