"""用户级配置（`xiaoyu config`）的测试。不打网络、不碰真实用户目录。"""

from __future__ import annotations

import contextlib
import io
import os
import pathlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import cli, config, providers


class _FakeWinPath(pathlib.PureWindowsPath):
    """POSIX 上不能实例化 WindowsPath，用 PureWindowsPath 桩验证 Windows 分支的拼路径逻辑。"""

    @classmethod
    def home(cls) -> "_FakeWinPath":
        return cls(r"C:\Users\u")


class _FakePosixPath(pathlib.PurePosixPath):
    """镜像桩：Windows runner 上不能实例化 PosixPath，posix 分支同样要用 Pure 桩。"""

    @classmethod
    def home(cls) -> "_FakePosixPath":
        return cls("/home/u")

    def expanduser(self) -> "_FakePosixPath":
        return self  # 测试路径不含 ~，原样返回即可


class UserConfigDirTest(unittest.TestCase):
    def test_posix_follows_xdg(self):
        with mock.patch.object(config.os, "name", "posix"), mock.patch.object(
            config, "Path", _FakePosixPath
        ), mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg"}):
            self.assertEqual(config.user_config_dir(), _FakePosixPath("/tmp/xdg/xiaoyu"))

    def test_posix_defaults_to_dot_config(self):
        with mock.patch.object(config.os, "name", "posix"), mock.patch.object(
            config, "Path", _FakePosixPath
        ), mock.patch.dict(os.environ, clear=False):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(
                config.user_config_dir(), _FakePosixPath("/home/u/.config/xiaoyu")
            )

    def test_windows_follows_appdata(self):
        with mock.patch.object(config.os, "name", "nt"), mock.patch.object(
            config, "Path", _FakeWinPath
        ), mock.patch.dict(os.environ, {"APPDATA": r"C:\Users\u\AppData\Roaming"}):
            self.assertEqual(
                config.user_config_dir(), _FakeWinPath(r"C:\Users\u\AppData\Roaming\xiaoyu")
            )

    def test_windows_without_appdata_falls_back(self):
        with mock.patch.object(config.os, "name", "nt"), mock.patch.object(
            config, "Path", _FakeWinPath
        ), mock.patch.dict(os.environ, clear=False):
            os.environ.pop("APPDATA", None)
            self.assertEqual(
                config.user_config_dir(), _FakeWinPath(r"C:\Users\u\AppData\Roaming\xiaoyu")
            )

    def test_windows_survives_undeterminable_home(self):
        """APPDATA 没了、home 也推不出（服务账户/被清空的 env——Windows 的
        Path.home() 只认环境变量）：必须退到临时目录，不能让 Agent 构造炸掉。
        CI 两个 Windows 格子红了三轮的根因之一。"""

        class _HomelessWinPath(_FakeWinPath):
            @classmethod
            def home(cls):
                raise RuntimeError("Could not determine home directory.")

        with mock.patch.object(config.os, "name", "nt"), mock.patch.object(
            config, "Path", _HomelessWinPath
        ), mock.patch.dict(os.environ, clear=False):
            os.environ.pop("APPDATA", None)
            self.assertEqual(
                config.user_config_dir(),
                _HomelessWinPath(tempfile.gettempdir()) / "xiaoyu",
            )

    def test_posix_survives_undeterminable_home(self):
        class _HomelessPosixPath(_FakePosixPath):
            @classmethod
            def home(cls):
                raise RuntimeError("Could not determine home directory.")

        with mock.patch.object(config.os, "name", "posix"), mock.patch.object(
            config, "Path", _HomelessPosixPath
        ), mock.patch.dict(os.environ, clear=False):
            os.environ.pop("XDG_CONFIG_HOME", None)
            self.assertEqual(
                config.user_config_dir(),
                _HomelessPosixPath(tempfile.gettempdir()) / "xiaoyu",
            )


class IsolatedConfigTest(unittest.TestCase):
    """配置目录、cwd、项目根全部指到临时目录：不碰真实 ~/.config，也不让仓库 .env 泄进来。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.confdir = Path(self.tmp.name) / "confhome"
        self.cwd = Path(self.tmp.name) / "cwd"
        self.cwd.mkdir()
        for patcher in (
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.confdir)}),
            #  直接替换 user_config_dir：XDG 只在 posix 分支生效，Windows 会绕到 APPDATA；
            #  patch os.name 也不行（跨平台实例化 Path 会炸），换函数最稳
            mock.patch.object(config, "user_config_dir", lambda: self.confdir / "xiaoyu"),
            mock.patch.object(config.Path, "cwd", return_value=self.cwd),
            mock.patch.object(config, "PROJECT_ROOT", Path(self.tmp.name) / "proot"),
            #  向导会按运行时逻辑探测直连 key，不桩掉会真的 shell out 去翻本机 Keychain
            mock.patch.object(config, "_read_from_keychain", lambda *a, **kw: None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        restore = mock.patch.dict(os.environ, clear=False)
        restore.start()
        self.addCleanup(restore.stop)
        for name in (
            *cli._CONFIG_VARS,
            "XIAOYU_API_KEY",
            "XIAOYU_ENV_FILE",
            #  所有内置直连厂商的键（含别名）都要清，开发机 shell 里导出的真 key
            #  会让向导"已检测到"分支莫名其妙地生效
            *(env for preset in providers.PRESETS.values() for env in preset.key_envs),
            "LITELLM_API_KEY",
        ):
            os.environ.pop(name, None)

    @property
    def env_path(self) -> Path:
        return self.confdir / "xiaoyu" / ".env"


class SaveUserEnvTest(IsolatedConfigTest):
    def test_creates_file_and_dirs(self):
        path = config.save_user_env({"XIAOYU_MODEL": "m1"})
        self.assertEqual(path, self.env_path)
        self.assertEqual(config._parse_dotenv(path), {"XIAOYU_MODEL": "m1"})

    def test_merge_keeps_untouched_keys(self):
        config.save_user_env({"XIAOYU_MODEL": "m1", "XIAOYU_API_KEY": "secret"})
        config.save_user_env({"XIAOYU_MODEL": "m2"})
        parsed = config._parse_dotenv(self.env_path)
        self.assertEqual(parsed["XIAOYU_MODEL"], "m2")
        self.assertEqual(parsed["XIAOYU_API_KEY"], "secret")

    @unittest.skipIf(os.name == "nt", "Windows 无 POSIX 权限位，依赖用户目录 ACL")
    def test_posix_permissions_owner_only(self):
        path = config.save_user_env({"XIAOYU_API_KEY": "secret"})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class LoadDotenvChainTest(IsolatedConfigTest):
    def test_user_env_is_loaded_as_fallback(self):
        config.save_user_env({"XIAOYU_MODEL": "from-user"})
        loaded = config.load_dotenv()
        self.assertIn(self.env_path.resolve(), loaded)
        self.assertEqual(os.environ["XIAOYU_MODEL"], "from-user")

    def test_cwd_env_beats_user_env(self):
        (self.cwd / ".env").write_text("XIAOYU_MODEL=from-cwd\n", encoding="utf-8")
        config.save_user_env({"XIAOYU_MODEL": "from-user", "XIAOYU_BASE_URL": "u"})
        config.load_dotenv()
        self.assertEqual(os.environ["XIAOYU_MODEL"], "from-cwd")
        #  cwd 没有的键仍从用户级补上
        self.assertEqual(os.environ["XIAOYU_BASE_URL"], "u")

    def test_explicit_file_is_exclusive(self):
        config.save_user_env({"XIAOYU_MODEL": "from-user"})
        explicit = Path(self.tmp.name) / "explicit.env"
        explicit.write_text("XIAOYU_BASE_URL=x\n", encoding="utf-8")
        loaded = config.load_dotenv(explicit)
        self.assertEqual(loaded, [explicit.resolve()])
        self.assertNotIn("XIAOYU_MODEL", os.environ)


class ConfigCommandTest(IsolatedConfigTest):
    def run_cmd(self, argv: list[str]) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.config_command(argv)
        return code, out.getvalue()

    def test_path_prints_user_env_path(self):
        code, out = self.run_cmd(["--path"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), str(self.env_path))

    def test_set_writes_pairs(self):
        code, _ = self.run_cmd(["--set", "XIAOYU_MODEL=m1", "--set", "XIAOYU_BASE_URL=https://x/v1"])
        self.assertEqual(code, 0)
        parsed = config._parse_dotenv(self.env_path)
        self.assertEqual(parsed, {"XIAOYU_MODEL": "m1", "XIAOYU_BASE_URL": "https://x/v1"})

    def test_set_rejects_malformed_pair(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code, _ = self.run_cmd(["--set", "no-equals-sign"])
        self.assertEqual(code, 2)
        self.assertFalse(self.env_path.exists())

    def test_show_never_prints_key(self):
        config.save_user_env({"XIAOYU_API_KEY": "sk-super-secret", "XIAOYU_MODEL": "m1"})
        code, out = self.run_cmd(["--show"])
        self.assertEqual(code, 0)
        self.assertNotIn("sk-super-secret", out)
        self.assertIn("已设置", out)
        self.assertIn("m1", out)

    def test_wizard_refuses_without_tty(self):
        err = io.StringIO()
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=False), contextlib.redirect_stderr(err):
            code, _ = self.run_cmd([])
        self.assertEqual(code, 2)
        self.assertIn("--set", err.getvalue())

    def test_wizard_key_hint_mentions_keychain_only_on_macos(self):
        prompts: list[str] = []

        def fake_input(prompt: str = "") -> str:
            prompts.append(prompt)
            #  直连 key 按 PRESETS 逐家问（全部留空），网关端点必须填否则会追问到死——
            #  按提示文案识别，不依赖问题序号（preset 数量变了测试不该跟着改）
            return "https://gw/v1" if "网关端点" in prompt else ""

        for platform, expect_keychain in (("win32", False), ("darwin", True), ("linux", False)):
            prompts.clear()
            with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), mock.patch(
                "builtins.input", side_effect=fake_input
            ), mock.patch.object(cli.sys, "platform", platform), contextlib.redirect_stdout(io.StringIO()):
                cli.config_wizard()
            key_prompt = prompts[-1]
            self.assertIn("API key", key_prompt)
            #  提示必须点名环境变量名，不能只说"环境变量"
            self.assertIn("XIAOYU_API_KEY", key_prompt)
            self.assertEqual("Keychain" in key_prompt, expect_keychain, msg=platform)

    def test_missing_key_error_uses_same_sources(self):
        for platform, expect_keychain in (("win32", False), ("darwin", True)):
            with mock.patch.object(config.sys, "platform", platform), mock.patch.object(
                config, "_read_from_keychain", return_value=None
            ):
                with self.assertRaises(config.MissingApiKey) as ctx:
                    config.load_api_key()
            message = str(ctx.exception)
            self.assertIn("xiaoyu config", message)
            self.assertIn("XIAOYU_API_KEY", message)
            self.assertEqual("Keychain" in message, expect_keychain, msg=platform)

    def test_wizard_writes_answers(self):
        #  依次：各家直连 key（全部回车=不用直连）、网关端点、主模型、摘要模型（回车=默认）、
        #        备用降级链（回车=不配）、网关 API key
        answers = iter(
            [""] * len(providers.PRESETS)
            + ["https://gw/v1", "main-model", "", "", "typed-key"]
        )
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), mock.patch(
            "builtins.input", side_effect=lambda *_: next(answers)
        ):
            code, out = self.run_cmd([])
        self.assertEqual(code, 0)
        parsed = config._parse_dotenv(self.env_path)
        self.assertEqual(parsed["XIAOYU_BASE_URL"], "https://gw/v1")
        self.assertEqual(parsed["XIAOYU_MODEL"], "main-model")
        #  摘要模型回车 = 用默认值
        self.assertEqual(parsed["XIAOYU_SUMMARY_MODEL"], config.DEFAULT_SUMMARY_MODEL)
        #  备用降级链留空 = 不写入
        self.assertNotIn("XIAOYU_FALLBACK_MODELS", parsed)
        self.assertEqual(parsed["XIAOYU_API_KEY"], "typed-key")
        #  程序自己的输出（提示语）里不包含 key；终端上看到的回显是终端行为，不经 stdout
        self.assertNotIn("typed-key", out)

    def test_wizard_accepts_direct_only_setup(self):
        """只填 DeepSeek 直连 key、不填网关——这是"开箱可用"这条路径的验收点。

        端点从此不是必填项，向导也不该再追着要。
        """
        #  依次：deepseek 直连 key、其余各家直连 key（回车=跳过）、
        #        网关端点（回车=不用）、主模型、摘要模型、备用链（都回车）
        answers = iter(
            ["sk-direct"]
            + [""] * (len(providers.PRESETS) - 1)
            + ["", "", "", ""]
        )
        with mock.patch.object(cli.sys.stdin, "isatty", return_value=True), mock.patch(
            "builtins.input", side_effect=lambda *_: next(answers)
        ):
            code, out = self.run_cmd([])
        self.assertEqual(code, 0)
        parsed = config._parse_dotenv(self.env_path)
        self.assertEqual(parsed["DEEPSEEK_API_KEY"], "sk-direct")
        self.assertNotIn("XIAOYU_BASE_URL", parsed, "没填网关就不该写空端点进配置")
        #  没有网关就不该再问网关 key（answers 也只准备了 5 个，多问会 StopIteration）
        self.assertNotIn("XIAOYU_API_KEY", parsed)
        self.assertNotIn("sk-direct", out, "程序自己的输出里不许出现 key")

    def test_wizard_detects_existing_direct_key(self):
        """已有直连 key（环境变量或 Keychain）时：先报来源，回车沿用、不逼填网关。"""
        for source_env, expected_source in (
            ({"DEEPSEEK_API_KEY": "sk-old"}, "环境变量"),
            ({}, "Keychain"),
        ):
            with self.subTest(expected_source):
                patchers = [mock.patch.dict(os.environ, source_env)]
                if not source_env:
                    patchers.append(
                        mock.patch.object(
                            config, "_read_from_keychain", lambda *a, **kw: "sk-old"
                        )
                    )
                prompts: list[str] = []

                def fake_input(prompt=""):
                    prompts.append(prompt)
                    return ""  # 全部回车：沿用直连 key、不配网关、模型用默认

                with contextlib.ExitStack() as stack:
                    for patcher in patchers:
                        stack.enter_context(patcher)
                    stack.enter_context(
                        mock.patch.object(cli.sys.stdin, "isatty", return_value=True)
                    )
                    stack.enter_context(mock.patch("builtins.input", fake_input))
                    code, out = self.run_cmd([])
                self.assertEqual(code, 0)
                self.assertIn("已检测到 deepseek 直连 key", out)
                self.assertIn(expected_source, out)
                self.assertNotIn("sk-old", out, "探测提示不许回显 key")
                self.assertIn("沿用现有", prompts[0], "已有 key 时提示语应是沿用而非新填")
                parsed = config._parse_dotenv(self.env_path)
                self.assertNotIn("DEEPSEEK_API_KEY", parsed, "回车沿用就不该往 .env 里写")
                self.assertNotIn("XIAOYU_BASE_URL", parsed, "有直连时网关留空不该被追问")

    def test_main_routes_config_subcommand(self):
        with mock.patch.object(cli, "config_command", return_value=0) as fake:
            code = cli.main(["config", "--path"])
        self.assertEqual(code, 0)
        fake.assert_called_once_with(["--path"])


class ContextWindowTest(unittest.TestCase):
    """上下文上限跟随当前模型查表：显式覆写 > 已知模型表 > 保守兜底。"""

    @staticmethod
    def _config(model: str) -> config.Config:
        return config.Config(base_url="", model=model, workspace=Path.cwd())

    def test_known_model_gets_table_window(self) -> None:
        self.assertEqual(self._config("deepseek-v4-flash").context_limit, 1_000_000)
        self.assertEqual(self._config("deepseek-v4-pro").context_limit, 1_000_000)
        self.assertEqual(self._config("glm-5.3").context_limit, 1_000_000)
        self.assertEqual(self._config("kimi-k3").context_limit, 1_048_576)
        self.assertEqual(self._config("claude-fable-5").context_limit, 1_000_000)
        self.assertEqual(self._config("qwen3.8-max").context_limit, 1_000_000)

    def test_qualified_name_strips_provider_prefix(self) -> None:
        self.assertEqual(self._config("gateway/deepseek-v4-pro").context_limit, 1_000_000)

    def test_unknown_model_falls_back(self) -> None:
        self.assertEqual(
            self._config("unlisted-model").context_limit,
            config.FALLBACK_CONTEXT_LIMIT,
        )

    def test_limit_follows_model_switch(self) -> None:
        """/model 切换、粘性降级都只改 config.model，上限必须跟着走。"""
        cfg = self._config("deepseek-v4-pro")
        cfg.model = "unlisted-model"
        self.assertEqual(cfg.context_limit, config.FALLBACK_CONTEXT_LIMIT)

    def test_explicit_override_beats_table(self) -> None:
        """XIAOYU_CONTEXT_LIMIT / 直接赋值是一刀切：换模型也不回落查表。"""
        cfg = self._config("deepseek-v4-pro")
        cfg.context_limit = 50_000
        self.assertEqual(cfg.context_limit, 50_000)
        cfg.model = "unlisted-model"
        self.assertEqual(cfg.context_limit, 50_000)

    def test_env_override_via_from_env(self) -> None:
        with mock.patch.dict(os.environ, {"XIAOYU_CONTEXT_LIMIT": "70000"}, clear=True):
            cfg = config.Config.from_env(workspace=Path.cwd())
        self.assertEqual(cfg.context_limit, 70_000)


class ExploreIterationsEnvTest(unittest.TestCase):
    """explore 子 agent 的轮次上限（默认 12）要能临时放宽——大仓库上 12 轮不够用。"""

    def _from_env(self, value: str) -> config.Config:
        with mock.patch.dict(os.environ, {"XIAOYU_EXPLORE_ITERATIONS": value}):
            return config.Config.from_env(workspace=Path.cwd())

    def test_default_is_twelve(self) -> None:
        cfg = config.Config(base_url="x", model="x", workspace=Path.cwd())
        self.assertEqual(cfg.explore_iterations, 12)

    def test_env_override(self) -> None:
        self.assertEqual(self._from_env("40").explore_iterations, 40)

    def test_clamped_and_garbage_ignored(self) -> None:
        self.assertEqual(self._from_env("999").explore_iterations, 100)
        self.assertEqual(self._from_env("0").explore_iterations, 1)
        self.assertEqual(self._from_env("很多").explore_iterations, 12)


class VersionFlagTest(unittest.TestCase):
    def test_version_prints_and_exits_zero(self):
        import xiaoyu

        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            cli.main(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertEqual(out.getvalue().strip(), f"xiaoyu {xiaoyu.__version__}")


class ModeEnvTest(unittest.TestCase):
    """XIAOYU_MODE：个人默认交互模式（写进用户级 .env 即永久生效）。
    命令行 --mode 经 overrides 必须压过它；写错的值由 Agent 侧 modes.get
    归一回默认档，这里只保证原样透传不校验。"""

    def test_env_sets_personal_default(self) -> None:
        with mock.patch.dict(os.environ, {"XIAOYU_MODE": "auto"}):
            self.assertEqual(config.Config.from_env(workspace=Path.cwd()).mode, "auto")

    def test_cli_override_beats_env(self) -> None:
        with mock.patch.dict(os.environ, {"XIAOYU_MODE": "auto"}):
            cfg = config.Config.from_env(workspace=Path.cwd(), mode="plan")
        self.assertEqual(cfg.mode, "plan")

    def test_blank_env_keeps_builtin_default(self) -> None:
        with mock.patch.dict(os.environ, {"XIAOYU_MODE": "  "}):
            self.assertEqual(config.Config.from_env(workspace=Path.cwd()).mode, "default")


if __name__ == "__main__":
    unittest.main()
