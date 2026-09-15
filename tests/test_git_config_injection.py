"""宿主侧自动 git 调用不吃仓库自带配置注入的测试。

攻击形态：连同 .git 目录分发的仓库（zip、同步盘）在 .git/config 里写
core.fsmonitor / filter 驱动 / diff 驱动 / merge 驱动 / gpg.program，或往
.git/hooks 放可执行钩子——xiaoyu 自己起的 git（@ 补全、worktree 建与查、
宸枢 diff/merge、插件取包）只要一跑就替它执行了代码。

每条用例都走真实入口、对着真实"恶意仓库"跑，断言标记文件不存在。
机器上没有 git 就整体跳过。
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import chenshu as chenshu_mod
from xiaoyu import plugins as plugins_mod
from xiaoyu import worktree as worktree_mod

HAS_GIT = shutil.which("git") is not None


def _setup_git(root: Path, *args: str) -> str:
    """布置用的 git：固定身份、隔离用户配置，布置阶段本身不受本机配置干扰。"""
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    for key in [k for k in env if k.startswith("GIT_CONFIG_") and k not in (
        "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM",
    )]:
        env.pop(key)
    result = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", env=env, check=True,
    )
    return result.stdout.strip()


class MaliciousRepoCase(unittest.TestCase):
    """公共装配：一个布满执行面的仓库 + 标记目录。"""

    def setUp(self):
        tmp = tempfile.mkdtemp(prefix="xiaoyu-gitinj-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.base = Path(tmp)
        self.marks = self.base / "marks"
        self.marks.mkdir()
        self.root = self.base / "repo"
        self.root.mkdir()
        _setup_git(self.root, "init", "-q", "-b", "main")
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        (self.root / "b.evil").write_text("b\n", encoding="utf-8")
        (self.root / ".gitattributes").write_text(
            "*.evil filter=evil diff=evil merge=evil\n", encoding="utf-8"
        )
        _setup_git(self.root, "add", "-A")
        _setup_git(self.root, "commit", "-qm", "init")

    def mark_cmd(self, name: str) -> str:
        """一条创建标记文件的命令（经 git 的 sh 执行；正斜杠路径两端都认）。"""
        python = Path(sys.executable).as_posix()
        target = (self.marks / name).as_posix()
        return f"\"{python}\" -c \"open('{target}', 'w').close()\""

    def arm(self) -> None:
        """布雷：全部写进仓库级 .git/config 与 hooks，模拟随仓库分发。"""
        for key, name in (
            ("core.fsmonitor", "fsmonitor"),
            ("filter.evil.smudge", "smudge"),
            ("filter.evil.clean", "clean"),
            ("diff.evil.textconv", "textconv"),
            ("diff.evil.command", "diffcmd"),
            ("merge.evil.driver", "mergedrv"),
            ("diff.external", "extdiff"),
            ("core.pager", "pager"),
        ):
            _setup_git(self.root, "config", key, self.mark_cmd(name))
        if os.name != "nt":
            for hook in ("post-checkout", "pre-merge-commit", "post-merge", "commit-msg"):
                path = self.root / ".git" / "hooks" / hook
                path.write_text(
                    f"#!/bin/sh\n{self.mark_cmd('hook-' + hook)}\n", encoding="utf-8"
                )
                path.chmod(0o755)

    def fired(self) -> list[str]:
        return sorted(p.name for p in self.marks.iterdir())


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class WorkspaceFilesTest(MaliciousRepoCase):
    def test_at_completion_does_not_run_repo_fsmonitor(self):
        from rich.console import Console

        from xiaoyu.permissions import Permissions
        from xiaoyu.tui import Tui

        self.arm()
        tui = Tui(Permissions(self.root), console=Console(file=io.StringIO()))
        files = tui.workspace_files()
        self.assertEqual(self.fired(), [])
        #  加固不能把功能本身弄坏：仍走 git ls-files（含未跟踪的 .gitattributes 以外文件）
        self.assertIn("a.txt", files)
        self.assertIn("b.evil", files)


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class WorktreeTest(MaliciousRepoCase):
    def setUp(self):
        super().setUp()
        patch = mock.patch.object(worktree_mod, "user_config_dir", lambda: self.base / "cfg")
        patch.start()
        self.addCleanup(patch.stop)

    def test_create_does_not_run_fsmonitor_hooks_or_smudge(self):
        self.arm()
        path = worktree_mod.create(self.root, "t")
        self.assertTrue((path / "a.txt").is_file())
        self.assertEqual(self.fired(), [])

    def test_create_branch_does_not_run_hooks(self):
        self.arm()
        path = worktree_mod.create_branch(self.root, "mission-x", "m", "main")
        self.assertTrue((path / "a.txt").is_file())
        self.assertEqual(self.fired(), [])

    def test_dirty_does_not_run_fsmonitor_or_clean_filter(self):
        path = worktree_mod.create(self.root, "t")
        self.arm()
        (path / "b.evil").write_text("changed\n", encoding="utf-8")
        self.assertTrue(worktree_mod.dirty(path))
        self.assertEqual(self.fired(), [])

    def test_dirty_still_sees_untracked_and_clean_states(self):
        self.arm()
        path = worktree_mod.create(self.root, "t")
        self.assertFalse(worktree_mod.dirty(path))
        (path / "new.txt").write_text("n\n", encoding="utf-8")
        self.assertTrue(worktree_mod.dirty(path))
        self.assertEqual(self.fired(), [])


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class ChenshuGitTest(MaliciousRepoCase):
    def test_diff_does_not_run_diff_drivers_or_filters(self):
        self.arm()
        (self.root / "b.evil").write_text("changed\n", encoding="utf-8")
        result = chenshu_mod._git(["diff", "HEAD"], self.root)
        self.assertEqual(result.returncode, 0)
        self.assertIn("b.evil", result.stdout)
        self.assertEqual(chenshu_mod._git_out(["diff", "--name-only", "HEAD"], self.root), "b.evil")
        self.assertEqual(self.fired(), [])

    def test_merge_does_not_run_merge_driver_or_hooks(self):
        _setup_git(self.root, "checkout", "-q", "-b", "side")
        (self.root / "b.evil").write_text("side\n", encoding="utf-8")
        _setup_git(self.root, "commit", "-qam", "side")
        _setup_git(self.root, "checkout", "-q", "main")
        (self.root / "b.evil").write_text("main\n", encoding="utf-8")
        _setup_git(self.root, "commit", "-qam", "main")
        self.arm()
        result = chenshu_mod._git(["merge", "--no-ff", "side", "-m", "m"], self.root)
        #  驱动被钉空 = 退回冲突（fail-closed），而不是悄悄执行或悄悄取一边
        self.assertNotEqual(result.returncode, 0)
        chenshu_mod._git(["merge", "--abort"], self.root)
        self.assertEqual(self.fired(), [])

    def test_merge_does_not_run_gpg_program(self):
        _setup_git(self.root, "checkout", "-q", "-b", "side")
        (self.root / "c.txt").write_text("c\n", encoding="utf-8")
        _setup_git(self.root, "add", "c.txt")
        _setup_git(self.root, "commit", "-qm", "side")
        _setup_git(self.root, "checkout", "-q", "main")
        _setup_git(self.root, "config", "user.email", "t@t")
        _setup_git(self.root, "config", "user.name", "t")
        _setup_git(self.root, "config", "commit.gpgSign", "true")
        _setup_git(self.root, "config", "gpg.program", self.mark_cmd("gpg"))
        self.arm()
        result = chenshu_mod._git(["merge", "--no-ff", "side", "-m", "m"], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.fired(), [])

    def test_merge_commit_keeps_user_identity(self):
        """隔离全局配置后 merge commit 的身份仍是用户配置的那个，
        不能退化成 git 按主机名自动拼的 user@host。"""
        _setup_git(self.root, "checkout", "-q", "-b", "side")
        (self.root / "c.txt").write_text("c\n", encoding="utf-8")
        _setup_git(self.root, "add", "c.txt")
        _setup_git(self.root, "commit", "-qm", "side")
        _setup_git(self.root, "checkout", "-q", "main")
        user_config = self.base / "user.gitconfig"
        user_config.write_text(
            "[user]\n\tname = Global Person\n\temail = global@example.com\n", encoding="utf-8"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
        env["GIT_CONFIG_GLOBAL"] = str(user_config)
        with mock.patch.dict(os.environ, env, clear=True):
            result = chenshu_mod._git(["merge", "--no-ff", "side", "-m", "m"], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        author = _setup_git(self.root, "log", "-1", "--format=%an <%ae>|%cn <%ce>")
        self.assertEqual(author, "Global Person <global@example.com>|Global Person <global@example.com>")

    def test_repo_identity_still_wins_over_user_identity(self):
        _setup_git(self.root, "checkout", "-q", "-b", "side")
        (self.root / "c.txt").write_text("c\n", encoding="utf-8")
        _setup_git(self.root, "add", "c.txt")
        _setup_git(self.root, "commit", "-qm", "side")
        _setup_git(self.root, "checkout", "-q", "main")
        _setup_git(self.root, "config", "user.email", "repo@example.com")
        _setup_git(self.root, "config", "user.name", "Repo Person")
        user_config = self.base / "user.gitconfig"
        user_config.write_text(
            "[user]\n\tname = Global Person\n\temail = global@example.com\n", encoding="utf-8"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
        env["GIT_CONFIG_GLOBAL"] = str(user_config)
        with mock.patch.dict(os.environ, env, clear=True):
            result = chenshu_mod._git(["merge", "--no-ff", "side", "-m", "m"], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        author = _setup_git(self.root, "log", "-1", "--format=%an <%ae>")
        self.assertEqual(author, "Repo Person <repo@example.com>")


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class PluginsGitTest(MaliciousRepoCase):
    def test_plugin_git_does_not_run_repo_fsmonitor(self):
        self.arm()
        plugins_mod._git(["status", "--porcelain"], cwd=self.root)
        self.assertEqual(self.fired(), [])


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class InheritedConfigTest(MaliciousRepoCase):
    """调用方/父进程带来的配置注入不能盖过钉住的值。"""

    def test_inherited_config_parameters_cannot_reenable_fsmonitor(self):
        self.arm()
        #  git 自己的 sq 引用法：值里的 ' 写成 '\''——引号写错 git 会整条拒跑，
        #  断言就永远 fail 不了
        value = self.mark_cmd("params").replace("'", "'\\''")
        injected = f"'core.fsmonitor'='{value}'"
        with mock.patch.dict(os.environ, {"GIT_CONFIG_PARAMETERS": injected}):
            worktree_mod._run_git(["status", "--porcelain"], self.root)
        self.assertEqual(self.fired(), [])

    def test_inherited_config_count_is_continued_not_clobbered(self):
        self.arm()
        extra = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "xiaoyu.probe",
            "GIT_CONFIG_VALUE_0": "kept",
            "GIT_CONFIG_KEY_1": "core.fsmonitor",
            "GIT_CONFIG_VALUE_1": self.mark_cmd("count"),
        }
        with mock.patch.dict(os.environ, extra):
            worktree_mod._run_git(["status", "--porcelain"], self.root)
            probe = worktree_mod._run_git(["config", "--get", "xiaoyu.probe"], self.root)
        self.assertEqual(probe.stdout.strip(), "kept")
        self.assertEqual(self.fired(), [])

    def test_caller_dash_c_cannot_override_pins(self):
        self.arm()
        worktree_mod._run_git(
            ["-c", f"core.fsmonitor={self.mark_cmd('dashc')}", "status", "--porcelain"], self.root
        )
        self.assertEqual(self.fired(), [])


class HardenArgvTest(unittest.TestCase):
    def test_diff_family_gets_no_driver_flags(self):
        from xiaoyu import gitsafe

        for sub in ("diff", "show", "log"):
            argv, _ = gitsafe.prepare([sub, "HEAD"], None)
            self.assertEqual(argv[:4], ["git", sub, "--no-ext-diff", "--no-textconv"])
        argv, _ = gitsafe.prepare(["status", "--porcelain"], None)
        self.assertEqual(argv, ["git", "status", "--porcelain"])

    def test_dash_c_moves_into_env_before_pins(self):
        from xiaoyu import gitsafe

        argv, env = gitsafe.prepare(["-c", "user.name=x", "diff"], None, env={})
        self.assertEqual(argv[:2], ["git", "diff"])
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "user.name")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "x")
        keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
        self.assertIn("core.fsmonitor", keys)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")

    def test_bogus_inherited_count_restarts_numbering(self):
        from xiaoyu import gitsafe

        _, env = gitsafe.prepare(["status"], None, env={"GIT_CONFIG_COUNT": "abc"})
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "core.fsmonitor")

    def test_network_mode_keeps_user_config(self):
        from xiaoyu import gitsafe

        _, env = gitsafe.prepare(["clone", "x"], None, env={}, network=True)
        self.assertNotIn("GIT_CONFIG_GLOBAL", env)
        keys = {env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
        self.assertIn("core.hooksPath", keys)
        self.assertNotIn("credential.helper", keys)
        _, env = gitsafe.prepare(["status"], None, env={})
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)


if __name__ == "__main__":
    unittest.main()
