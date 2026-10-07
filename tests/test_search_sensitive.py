"""搜索类工具的敏感文件过滤：grep / list_files 不把 .env、私钥、.ssh 读进上下文。

三个后端（rg / grep / 纯 Python）都要挡得住；开关 XIAOYU_SEARCH_SENSITIVE=0 能关；
--unguarded 预设跟随护栏表。不打网络。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import guardrails
from xiaoyu.config import Config
from xiaoyu.tools import Toolbox, sensitive_reason


class SensitiveReasonTest(unittest.TestCase):
    def test_names_and_dirs(self) -> None:
        for shown in (
            ".env",
            ".env.production",
            "certs/server.pem",
            "keys/private.key",
            ".ssh/config",
            "home/u/.ssh/id_rsa",
            "id_ed25519.pub",
            ".gnupg/pubring.kbx",
            ".aws/credentials",
            ".netrc",
            "repo/.git/config",
        ):
            self.assertIsNotNone(sensitive_reason(Path(shown)), shown)

    def test_ordinary_files_pass(self) -> None:
        for shown in (
            "src/app.py",
            "environment.md",
            "keys.py",
            "aws/credentials.md",
            "docs/ssh-guide.md",
            ".envrc",
        ):
            self.assertIsNone(sensitive_reason(Path(shown)), shown)


class SearchFilterTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(base_url="x", model="x", workspace=self.root, enable_plugins=False)
        self.box = Toolbox(self.config)
        (self.root / "src").mkdir()
        (self.root / "src" / "app.py").write_text("SECRET_KEY = read_env()\n", encoding="utf-8")
        (self.root / ".env").write_text("SECRET_KEY=hunter2\n", encoding="utf-8")
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh" / "id_rsa").write_text("SECRET_KEY private\n", encoding="utf-8")
        (self.root / "certs").mkdir()
        (self.root / "certs" / "server.pem").write_text("SECRET_KEY pem\n", encoding="utf-8")

    def grep(self, **args: object) -> str:
        return self.box.run("grep", {"pattern": "SECRET_KEY", **args})


class GrepFilterTest(SearchFilterTestCase):
    def test_sensitive_hits_are_dropped_with_a_note(self) -> None:
        result = self.grep()
        self.assertIn("src/app.py:1", result)
        self.assertNotIn("hunter2", result)
        self.assertNotIn("id_rsa", result)
        self.assertNotIn("server.pem", result)
        self.assertIn("已跳过 3 个敏感文件", result)
        self.assertTrue(result.startswith("1 条匹配"), result)

    def test_only_sensitive_hits_reads_as_no_match(self) -> None:
        result = self.box.run("grep", {"pattern": "hunter2"})
        self.assertIn("没有匹配", result)
        self.assertIn("已跳过 1 个敏感文件", result)

    def test_sensitive_root_is_refused(self) -> None:
        for path in (".ssh", ".env"):
            result = self.grep(path=path)
            self.assertTrue(result.startswith("ERROR"), result)
            self.assertIn("敏感路径", result)
            self.assertNotIn("hunter2", result)

    def test_switch_off_lets_everything_through(self) -> None:
        self.config.search_sensitive = False
        result = self.grep()
        self.assertIn(".env:1", result)
        self.assertIn("hunter2", result)
        self.assertNotIn("已跳过", result)
        self.assertNotIn("ERROR", self.grep(path=".ssh"))


class GrepFilterWithoutBackendsTest(GrepFilterTest):
    """纯 Python 兜底走同一条过滤。"""

    def setUp(self) -> None:
        super().setUp()
        for target in ("xiaoyu.sandbox.shutil.which", "xiaoyu.tools._locate_grep"):
            patcher = mock.patch(target, return_value=None)
            patcher.start()
            self.addCleanup(patcher.stop)


class ListFilesFilterTest(SearchFilterTestCase):
    def test_sensitive_files_are_not_listed(self) -> None:
        result = self.box.run("list_files", {})
        listed = result.splitlines()
        self.assertIn("src/app.py", listed)
        for hidden in (".env", ".ssh/id_rsa", "certs/server.pem"):
            self.assertNotIn(hidden, listed)
        self.assertIn("已跳过 3 个敏感文件", result)

    def test_sensitive_root_is_refused(self) -> None:
        result = self.box.run("list_files", {"path": ".ssh"})
        self.assertTrue(result.startswith("ERROR"), result)
        self.assertIn("敏感路径", result)

    def test_switch_off_lists_them(self) -> None:
        self.config.search_sensitive = False
        result = self.box.run("list_files", {})
        self.assertIn(".env", result)
        self.assertIn(".ssh/id_rsa", result)
        self.assertNotIn("已跳过", result)


class SwitchWiringTest(unittest.TestCase):
    def test_layer_registered_and_env_switch(self) -> None:
        self.assertIn("search_sensitive", {layer.field for layer in guardrails.LAYERS})
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {"XIAOYU_SEARCH_SENSITIVE": "0"}):
                self.assertFalse(Config.from_env(workspace=Path(tmp)).search_sensitive)
            clean = {k: v for k, v in os.environ.items() if k != "XIAOYU_SEARCH_SENSITIVE"}
            with mock.patch.dict(os.environ, clean, clear=True):
                self.assertTrue(Config.from_env(workspace=Path(tmp)).search_sensitive)

    def test_unguarded_preset_turns_it_off(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config.from_env(workspace=Path(tmp), unguarded=True)
        self.assertFalse(cfg.search_sensitive)
        self.assertFalse(cfg.hardline)
