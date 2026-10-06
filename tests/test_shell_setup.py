"""`xiaoyu term install / uninstall` 的测试。

这条命令写**工作区之外的 shell 启动文件**，所以重点和 editor_setup 一样在
"什么时候不该动手"：用户手写过不动、标记残缺不动、已经配好不重复加；
写前留备份；移除时只收走自己那一段，别的字节（包括 CRLF 换行）原样留下。
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

INIT = 'eval "$(xiaoyu term init zsh)"'


def _read(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _write(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


class TestIntegrationLines(unittest.TestCase):
    def test_zsh_brings_compinit_completion_and_init(self) -> None:
        lines, notes = shell_setup.integration_lines("zsh", "xiaoyu", natural=True)
        self.assertEqual(
            lines,
            [
                shell_setup._ZSH_COMPINIT,
                'eval "$(xiaoyu completion zsh)"',
                'eval "$(xiaoyu term init zsh --natural)"',
            ],
        )
        self.assertEqual(notes, [])

    def test_bash_has_no_compinit(self) -> None:
        lines, _ = shell_setup.integration_lines("bash", "xiaoyu")
        self.assertEqual(lines, ['eval "$(xiaoyu completion bash)"', 'eval "$(xiaoyu term init bash)"'])

    def test_fish_pipes_into_source(self) -> None:
        lines, _ = shell_setup.integration_lines("fish", "xiaoyu")
        self.assertEqual(lines, ["xiaoyu completion fish | source", "xiaoyu term init fish | source"])

    def test_no_completion(self) -> None:
        lines, _ = shell_setup.integration_lines("zsh", "xiaoyu", completion=False)
        self.assertEqual(lines, [INIT])

    def test_completion_skipped_when_xiaoyu_not_on_path(self) -> None:
        """补全挂在命令名上：PATH 里没有 xiaoyu，写了也触发不到，要说明。"""
        lines, notes = shell_setup.integration_lines("zsh", "'/opt/py' -m xiaoyu")
        self.assertEqual(lines, ['eval "$(\'/opt/py\' -m xiaoyu term init zsh)"'])
        self.assertTrue(notes and "PATH" in notes[0])

    def test_name_is_quoted(self) -> None:
        lines, _ = shell_setup.integration_lines("bash", "xiaoyu", name="a b")
        self.assertIn("--name 'a b'", lines[-1])


class TestPlatform(unittest.TestCase):
    def test_zsh_honours_zdotdir(self) -> None:
        with mock.patch.dict(os.environ, {"ZDOTDIR": str(Path("/tmp/zd"))}):
            self.assertEqual(shell_setup.rc_path("zsh"), Path("/tmp/zd") / ".zshrc")

    def test_bash_rc_file_per_platform(self) -> None:
        """macOS 的终端开登录 shell，只读 .bash_profile；Linux 与 Git Bash 读 .bashrc。"""
        for platform, name in (("darwin", ".bash_profile"), ("linux", ".bashrc"), ("win32", ".bashrc")):
            with self.subTest(platform=platform), mock.patch.object(shell_setup.sys, "platform", platform):
                self.assertEqual(shell_setup.rc_path("bash").name, name)

    def test_detect_shell(self) -> None:
        for value, expected in (
            ("/bin/zsh", "zsh"),
            ("/usr/bin/bash.exe", "bash"),  # Git Bash
            ("/usr/bin/tcsh", None),
            ("", None),  # Windows 原生终端没有 $SHELL
        ):
            with self.subTest(value=value), mock.patch.dict(os.environ, {"SHELL": value}):
                self.assertEqual(shell_setup.detect_shell(), expected)


class TestPlanAndApply(unittest.TestCase):
    LINES = [INIT]

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / ".zshrc"

    def plan(self, lines: list[str] | None = None) -> shell_setup.Plan:
        return shell_setup.plan_install("zsh", lines or self.LINES, self.path)

    def test_missing_file_is_created(self) -> None:
        plan = self.plan()
        self.assertEqual(plan.action, "install")
        shell_setup.apply(plan)
        self.assertEqual(_read(self.path), shell_setup.block(self.LINES))

    def test_appends_after_existing_content_and_backs_up(self) -> None:
        _write(self.path, "export A=1")  # 末尾没换行
        shell_setup.apply(self.plan())
        self.assertTrue(_read(self.path).startswith("export A=1\n\n" + shell_setup.BEGIN))
        self.assertEqual(_read(self.path.with_name(".zshrc.bak")), "export A=1")

    def test_second_run_is_a_noop(self) -> None:
        shell_setup.apply(self.plan())
        self.assertEqual(self.plan().action, "already")

    def test_changed_lines_replace_only_our_block(self) -> None:
        _write(self.path, "before\n")
        shell_setup.apply(self.plan())
        _write(self.path, _read(self.path) + "after\n")
        new = shell_setup.integration_lines("zsh", "xiaoyu", natural=True)[0]
        plan = self.plan(new)
        self.assertEqual(plan.action, "update")
        shell_setup.apply(plan)
        self.assertEqual(_read(self.path), "before\n\n" + shell_setup.block(new) + "after\n")

    def test_crlf_file_stays_crlf(self) -> None:
        """文件原本是 CRLF 就按 CRLF 写，移除后字节原样还原。"""
        original = "export A=1\r\nalias l=ls\r\n"
        _write(self.path, original)
        shell_setup.apply(self.plan())
        text = _read(self.path)
        self.assertNotIn("\n", text.replace("\r\n", ""))
        self.assertEqual(self.plan().action, "already")
        with mock.patch.object(shell_setup, "_candidate_paths", return_value=[self.path]):
            shell_setup.apply_removal(shell_setup.removal_plans()[0])
        self.assertEqual(_read(self.path), original)

    def test_new_file_uses_lf_on_every_platform(self) -> None:
        shell_setup.apply(self.plan())
        self.assertNotIn("\r", _read(self.path))

    def test_hand_written_line_is_left_alone(self) -> None:
        """用户自己写过 term init：不替他改，也不再加一份；缺补全就告诉他加哪行。"""
        _write(self.path, 'eval "$(/opt/py -m xiaoyu term init zsh)"\n')
        lines = shell_setup.integration_lines("zsh", "xiaoyu")[0]
        plan = self.plan(lines)
        self.assertEqual(plan.action, "manual")
        self.assertTrue(any("xiaoyu completion zsh" in note for note in plan.notes))
        self.assertTrue(any("compinit" in note for note in plan.notes))  # zsh 少了它 compdef 不存在

    def test_hand_written_with_completion_has_no_hint(self) -> None:
        _write(self.path, 'eval "$(xiaoyu completion zsh)"\neval "$(xiaoyu term init zsh)"\n')
        plan = self.plan(shell_setup.integration_lines("zsh", "xiaoyu")[0])
        self.assertEqual(plan.action, "manual")
        self.assertEqual(plan.notes, [])

    def test_commented_out_line_does_not_count(self) -> None:
        _write(self.path, '# eval "$(xiaoyu term init zsh)"\n')
        self.assertEqual(self.plan().action, "install")

    def test_broken_markers_are_not_touched(self) -> None:
        _write(self.path, shell_setup.BEGIN + "\nsomething\n")
        plan = self.plan()
        self.assertEqual(plan.action, "broken")
        with self.assertRaises(ValueError):
            shell_setup.apply(plan)

    def test_removal_restores_original_bytes(self) -> None:
        original = "export A=1\nalias l=ls\n"
        _write(self.path, original)
        shell_setup.apply(self.plan())
        with mock.patch.object(shell_setup, "_candidate_paths", return_value=[self.path]):
            plans = shell_setup.removal_plans()
            self.assertEqual(len(plans), 1)
            shell_setup.apply_removal(plans[0])
            self.assertEqual(_read(self.path), original)
            self.assertEqual(shell_setup.removal_plans(), [])

    def test_installed_in_reports_kind_and_completion(self) -> None:
        with mock.patch.object(shell_setup, "_candidate_paths", return_value=[self.path]):
            _write(self.path, INIT + "\n")
            found = shell_setup.installed_in()
            self.assertEqual([(f.kind, f.completion) for f in found], [("manual", False)])
            self.assertEqual(shell_setup.removal_plans(), [])
            _write(self.path, shell_setup.block(shell_setup.integration_lines("zsh", "xiaoyu")[0]))
            found = shell_setup.installed_in()
            self.assertEqual([(f.kind, f.completion) for f in found], [("marked", True)])


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
        text = _read(self.path)
        self.assertIn('eval "$(xiaoyu term init zsh --natural)"', text)
        self.assertIn('eval "$(xiaoyu completion zsh)"', text)
        code, _ = self.run_cmd(["uninstall", "--yes"])
        self.assertEqual(code, 0)
        self.assertNotIn("xiaoyu", _read(self.path))

    def test_no_completion_flag(self) -> None:
        self.run_cmd(["install", "zsh", "--no-completion", "--yes"])
        self.assertNotIn("completion", _read(self.path))

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
        with mock.patch.dict(os.environ, {"SHELL": ""}), mock.patch.object(cli_term, "_on_windows", return_value=False):
            code, out = self.run_cmd(["install", "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("xiaoyu term install zsh", out)

    def test_windows_without_shell_points_to_git_bash_and_powershell(self) -> None:
        with mock.patch.dict(os.environ, {"SHELL": ""}), mock.patch.object(cli_term, "_on_windows", return_value=True):
            code, out = self.run_cmd(["install", "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("term install bash", out)
        self.assertIn("PowerShell", out)


if __name__ == "__main__":
    unittest.main()
