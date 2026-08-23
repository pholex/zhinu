"""只读工具（grep / list_files）与 explore 子 agent 的隔离约束测试。不打网络。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.config import Config
from xiaoyu.tools import Toolbox, _locate_grep


class ReadonlyToolsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.config = Config(base_url="x", model="x", workspace=self.root, enable_plugins=False)
        self.box = Toolbox(self.config)

        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "core.py").write_text(
            "def fetch_user(uid):\n    return uid\n\n\nTIMEOUT = 30\n", encoding="utf-8"
        )
        (self.root / "pkg" / "api.py").write_text(
            "from pkg.core import fetch_user\n\n\ndef view():\n    return fetch_user(1)\n",
            encoding="utf-8",
        )
        (self.root / "notes.md").write_text("fetch_user 是入口\n", encoding="utf-8")
        #  噪声目录：必须被跳过
        noise = self.root / "node_modules" / "junk"
        noise.mkdir(parents=True)
        (noise / "index.js").write_text("fetch_user();\n", encoding="utf-8")
        cache = self.root / "__pycache__"
        cache.mkdir()
        (cache / "core.pyc").write_text("fetch_user\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TestGrep(ReadonlyToolsTestCase):
    def test_finds_matches_with_line_numbers(self) -> None:
        result = self.box.run("grep", {"pattern": "def fetch_user"})
        self.assertIn("pkg/core.py:1", result)

    def test_skips_noise_directories(self) -> None:
        result = self.box.run("grep", {"pattern": "fetch_user"})
        self.assertNotIn("node_modules", result)
        self.assertNotIn("__pycache__", result)

    def test_glob_filter(self) -> None:
        result = self.box.run("grep", {"pattern": "fetch_user", "glob": "*.md"})
        self.assertIn("notes.md", result)
        self.assertNotIn("core.py", result)

    def test_no_match_is_not_an_error(self) -> None:
        result = self.box.run("grep", {"pattern": "绝对不存在的符号XYZ"})
        self.assertIn("没有匹配", result)
        self.assertFalse(result.startswith("ERROR"))

    def test_paths_are_workspace_relative(self) -> None:
        result = self.box.run("grep", {"pattern": "TIMEOUT"})
        self.assertNotIn(str(self.root), result)

    def test_max_matches_truncates(self) -> None:
        (self.root / "many.py").write_text("x = 1\n" * 50, encoding="utf-8")
        result = self.box.run("grep", {"pattern": "x = 1", "max_matches": 5})
        self.assertIn("未显示", result)


class TestGrepWithoutRipgrep(ReadonlyToolsTestCase):
    """模拟 Windows：机器上既没有 rg 也没有 grep.exe。

    以前这条路直接抛 [WinError 2] 变成 `ERROR: 无法搜索`，模型每搜一次白烧一轮，
    只能退化成一个个 read_file——explore 的 12 轮上限就是这么烧光的。
    """

    def setUp(self) -> None:
        super().setUp()
        #  两条都要掐死：which 挡掉 rg，_locate_grep 挡掉 grep。只 patch which
        #  是不够的——真在 Windows 上跑时 _locate_grep 会探到 runner/开发机预装的
        #  Git 自带 grep，这组用例就悄悄测了另一条分支（CI 上真这么翻过车）。
        for target, value in (
            ("xiaoyu.tools.shutil.which", None),
            ("xiaoyu.tools._locate_grep", None),
        ):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_finds_matches_with_line_numbers(self) -> None:
        result = self.box.run("grep", {"pattern": "def fetch_user"})
        self.assertNotIn("ERROR", result)
        self.assertIn("pkg/core.py:1", result)

    def test_skips_noise_directories(self) -> None:
        result = self.box.run("grep", {"pattern": "fetch_user"})
        self.assertNotIn("node_modules", result)
        self.assertNotIn("__pycache__", result)

    def test_glob_filter(self) -> None:
        result = self.box.run("grep", {"pattern": "fetch_user", "glob": "*.md"})
        self.assertIn("notes.md", result)
        self.assertNotIn("core.py", result)

    def test_path_glob_filter(self) -> None:
        result = self.box.run("grep", {"pattern": "fetch_user", "glob": "pkg/*.py"})
        self.assertIn("pkg/api.py", result)
        self.assertNotIn("notes.md", result)

    def test_no_match_is_not_an_error(self) -> None:
        result = self.box.run("grep", {"pattern": "绝对不存在的符号XYZ"})
        self.assertIn("没有匹配", result)
        self.assertFalse(result.startswith("ERROR"))

    def test_paths_are_workspace_relative(self) -> None:
        result = self.box.run("grep", {"pattern": "TIMEOUT"})
        self.assertNotIn(str(self.root), result)

    def test_max_matches_truncates(self) -> None:
        (self.root / "many.py").write_text("x = 1\n" * 50, encoding="utf-8")
        result = self.box.run("grep", {"pattern": "x = 1", "max_matches": 5})
        self.assertIn("未显示", result)

    def test_skips_binary_files(self) -> None:
        (self.root / "blob.bin").write_bytes(b"fetch_user\x00\x01\x02")
        result = self.box.run("grep", {"pattern": "fetch_user"})
        self.assertNotIn("blob.bin", result)

    def test_hints_to_install_ripgrep(self) -> None:
        result = self.box.run("grep", {"pattern": "fetch_user"})
        self.assertIn("ripgrep", result)

    def test_bad_regex_is_reported(self) -> None:
        result = self.box.run("grep", {"pattern": "(unclosed"})
        self.assertIn("正则不合法", result)

    def test_missing_path_still_errors(self) -> None:
        result = self.box.run("grep", {"pattern": "x", "path": "没有这个目录"})
        self.assertIn("路径不存在", result)


class TestLocateGrep(unittest.TestCase):
    """PATH 上没有 grep 时，Windows 要能捡起 Git for Windows 自带的那个 GNU grep。

    它就躺在 Git\\usr\\bin\\grep.exe，但只有 Git\\cmd 进 PATH——同一台机器上
    Git Bash 里 which 找得到、cmd/PowerShell 里找不到，正是用户遇到的那半边。
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)

    def _fake_git_install(self, base: Path) -> Path:
        """造一棵假的 Git for Windows 目录树，返回其中的 grep.exe。"""
        grep = base / "Git" / "usr" / "bin" / "grep.exe"
        grep.parent.mkdir(parents=True)
        grep.write_text("", encoding="utf-8")
        git = base / "Git" / "cmd" / "git.exe"
        git.parent.mkdir(parents=True)
        git.write_text("", encoding="utf-8")
        return grep

    def _which(self, **answers: str):
        return mock.patch(
            "xiaoyu.tools.shutil.which", side_effect=lambda name: answers.get(name)
        )

    def _env(self, **overrides: str):
        """把所有会被探测的 Windows 目录变量指到一个空目录，只留本用例要验的那个。

        不这么做，在真 Windows 上（CI runner、用户开发机）会探到**真实**的
        Git 安装，"没装 Git 时返回 None"这类断言必然假阳性——v0.30.5 第一次发版
        就是栽在这上面（Windows job 红、publish 被跳过）。也不能用 clear=True 清
        整个 env：那会抹掉 SYSTEMROOT/USERPROFILE 之类，Windows 上另有一串连坐。
        """
        blank = str(self.root / "没有这个目录")
        env = {
            key: blank
            for key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA")
        }
        env.update(overrides)
        return mock.patch.dict(os.environ, env)

    def test_path_grep_wins(self) -> None:
        with self._which(grep="/usr/bin/grep"):
            self.assertEqual(_locate_grep(), "/usr/bin/grep")

    def test_posix_does_not_probe(self) -> None:
        """非 Windows 上 PATH 没有就是没有，不去乱猜路径。"""
        with self._which(), mock.patch("xiaoyu.tools.os.name", "posix"):
            self.assertIsNone(_locate_grep())

    def test_windows_finds_grep_next_to_git_exe(self) -> None:
        grep = self._fake_git_install(self.root)
        git = str(self.root / "Git" / "cmd" / "git.exe")
        with (
            self._which(git=git),
            mock.patch("xiaoyu.tools.os.name", "nt"),
            self._env(),
        ):
            self.assertEqual(_locate_grep(), str(grep))

    def test_windows_probes_program_files_without_git_on_path(self) -> None:
        grep = self._fake_git_install(self.root)
        with (
            self._which(),
            mock.patch("xiaoyu.tools.os.name", "nt"),
            self._env(ProgramFiles=str(self.root)),
        ):
            self.assertEqual(_locate_grep(), str(grep))

    def test_windows_probes_user_scope_install(self) -> None:
        grep = self._fake_git_install(self.root / "Programs")
        with (
            self._which(),
            mock.patch("xiaoyu.tools.os.name", "nt"),
            self._env(LOCALAPPDATA=str(self.root)),
        ):
            self.assertEqual(_locate_grep(), str(grep))

    def test_windows_without_git_returns_none(self) -> None:
        with (
            self._which(),
            mock.patch("xiaoyu.tools.os.name", "nt"),
            self._env(),
        ):
            self.assertIsNone(_locate_grep())


class TestListFiles(ReadonlyToolsTestCase):
    def test_lists_python_files(self) -> None:
        result = self.box.run("list_files", {"pattern": "**/*.py"})
        self.assertIn("pkg/core.py", result)
        self.assertIn("pkg/api.py", result)

    def test_skips_noise(self) -> None:
        result = self.box.run("list_files", {})
        self.assertNotIn("node_modules", result)
        self.assertNotIn("__pycache__", result)

    def test_no_match(self) -> None:
        self.assertIn("没有匹配", self.box.run("list_files", {"pattern": "**/*.rs"}))


class TestReadonlySubset(ReadonlyToolsTestCase):
    def test_readonly_toolbox_excludes_mutating_tools(self) -> None:
        box = Toolbox(self.config, only=Toolbox.READONLY)
        self.assertEqual(sorted(box.names()), ["grep", "list_files", "read_file"])
        #  这是 explore 子 agent 的安全边界：给了 bash 就等于给了写权限
        for forbidden in ("bash", "write_file", "str_replace", "explore"):
            self.assertIsNone(box.get(forbidden), f"只读子集里不该有 {forbidden}")

    def test_readonly_tools_never_require_approval(self) -> None:
        box = Toolbox(self.config, only=Toolbox.READONLY)
        for name in box.names():
            tool = box.get(name)
            self.assertFalse(tool.requires_approval, f"{name} 不该需要确认")

    def test_readonly_subset_cannot_modify_anything(self) -> None:
        box = Toolbox(self.config, only=Toolbox.READONLY)
        before = {
            path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()
        }
        box.run("grep", {"pattern": "fetch_user"})
        box.run("list_files", {"pattern": "**/*.py"})
        box.run("read_file", {"path": "pkg/core.py"})
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after, "只读工具改动了文件")

    def test_unknown_tool_in_subset_raises(self) -> None:
        with self.assertRaises(ValueError):
            Toolbox(self.config, only=["read_file", "不存在的工具"])

    def test_full_toolbox_has_explore_absent_until_agent_registers(self) -> None:
        #  explore 由 Agent 挂载（它需要 client 和 usage），Toolbox 本身不含
        self.assertIsNone(self.box.get("explore"))
        self.assertEqual(
            sorted(self.box.names()),
            [
                "bash", "browser", "grep", "kill_task", "list_files", "monitor",
                "read_file", "recall", "str_replace", "task_output", "write_file",
            ],
        )


class TestExploreToolShape(unittest.TestCase):
    def test_tool_schema_is_wellformed(self) -> None:
        from xiaoyu.explore import make_explore_tool

        config = Config(base_url="x", model="x", workspace=Path.cwd())
        tool = make_explore_tool(config, registry=None, usage=None)
        schema = tool.schema()
        self.assertEqual(schema["function"]["name"], "explore")
        self.assertIn("question", schema["function"]["parameters"]["properties"])
        self.assertFalse(tool.requires_approval, "只读检索不需要人工确认")

    def test_sub_agent_config_disables_nesting(self) -> None:
        """子 agent 必须关掉 explore，否则会无限套娃。"""
        from xiaoyu.config import Config as C

        sub = C(base_url="x", model="x", workspace=Path.cwd(), enable_explore=False)
        self.assertFalse(sub.enable_explore)

    def test_prompt_locks_key_elements(self) -> None:
        """锁住 explore prompt 的关键要素（证据自证 + 代价声明 + 负样本）。"""
        from xiaoyu.explore import EXPLORE_PROMPT

        self.assertIn("路径:行号", EXPLORE_PROMPT)
        #  错误代价声明（反垃圾 prompt 要素）：写错比漏掉严重
        self.assertIn("代价不对等", EXPLORE_PROMPT)
        self.assertIn("未确认", EXPLORE_PROMPT)
        #  负样本清单：分清"出现过"和"被使用"（D 组实验的诱饵常量）
        self.assertIn("形似不算命中", EXPLORE_PROMPT)


if __name__ == "__main__":
    unittest.main()
