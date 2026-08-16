"""folder trust 门的测试：六条优先级、过宽根、信任表级联、探测清单、消费点。

不打网络；信任表落在 tests/__init__.py 隔离出的配置目录里。
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import folder_trust as ft
from xiaoyu import mcp as mcp_mod
from xiaoyu.config import load_dotenv
from xiaoyu.permissions import Permissions, workspace_rules_path


class DecideTest(unittest.TestCase):
    """六条优先级的纯函数测试。顺序即语义，任何一条换位都该在这里炸。"""

    def test_feature_off_trusts_everything(self):
        self.assertEqual(
            ft.decide(
                feature_enabled=False,
                store_trusted=False,
                key_recordable=True,
                configs_present=True,
                interactive=True,
            ),
            "trusted",
        )

    def test_store_trusted_wins(self):
        self.assertEqual(
            ft.decide(
                feature_enabled=True,
                store_trusted=True,
                key_recordable=True,
                configs_present=True,
                interactive=True,
            ),
            "trusted",
        )

    def test_unrecordable_key_trusted_even_with_configs_and_interactive(self):
        #  锁死判定次序：不可记录排在"有没有配置""交不交互"之前——
        #  这个键永远存不进信任表，在此拦截就是每次启动都问同一个问题
        self.assertEqual(
            ft.decide(
                feature_enabled=True,
                store_trusted=None,
                key_recordable=False,
                configs_present=True,
                interactive=True,
            ),
            "trusted",
        )

    def test_no_configs_trusted(self):
        self.assertEqual(
            ft.decide(
                feature_enabled=True,
                store_trusted=None,
                key_recordable=True,
                configs_present=False,
                interactive=True,
            ),
            "trusted",
        )

    def test_recorded_untrust_skips_prompt(self):
        #  用户表过态就不再骚扰：记录过"不信任"直接不信任，交互与否无关
        self.assertEqual(
            ft.decide(
                feature_enabled=True,
                store_trusted=False,
                key_recordable=True,
                configs_present=True,
                interactive=True,
            ),
            "untrusted",
        )

    def test_interactive_prompts(self):
        self.assertEqual(
            ft.decide(
                feature_enabled=True,
                store_trusted=None,
                key_recordable=True,
                configs_present=True,
                interactive=True,
            ),
            "prompt",
        )

    def test_headless_untrusted(self):
        self.assertEqual(
            ft.decide(
                feature_enabled=True,
                store_trusted=None,
                key_recordable=True,
                configs_present=True,
                interactive=False,
            ),
            "untrusted",
        )


class UnsafeRootTest(unittest.TestCase):
    def test_relative_and_fs_root_are_unsafe(self):
        self.assertTrue(ft.unsafe_trust_root(Path("relative/path")))
        self.assertTrue(ft.unsafe_trust_root(Path("/")))

    def test_home_is_unsafe(self):
        home = ft.home_dir()
        if home is None:
            self.skipTest("环境推不出 home")
        self.assertTrue(ft.unsafe_trust_root(home))

    def test_normal_dir_is_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertFalse(ft.unsafe_trust_root(Path(tmp).resolve()))


class WorkspaceKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def test_git_root_wins(self):
        (self.root / ".git").mkdir()
        sub = self.root / "a" / "b"
        sub.mkdir(parents=True)
        self.assertEqual(ft.workspace_key(sub), self.root)

    def test_no_git_uses_workspace(self):
        sub = self.root / "plain"
        sub.mkdir()
        self.assertEqual(ft.workspace_key(sub), sub)

    def test_overbroad_git_root_falls_back_to_workspace(self):
        #  dotfiles-in-home 场景：家目录本身是 git 仓库，键要收窄回工作区
        (self.root / ".git").mkdir()
        sub = self.root / "project"
        sub.mkdir()
        with mock.patch.object(ft, "home_dir", lambda: self.root):
            self.assertEqual(ft.workspace_key(sub), sub)


class RepoConfigKindsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def test_clean_workspace_has_no_kinds(self):
        self.assertEqual(ft.repo_config_kinds(self.workspace), [])

    def test_mcp_json_detected(self):
        (self.workspace / ".mcp.json").write_text("{}", encoding="utf-8")
        self.assertEqual(ft.repo_config_kinds(self.workspace), ["mcp"])

    def test_comment_only_permissions_not_detected(self):
        path = self.workspace / ".xiaoyu" / "permissions.txt"
        path.parent.mkdir()
        path.write_text("# 只有注释\n\n", encoding="utf-8")
        self.assertEqual(ft.repo_config_kinds(self.workspace), [])
        path.write_text("allow bash(git *)\n", encoding="utf-8")
        self.assertEqual(ft.repo_config_kinds(self.workspace), ["permission"])

    def test_env_detected(self):
        (self.workspace / ".env").write_text("XIAOYU_BASE_URL=http://evil\n", encoding="utf-8")
        self.assertEqual(ft.repo_config_kinds(self.workspace), ["env"])


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        store = self.root / "trusted_folders.json"
        patcher = mock.patch.object(ft, "trust_store_path", lambda: store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_record_and_cascade(self):
        key = self.root / "repo"
        key.mkdir()
        self.assertIsNotNone(ft.record_decision(key, True))
        self.assertIs(ft.stored_verdict(key), True)
        #  信任向子目录级联
        self.assertIs(ft.stored_verdict(key / "sub" / "deep"), True)
        #  兄弟目录不沾光
        self.assertIsNone(ft.stored_verdict(self.root / "other"))

    def test_most_specific_record_wins(self):
        parent = self.root / "repo"
        child = parent / "vendor"
        ft.record_decision(parent, True)
        ft.record_decision(child, False)
        self.assertIs(ft.stored_verdict(child), False)
        self.assertIs(ft.stored_verdict(parent), True)

    def test_unsafe_keys_rejected_on_write_and_skipped_on_read(self):
        self.assertIsNone(ft.record_decision(Path("/"), True))
        #  手改文件塞进过宽根的记录，读取时也不认——造不出全局放行
        from xiaoyu.mcp_guard import save_json_atomic

        save_json_atomic(
            ft.trust_store_path(), {"folders": {"/": {"trusted": True}}}
        )
        self.assertIsNone(ft.stored_verdict(self.root / "anything"))

    def test_corrupt_store_is_empty(self):
        ft.trust_store_path().write_text("not json", encoding="utf-8")
        self.assertIsNone(ft.stored_verdict(self.root))


class EnabledFlagTest(unittest.TestCase):
    def test_default_on_and_env_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(ft.ENABLE_ENV, None)
            self.assertTrue(ft.enabled())
        with mock.patch.dict(os.environ, {ft.ENABLE_ENV: "0"}):
            self.assertFalse(ft.enabled())


class EvaluateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve() / "repo"
        self.workspace.mkdir()
        (self.workspace / ".mcp.json").write_text("{}", encoding="utf-8")
        store = Path(self.tmp.name) / "trusted_folders.json"
        patcher = mock.patch.object(ft, "trust_store_path", lambda: store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_prompt_then_recorded(self):
        decision = ft.evaluate(self.workspace, interactive=True)
        self.assertEqual(decision.verdict, "prompt")
        self.assertEqual(decision.kinds, ("mcp",))
        ft.record_decision(decision.key, True)
        self.assertEqual(ft.evaluate(self.workspace, interactive=True).verdict, "trusted")

    def test_headless_untrusted(self):
        self.assertEqual(ft.evaluate(self.workspace, interactive=False).verdict, "untrusted")

    def test_clean_workspace_trusted_without_record(self):
        clean = Path(self.tmp.name) / "clean"
        clean.mkdir()
        decision = ft.evaluate(clean, interactive=False)
        self.assertEqual(decision.verdict, "trusted")


class AskUserTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = Path(self.tmp.name) / "trusted_folders.json"
        patcher = mock.patch.object(ft, "trust_store_path", lambda: store)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = Path(self.tmp.name).resolve() / "repo"
        self.key.mkdir()
        self.decision = ft.TrustDecision("prompt", self.key, ("mcp",))

    def _ask(self, answer: str) -> bool:
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch("builtins.input", return_value=answer):
                return ft.ask_user(self.decision)

    def test_yes_records_trust(self):
        self.assertTrue(self._ask("y"))
        self.assertIs(ft.stored_verdict(self.key), True)

    def test_anything_else_records_untrust(self):
        self.assertFalse(self._ask(""))
        self.assertIs(ft.stored_verdict(self.key), False)

    def test_eof_is_no(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with mock.patch("builtins.input", side_effect=EOFError):
                self.assertFalse(ft.ask_user(self.decision))


class ConsumersTest(unittest.TestCase):
    """探测清单与消费点必须对齐：这里逐个验证"不信任时真的被跳过"。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()

    def test_mcp_project_scope_skipped(self):
        (self.workspace / ".mcp.json").write_text(
            '{"mcpServers": {"x": {"command": "echo"}}}', encoding="utf-8"
        )
        with contextlib.redirect_stderr(io.StringIO()):
            trusted = mcp_mod.load_server_specs(self.workspace)
            untrusted = mcp_mod.load_server_specs(self.workspace, include_project=False)
        self.assertEqual([spec.name for spec in trusted], ["x"])
        self.assertEqual(untrusted, [])

    def test_workspace_permissions_skipped(self):
        path = workspace_rules_path(self.workspace)
        path.parent.mkdir()
        path.write_text("allow bash(git *)\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()):
            trusted = Permissions.load(self.workspace)
            untrusted = Permissions.load(self.workspace, include_workspace=False)
        self.assertEqual(trusted.decide("bash", {"command": "git status"}), "allow")
        self.assertEqual(untrusted.decide("bash", {"command": "git status"}), "ask")

    def test_dotenv_untrusted_workspace_skipped(self):
        (self.workspace / ".env").write_text("XIAOYU_FT_PROBE=evil\n", encoding="utf-8")
        original = Path.cwd()
        os.chdir(self.workspace)
        self.addCleanup(os.chdir, original)
        os.environ.pop("XIAOYU_FT_PROBE", None)
        self.addCleanup(os.environ.pop, "XIAOYU_FT_PROBE", None)
        loaded = load_dotenv(untrusted_dir=self.workspace)
        self.assertNotIn(self.workspace / ".env", loaded)
        self.assertIsNone(os.environ.get("XIAOYU_FT_PROBE"))
        #  信任后照常加载
        loaded = load_dotenv()
        self.assertIn(self.workspace / ".env", loaded)
        self.assertEqual(os.environ.get("XIAOYU_FT_PROBE"), "evil")

    def test_dotenv_explicit_file_not_gated(self):
        env_file = self.workspace / ".env"
        env_file.write_text("XIAOYU_FT_PROBE2=ok\n", encoding="utf-8")
        os.environ.pop("XIAOYU_FT_PROBE2", None)
        self.addCleanup(os.environ.pop, "XIAOYU_FT_PROBE2", None)
        loaded = load_dotenv(env_file, untrusted_dir=self.workspace)
        self.assertIn(env_file, loaded)
        self.assertEqual(os.environ.get("XIAOYU_FT_PROBE2"), "ok")


if __name__ == "__main__":
    unittest.main()
