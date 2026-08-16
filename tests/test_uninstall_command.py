"""xiaoyu uninstall 卸载命令的测试。

锁住五件事：
1. 默认保留配置目录，--purge 才删；
2. terminal-setup 写入的编辑器键绑定在卸载时一并移除；
3. pip 不存在（pipx/uv tool 环境）时收尾照做、包本体给出替代命令并返回 1；
4. pip uninstall 作为独立 argv 由本解释器执行，失败返回 1；
5. --dry-run 只打计划，什么都不动。
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xiaoyu import cli, editor_setup

UNINSTALL_ARGV = [sys.executable, "-m", "pip", "uninstall", "-y", "xiaoyu-agent"]


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.uninstall_command(argv)
    return code, out.getvalue()


class UninstallCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config_dir = Path(tempfile.mkdtemp()) / "xiaoyu"
        (self.config_dir / "sessions").mkdir(parents=True)
        for target, kwargs in (
            ("xiaoyu.config.user_config_dir", {"return_value": self.config_dir}),
            #  真跑会去敲 macOS Keychain（security 子进程），测试里一律断开
            ("xiaoyu.cli._hint_keychain_leftover", {}),
            ("xiaoyu.editor_setup.removal_plans", {"return_value": []}),
        ):
            patcher = mock.patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls: list[list[str]] = []

    def fake_run(self, *, probe_ok: bool = True, uninstall_ok: bool = True):
        def run(argv, **kwargs):
            self.calls.append(argv)
            if "--version" in argv:
                return SimpleNamespace(returncode=0 if probe_ok else 1)
            return SimpleNamespace(returncode=0 if uninstall_ok else 1)

        return run

    def test_default_keeps_config_dir(self) -> None:
        with mock.patch.object(cli.subprocess, "run", side_effect=self.fake_run()):
            code, output = _run(["--yes"])
        self.assertEqual(code, 0)
        self.assertTrue(self.config_dir.is_dir())
        self.assertIn("保留配置目录", output)
        self.assertIn(UNINSTALL_ARGV, self.calls)

    def test_purge_deletes_config_dir(self) -> None:
        with mock.patch.object(cli.subprocess, "run", side_effect=self.fake_run()):
            code, output = _run(["--purge", "--yes"])
        self.assertEqual(code, 0)
        self.assertFalse(self.config_dir.exists())
        self.assertIn(UNINSTALL_ARGV, self.calls)

    def test_removes_editor_bindings(self) -> None:
        root = Path(tempfile.mkdtemp())
        path = root / "keybindings.json"
        path.write_text(
            json.dumps([{"key": "ctrl+k", "command": "保留我"}, editor_setup._BINDING]),
            encoding="utf-8",
        )
        plan = editor_setup.Plan(editor_setup.Editor("测试编辑器", "Test"), path, "remove")
        with mock.patch.object(cli.subprocess, "run", side_effect=self.fake_run()), \
                mock.patch("xiaoyu.editor_setup.removal_plans", return_value=[plan]):
            code, output = _run(["--yes"])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            [{"key": "ctrl+k", "command": "保留我"}],
        )
        self.assertIn("测试编辑器", output)

    def test_no_pip_still_cleans_but_returns_one(self) -> None:
        """pipx/uv 环境：附属物照收拾，包本体给出对应命令，返回 1 表示没卸完。"""
        with mock.patch.object(cli.subprocess, "run", side_effect=self.fake_run(probe_ok=False)):
            code, output = _run(["--purge", "--yes"])
        self.assertEqual(code, 1)
        self.assertFalse(self.config_dir.exists())  # 收尾做了
        self.assertEqual(len(self.calls), 1)  # 探测失败后不再碰 pip
        self.assertIn("pipx uninstall xiaoyu-agent", output)
        self.assertIn("uv tool uninstall xiaoyu-agent", output)

    def test_dry_run_touches_nothing(self) -> None:
        with mock.patch.object(cli.subprocess, "run", side_effect=self.fake_run()):
            code, output = _run(["--purge", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertTrue(self.config_dir.is_dir())
        self.assertEqual(len(self.calls), 1)  # 只有 pip 探测，没有 uninstall
        self.assertIn("将删除", output)

    def test_declined_confirmation_changes_nothing(self) -> None:
        with mock.patch.object(cli.subprocess, "run", side_effect=self.fake_run()), \
                mock.patch("builtins.input", return_value="n"):
            code, output = _run(["--purge"])
        self.assertEqual(code, 1)
        self.assertTrue(self.config_dir.is_dir())
        self.assertNotIn(UNINSTALL_ARGV, self.calls)
        self.assertIn("没有改动", output)

    def test_pip_failure_returns_one(self) -> None:
        with mock.patch.object(
            cli.subprocess, "run", side_effect=self.fake_run(uninstall_ok=False)
        ):
            code, output = _run(["--yes"])
        self.assertEqual(code, 1)
        self.assertIn("卸载失败", output)

    def test_main_dispatches_uninstall(self) -> None:
        with mock.patch.object(cli, "uninstall_command", return_value=0) as command:
            code = cli.main(["uninstall", "--purge"])
        self.assertEqual(code, 0)
        command.assert_called_once_with(["--purge"])


if __name__ == "__main__":
    unittest.main()
