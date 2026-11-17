"""宿主侧 git 隔离用户配置之后，换行转换设置要跟着用户走。

不跟的后果：用户全局配了 core.autocrlf=true（Windows 安装器的默认值），签出的
文本文件是 CRLF、index 里是 LF；宿主的 git 按"不转换"比较，每个文件都成了
已修改——宸枢"工作区干净才合并"永远过不去，worktree 也删不掉。
这里用一份临时全局配置在任何平台上复现，不依赖本机设置。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import gitsafe
from xiaoyu import worktree as worktree_mod

HAS_GIT = shutil.which("git") is not None


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class LineEndingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = self.tmp / "repo"
        self.root.mkdir()
        self.user_config = self.tmp / "gitconfig"
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_CONFIG_")}
        env.update(GIT_CONFIG_GLOBAL=str(self.user_config), GIT_CONFIG_NOSYSTEM="1")
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.clear_caches()
        self.addCleanup(self.clear_caches)

    @staticmethod
    def clear_caches() -> None:
        gitsafe._line_ending_cache.clear()
        gitsafe._safe_directory_cache.clear()

    def user(self, text: str) -> None:
        self.user_config.write_text(text, encoding="utf-8", newline="\n")
        self.clear_caches()

    def git(self, *args: str) -> str:
        """用户自己敲的 git：读得到全局配置。"""
        result = subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=self.root,
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
        )
        return result.stdout

    def crlf_checkout(self) -> None:
        """用户眼里干净的仓库：工作区 CRLF、index LF。"""
        self.git("init", "-q", "-b", "main")
        (self.root / "a.txt").write_bytes(b"one\r\ntwo\r\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "init")
        self.assertEqual(self.git("status", "--porcelain"), "")

    def pins(self, args: list[str]) -> dict[str, str]:
        _, env = gitsafe.prepare(args, self.root)
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        return {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}

    def test_host_status_agrees_with_the_user_under_autocrlf(self) -> None:
        self.user("[core]\n\tautocrlf = true\n")
        self.crlf_checkout()
        result = worktree_mod._run_git(["status", "--porcelain", "--untracked-files=normal"], self.root)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertFalse(worktree_mod.dirty(self.root))

    def test_without_user_setting_nothing_is_pinned(self) -> None:
        self.user("")
        self.git("init", "-q", "-b", "main")
        self.assertNotIn("core.autocrlf", self.pins(["status"]))
        self.assertNotIn("core.eol", self.pins(["status"]))

    def test_repository_setting_wins_over_user_setting(self) -> None:
        """命令级会盖过仓库级：仓库自己设了就不能再把全局值转回来。"""
        self.user("[core]\n\tautocrlf = true\n\teol = crlf\n")
        self.git("init", "-q", "-b", "main")
        self.assertEqual(self.pins(["status"])["core.autocrlf"], "true")
        self.git("config", "core.autocrlf", "input")
        pins = self.pins(["status"])
        self.assertNotIn("core.autocrlf", pins)
        self.assertEqual(pins["core.eol"], "crlf")

    def test_only_known_values_are_carried(self) -> None:
        self.user("[core]\n\tautocrlf = yes\n\teol = whatever\n")
        self.git("init", "-q", "-b", "main")
        pins = self.pins(["diff"])
        self.assertEqual(pins["core.autocrlf"], "true")
        self.assertNotIn("core.eol", pins)

    def test_commands_that_read_no_content_skip_the_lookup(self) -> None:
        self.user("[core]\n\tautocrlf = true\n")
        self.git("init", "-q", "-b", "main")
        self.assertNotIn("core.autocrlf", self.pins(["rev-parse", "HEAD"]))


class WorktreeListingTest(unittest.TestCase):
    """对 `git worktree list --porcelain` 的路径比较不能按字面：Windows 上 git 打正斜杠。"""

    def test_matches_only_the_worktree_field_for_that_path(self) -> None:
        path = Path(tempfile.gettempdir()) / "wt" / "one"
        self.assertTrue(worktree_mod.listed(f"worktree {path}", path))
        self.assertTrue(worktree_mod.listed(f"worktree {path}{os.sep}", path))
        self.assertFalse(worktree_mod.listed(f"worktree {path}-other", path))
        self.assertFalse(worktree_mod.listed(f"branch {path}", path))
        self.assertFalse(worktree_mod.listed("", path))

    @unittest.skipUnless(os.name == "nt", "正斜杠与盘符大小写只在 Windows 上是同一条路径")
    def test_forward_slashes_and_drive_case_are_the_same_path(self) -> None:
        path = Path(tempfile.gettempdir()).resolve() / "wt" / "one"
        printed = path.as_posix()
        self.assertTrue(worktree_mod.listed("worktree " + printed, path))
        self.assertTrue(worktree_mod.listed("worktree " + printed[0].swapcase() + printed[1:], path))


if __name__ == "__main__":
    unittest.main()
