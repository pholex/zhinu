"""`xiaoyu term install / uninstall` 的测试。

这条命令写**工作区之外的 shell 启动文件**，所以重点和 editor_setup 一样在
"什么时候不该动手"：用户手写过不动、标记残缺不动、已经配好不重复加；
写前留备份；移除时只收走自己那一段，别的字节原样留下。
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import cli_term, shell_setup


class TestInitLine(unittest.TestCase):
    def test_zsh_line_with_flags(self) -> None:
        line = shell_setup.init_line("zsh", "xiaoyu", natural=True)
        self.assertEqual(line, 'eval "$(xiaoyu term init zsh --natural)"')

    def test_fish_pipes_into_source(self) -> None:
        self.assertEqual(shell_setup.init_line("fish", "xiaoyu"), "xiaoyu term init fish | source")

    def test_name_is_quoted(self) -> None:
        line = shell_setup.init_line("bash", "xiaoyu", name="a b")
        self.assertIn("--name 'a b'", line)


class TestRcPath(unittest.TestCase):
    def test_zsh_honours_zdotdir(self) -> None:
        with mock.patch.dict(os.environ, {"ZDOTDIR": "/tmp/zd"}):
            self.assertEqual(shell_setup.rc_path("zsh"), Path("/tmp/zd/.zshrc"))

    def test_bash_on_macos_uses_bash_profile(self) -> None:
        """macOS 的终端开登录 shell，只读 .bash_profile。"""
        with mock.patch.object(shell_setup.sys, "platform", "darwin"):
            self.assertEqual(shell_setup.rc_path("bash").name, ".bash_profile")
        with mock.patch.object(shell_setup.sys, "platform", "linux"):
            self.assertEqual(shell_setup.rc_path("bash").name, ".bashrc")

    def test_detect_shell(self) -> None:
        with mock.patch.dict(os.environ, {"SHELL": "/bin/zsh"}):
            self.assertEqual(shell_setup.detect_shell(), "zsh")
        with mock.patch.dict(os.environ, {"SHELL": "/usr/bin/tcsh"}):
            self.assertIsNone(shell_setup.detect_shell())


class TestPlanAndApply(unittest.TestCase):
    LINE = 'eval "$(xiaoyu term init zsh)"'

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".zshrc"

    def plan(self, line: str | None = None) -> shell_setup.Plan:
        return shell_setup.plan_install("zsh", line or self.LINE, self.path)

    def test_missing_file_is_created(self) -> None:
        plan = self.plan()
        self.assertEqual(plan.action, "install")
        shell_setup.apply(plan)
        self.assertEqual(self.path.read_text(), shell_setup.block(self.LINE))

    def test_appends_after_existing_content_and_backs_up(self) -> None:
        self.path.write_text("export A=1")  # 末尾没换行
        shell_setup.apply(self.plan())
        text = self.path.read_text()
        self.assertTrue(text.startswith("export A=1\n\n" + shell_setup.BEGIN))
        self.assertEqual(self.path.with_name(".zshrc.bak").read_text(), "export A=1")

    def test_second_run_is_a_noop(self) -> None:
        shell_setup.apply(self.plan())
        self.assertEqual(self.plan().action, "already")

    def test_changed_flags_replace_only_our_block(self) -> None:
        self.path.write_text("before\n")
        shell_setup.apply(self.plan())
        with self.path.open("a") as handle:
            handle.write("after\n")
        natural = 'eval "$(xiaoyu term init zsh --natural)"'
        plan = self.plan(natural)
        self.assertEqual(plan.action, "update")
        shell_setup.apply(plan)
        text = self.path.read_text()
        self.assertEqual(text, "before\n\n" + shell_setup.block(natural) + "after\n")
        self.assertNotIn(self.LINE + "\n", text)

    def test_hand_written_line_is_left_alone(self) -> None:
        """用户自己写过 term init：不替他改，也不再加一份。"""
        self.path.write_text('eval "$(/opt/py -m xiaoyu term init zsh)"\n')
        self.assertEqual(self.plan().action, "manual")

    def test_commented_out_line_does_not_count(self) -> None:
        self.path.write_text('# eval "$(xiaoyu term init zsh)"\n')
        self.assertEqual(self.plan().action, "install")

    def test_broken_markers_are_not_touched(self) -> None:
        self.path.write_text(shell_setup.BEGIN + "\nsomething\n")
        plan = self.plan()
        self.assertEqual(plan.action, "broken")
        with self.assertRaises(ValueError):
            shell_setup.apply(plan)

    def test_removal_restores_original_bytes(self) -> None:
        original = "export A=1\nalias l=ls\n"
        self.path.write_text(original)
        shell_setup.apply(self.plan())
        with mock.patch.object(shell_setup, "_candidate_paths", return_value=[self.path]):
            plans = shell_setup.removal_plans()
            self.assertEqual(len(plans), 1)
            shell_setup.apply_removal(plans[0])
            self.assertEqual(self.path.read_text(), original)
            self.assertEqual(shell_setup.removal_plans(), [])

    def test_removal_ignores_hand_written_line(self) -> None:
        self.path.write_text('eval "$(xiaoyu term init zsh)"\n')
        with mock.patch.object(shell_setup, "_candidate_paths", return_value=[self.path]):
            self.assertEqual(shell_setup.removal_plans(), [])
            self.assertEqual(shell_setup.installed_in(), [(self.path, "manual")])


class TestCommand(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".zshrc"
        for target, kwargs in (
            ("xiaoyu.shell_setup.rc_path", {"return_value": self.path}),
            ("xiaoyu.shell_setup._candidate_paths", {"return_value": [self.path]}),
            ("xiaoyu.term.default_launcher", {"return_value": "xiaoyu"}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cmd(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = cli_term.term_command(argv)
        return code, out.getvalue()

    def test_install_then_uninstall(self) -> None:
        code, _ = self.run_cmd(["install", "zsh", "--natural", "--yes"])
        self.assertEqual(code, 0)
        self.assertIn('eval "$(xiaoyu term init zsh --natural)"', self.path.read_text())
        code, _ = self.run_cmd(["uninstall", "--yes"])
        self.assertEqual(code, 0)
        self.assertNotIn("term init", self.path.read_text())

    def test_dry_run_writes_nothing(self) -> None:
        code, out = self.run_cmd(["install", "zsh", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("将写入", out)
        self.assertFalse(self.path.exists())

    def test_declined_confirmation_writes_nothing(self) -> None:
        with mock.patch("builtins.input", return_value="n"):
            code, _ = self.run_cmd(["install", "zsh"])
        self.assertEqual(code, 1)
        self.assertFalse(self.path.exists())

    def test_natural_rejected_for_bash(self) -> None:
        code, out = self.run_cmd(["install", "bash", "--natural", "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("--natural", out)
        self.assertFalse(self.path.exists())

    def test_unknown_shell_asks_to_name_it(self) -> None:
        with mock.patch.dict(os.environ, {"SHELL": ""}):
            code, out = self.run_cmd(["install", "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("xiaoyu term install zsh", out)


if __name__ == "__main__":
    unittest.main()
