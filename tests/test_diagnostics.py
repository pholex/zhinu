"""自诊断：计量器自注册与配对、进程快照、doctor 各项判定与汇总。"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from xiaoyu import diagnostics


class GaugeTest(unittest.TestCase):
    def setUp(self) -> None:
        diagnostics._reset_registry_for_tests()
        self.addCleanup(diagnostics._reset_registry_for_tests)

    def test_registers_on_first_use_not_on_declaration(self) -> None:
        gauge = diagnostics.Gauge("t.lazy")
        self.assertNotIn("t.lazy", diagnostics.snapshot())
        gauge.inc()
        self.assertEqual(diagnostics.snapshot()["t.lazy"], 1)

    def test_never_goes_negative(self) -> None:
        gauge = diagnostics.Gauge("t.floor")
        gauge.dec()
        gauge.dec()
        self.assertEqual(gauge.value, 0)
        gauge.set(-5)
        self.assertEqual(gauge.value, 0)

    def test_track_decrements_on_exception(self) -> None:
        gauge = diagnostics.Gauge("t.track")
        with self.assertRaises(RuntimeError):
            with gauge.track():
                self.assertEqual(gauge.value, 1)
                raise RuntimeError("boom")
        self.assertEqual(gauge.value, 0)

    def test_snapshot_sorted_and_latest_instance_wins(self) -> None:
        diagnostics.Gauge("t.b").inc(2)
        diagnostics.Gauge("t.a").inc()
        again = diagnostics.Gauge("t.b")
        again.inc(7)
        self.assertEqual(list(diagnostics.snapshot()), ["t.a", "t.b"])
        self.assertEqual(diagnostics.snapshot()["t.b"], 7)

    def test_process_stats_shape(self) -> None:
        stats = diagnostics.process_stats()
        self.assertEqual(stats["pid"], os.getpid())
        self.assertGreaterEqual(stats["threads"], 1)
        self.assertIn("rss_bytes", stats)
        report = diagnostics.report()
        self.assertEqual(set(report), {"version", "process", "gauges"})


class DoctorChecksTest(unittest.TestCase):
    def test_disk_thresholds(self) -> None:
        paths = {"a": Path("/x"), "b": Path("/y")}
        ok = diagnostics.check_disk(paths, measure=lambda _p: 10 * diagnostics.GIB)
        self.assertEqual(ok.status, "ok")
        warn = diagnostics.check_disk(paths, measure=lambda _p: 2 * diagnostics.GIB)
        self.assertEqual(warn.status, "warn")
        self.assertIn("2.0 GiB", warn.summary)
        fail = diagnostics.check_disk(paths, measure=lambda _p: 100 * 1024 * 1024)
        self.assertEqual(fail.status, "fail")
        self.assertTrue(fail.remedy)
        unknown = diagnostics.check_disk(paths, measure=lambda _p: None)
        self.assertEqual(unknown.status, "warn")
        self.assertIn("未能完整测量", unknown.summary)

    def test_config_dir_unwritable_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cfg"
            target.mkdir()
            with mock.patch.object(Path, "write_text", side_effect=PermissionError("ro")):
                check = diagnostics.check_config_dir(target)
        self.assertEqual(check.status, "fail")
        self.assertIn("不可写", check.summary)

    def test_providers_never_echo_values(self) -> None:
        env = {"DEEPSEEK_API_KEY": "sk-SECRET-VALUE", "XIAOYU_BASE_URL": "", "XIAOYU_API_KEY": ""}
        with mock.patch.dict(os.environ, env, clear=False), mock.patch(
            "xiaoyu.config._read_from_keychain", return_value=None
        ):
            check = diagnostics.check_providers()
        self.assertEqual(check.status, "ok")
        dumped = json.dumps(check.to_dict(), ensure_ascii=False)
        self.assertNotIn("SECRET", dumped)
        self.assertIn("deepseek", dumped)

    def test_providers_none_configured_fails(self) -> None:
        from xiaoyu.providers import PRESETS

        names = {name: "" for preset in PRESETS.values() for name in preset.key_envs}
        names.update({"XIAOYU_BASE_URL": "", "XIAOYU_API_KEY": "", "LITELLM_API_KEY": ""})
        with mock.patch.dict(os.environ, names, clear=False), mock.patch(
            "xiaoyu.config._read_from_keychain", return_value=None
        ):
            check = diagnostics.check_providers()
        self.assertEqual(check.status, "fail")

    def test_mcp_config_broken_json_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / ".mcp.json").write_text("{not json", encoding="utf-8")
            with mock.patch("xiaoyu.mcp.config_paths", return_value=[ws / ".mcp.json"]):
                check = diagnostics.check_mcp_config(ws)
        self.assertEqual(check.status, "fail")

    def test_sessions_counts_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "sessions"
            (root / "ws").mkdir(parents=True)
            (root / "ws" / "a.jsonl").write_text("x" * 10, encoding="utf-8")
            check = diagnostics.check_sessions(root)
        self.assertEqual(check.status, "ok")
        self.assertIn("1 个文件", check.details[0])

    def test_overall_and_render(self) -> None:
        checks = [
            diagnostics.Check("a", "ok", "fine"),
            diagnostics.Check("b", "warn", "meh", ["d1"], remedy="fix b"),
        ]
        self.assertEqual(diagnostics.overall(checks), "warn")
        lines = diagnostics.render(checks)
        self.assertTrue(lines[0].startswith("OK "))
        self.assertTrue(lines[1].startswith("WARN"))
        self.assertIn("→ fix b", lines[-1])
        payload = json.loads(diagnostics.to_json(checks))
        self.assertEqual(payload["status"], "warn")
        self.assertEqual(len(payload["checks"]), 2)


class MinPythonMatchesPackageTest(unittest.TestCase):
    def test_doctor_floor_equals_requires_python(self) -> None:
        """doctor 的版本下限与 pyproject 的 requires-python 是同一个数，改一处必须改另一处。"""
        import tomllib

        root = Path(__file__).resolve().parents[1]
        with (root / "pyproject.toml").open("rb") as handle:
            spec = tomllib.load(handle)["project"]["requires-python"]
        self.assertEqual(spec, ">=" + ".".join(map(str, diagnostics.MIN_PYTHON)))


class RemediesPointSomewhereRealTest(unittest.TestCase):
    """doctor 给的出路得真的走得通。"""

    def test_linux_sandbox_tells_missing_from_blocked(self) -> None:
        from xiaoyu import sandbox

        with mock.patch.object(sandbox, "available", return_value=False), mock.patch.object(
            sandbox.sys, "platform", "linux"
        ):
            with mock.patch.object(sandbox, "_bwrap_path", return_value=None):
                why, remedy = sandbox.unavailable_reason()
                self.assertIn("没有安装", why)
                self.assertIn("install bubblewrap", remedy)
            with mock.patch.object(sandbox, "_bwrap_path", return_value="/usr/bin/bwrap"):
                why, remedy = sandbox.unavailable_reason()
                self.assertIn("跑不起来", why)
                self.assertIn("AppArmor", remedy)
                self.assertIn("重装 bubblewrap 没有用", remedy)

    def test_available_sandbox_has_no_reason(self) -> None:
        from xiaoyu import sandbox

        with mock.patch.object(sandbox, "available", return_value=True):
            self.assertEqual(sandbox.unavailable_reason(), ("", ""))

    def test_mcp_server_whose_command_is_missing_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / ".mcp.json").write_text(
                json.dumps({"mcpServers": {
                    "ghost": {"command": "xiaoyu-no-such-mcp-binary"},
                    "here": {"command": sys.executable},
                    "remote": {"url": "https://mcp.example.com/mcp"},
                    "off": {"command": "also-missing-but-disabled", "disabled": True},
                }}),
                encoding="utf-8",
            )
            with mock.patch("xiaoyu.mcp.user_config_dir", lambda: workspace / "userconf"):
                check = diagnostics.check_mcp_config(workspace)
        self.assertEqual(check.status, "warn")
        flagged = [line for line in check.details if "找不到" in line]
        self.assertEqual(len(flagged), 1)
        self.assertIn("ghost", flagged[0])

    def _fake_dist(self, version: str, files: dict[str, str]) -> mock.Mock:
        dist = mock.Mock()
        dist.version = version
        dist.read_text.side_effect = files.get
        dist._path = "/site-packages/xiaoyu_agent.dist-info"
        return dist

    def test_install_reports_version_and_form(self) -> None:
        import xiaoyu

        dist = self._fake_dist(xiaoyu.__version__, {"INSTALLER": "pip\n"})
        with mock.patch("importlib.metadata.distribution", return_value=dist), \
                mock.patch.object(sys, "prefix", "/opt/venv"):
            check = diagnostics.check_install()
        self.assertEqual(check.status, "ok")
        self.assertIn(xiaoyu.__version__, check.summary)
        self.assertIn("pip", check.summary)
        self.assertTrue(any("代码位置" in line for line in check.details))

    def test_install_flags_record_that_lags_behind_the_code(self) -> None:
        import xiaoyu

        editable = json.dumps({"url": "file:///src", "dir_info": {"editable": True}})
        dist = self._fake_dist("0.0.1", {"direct_url.json": editable})
        with mock.patch("importlib.metadata.distribution", return_value=dist):
            check = diagnostics.check_install()
        self.assertEqual(check.status, "warn")
        self.assertIn("0.0.1", check.summary)
        self.assertIn(xiaoyu.__version__, check.summary)
        self.assertIn("pip install -e", check.remedy)

    def test_install_without_record_is_not_a_problem(self) -> None:
        from importlib import metadata

        with mock.patch(
            "importlib.metadata.distribution", side_effect=metadata.PackageNotFoundError("x")
        ):
            check = diagnostics.check_install()
        self.assertEqual(check.status, "ok")
        self.assertIn("源码目录", check.summary)

    def test_install_form_recognises_tool_managers(self) -> None:
        dist = self._fake_dist("1.0", {"INSTALLER": "uv"})
        for prefix, expected in (
            ("/home/u/.local/share/pipx/venvs/xiaoyu-agent", "pipx"),
            ("/home/u/.local/share/uv/tools/xiaoyu-agent", "uv tool"),
            ("C:\\Users\\u\\pipx\\venvs\\xiaoyu-agent", "pipx"),
            ("/opt/venv", "uv"),
        ):
            with self.subTest(prefix=prefix), mock.patch.object(sys, "prefix", prefix):
                self.assertEqual(diagnostics.install_form(dist), expected)

    def test_mcp_command_is_found_through_declared_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            bin_dir = workspace / "bin"
            bin_dir.mkdir()
            name = "xiaoyu-fake-launcher" + (".cmd" if os.name == "nt" else "")
            launcher = bin_dir / name
            launcher.write_text("@echo off\n" if os.name == "nt" else "#!/bin/sh\n", encoding="utf-8")
            launcher.chmod(0o755)
            (workspace / ".mcp.json").write_text(
                json.dumps({"mcpServers": {
                    "declared": {"command": name, "env": {"PATH": str(bin_dir)}},
                }}),
                encoding="utf-8",
            )
            with mock.patch("xiaoyu.mcp.user_config_dir", lambda: workspace / "userconf"):
                check = diagnostics.check_mcp_config(workspace)
        self.assertEqual(check.status, "ok", check.details)

    def test_mcp_config_pointing_at_missing_paths_is_flagged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            gone = workspace / "moved-away"
            (workspace / ".mcp.json").write_text(
                json.dumps({"mcpServers": {
                    "stale": {
                        "command": sys.executable,
                        "args": [str(gone / "server.py"), "--root", str(workspace), "-x"],
                        "env": {
                            "DATA_DIR": str(gone / "data"),
                            #  以 / 开头但不是文件：键名不像路径就不查
                            "API_PREFIX": "/v1",
                            #  占位符没兑现是另一类问题；多目录列表里缺一两个属正常
                            "CERT_FILE": "${XIAOYU_TEST_UNSET}/ca.pem",
                            "PATH": os.pathsep.join([str(gone / "a"), str(gone / "b")]),
                        },
                    },
                }}),
                encoding="utf-8",
            )
            with mock.patch("xiaoyu.mcp.user_config_dir", lambda: workspace / "userconf"):
                check = diagnostics.check_mcp_config(workspace)
        self.assertEqual(check.status, "warn")
        flagged = [line for line in check.details if "不存在的路径" in line]
        self.assertEqual(len(flagged), 2, flagged)
        self.assertTrue(any("server.py" in line for line in flagged))
        self.assertTrue(any("data" in line for line in flagged))

    def test_oversized_sessions_remedy_names_no_missing_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            (sessions / "a.jsonl").write_text("x", encoding="utf-8")
            #  把"偏大"的门槛压到 1 字节以下：不必真造 2GB 的文件
            with mock.patch.object(diagnostics, "GIB", 0.25):
                check = diagnostics.check_sessions(sessions)
        self.assertEqual(check.status, "warn")
        self.assertNotIn("清理旧会话", check.remedy)
        self.assertIn("直接删", check.remedy)


class DoctorCommandTest(unittest.TestCase):
    def test_runs_in_isolated_home_and_exits_by_status(self) -> None:
        from xiaoyu.cli import doctor_command

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HOME": tmp, "USERPROFILE": tmp,
                "XDG_CONFIG_HOME": str(Path(tmp) / "config"), "APPDATA": str(Path(tmp) / "config"),
            }
            #  Keychain 不归 HOME 管：不挡住的话这条用例会去读开发机的真钥匙串，
            #  结果随机器而变
            with mock.patch.dict(os.environ, env, clear=False), mock.patch(
                "xiaoyu.config._read_from_keychain", return_value=None
            ) as keychain:
                out = io.StringIO()
                with redirect_stdout(out):
                    code = doctor_command(["--json", "--workspace", tmp])
            self.assertTrue(keychain.called, "前提：doctor 确实会去查钥匙串")
        payload = json.loads(out.getvalue())
        ids = [check["id"] for check in payload["checks"]]
        self.assertEqual(
            ids,
            ["install", "python", "config_dir", "disk", "providers", "env", "proxy", "sandbox",
             "bash_parser", "tools", "shell", "mcp_config", "sessions"],
        )
        self.assertEqual(code, 1 if payload["status"] == "fail" else 0)
        self.assertIn("gauges", payload["diagnostics"])



#  假凭据按运行期拼出来：字面量会撞提交前的敏感词检查，而且本来就不该有一个"长得像真的"的 key 躺在仓库里
FAKE_KEY = "sk-" + "a" * 24


class ProbeTest(unittest.TestCase):
    """--probe：对默认模型真发一条请求。这里用假 registry，不出网。"""

    def _route(self, client):
        import types

        return types.SimpleNamespace(provider="fake", model="m", qualified="fake/m", client=client)

    def _run(self, client):
        import types

        registry = types.SimpleNamespace(resolve=lambda name: self._route(client))
        config = types.SimpleNamespace(model="m")
        with mock.patch("xiaoyu.providers.build", return_value=registry):
            return diagnostics.probe_model(config)

    def test_ok_reports_latency_and_reply(self) -> None:
        import types

        message = types.SimpleNamespace(content="ok")
        response = types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=message)],
            usage=types.SimpleNamespace(prompt_tokens=5, completion_tokens=1),
        )
        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(
                completions=types.SimpleNamespace(create=lambda **kw: response)
            )
        )
        check = self._run(client)
        self.assertEqual(check.status, "ok")
        self.assertIn("fake/m 应答", check.summary)
        self.assertIn("ms", check.summary)
        self.assertIn("回复：ok", check.details)

    def test_failure_is_classified_not_raised(self) -> None:
        import types

        import httpx2
        import openai

        def boom(**kw):
            request = httpx2.Request("POST", "http://unused")
            response = httpx2.Response(401, request=request)
            raise openai.AuthenticationError("bad key " + FAKE_KEY, response=response, body=None)

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=boom))
        )
        check = self._run(client)
        self.assertEqual(check.status, "fail")
        self.assertIn("auth", check.summary)
        self.assertTrue(check.remedy)
        #  报错正文里的凭据不进体检输出
        self.assertNotIn(FAKE_KEY, "\n".join(check.details))

    def test_missing_config_is_a_check_not_a_traceback(self) -> None:
        from xiaoyu.config import MissingConfig

        with mock.patch("xiaoyu.providers.build", side_effect=MissingConfig("没有 key")):
            check = diagnostics.probe_model(object())
        self.assertEqual(check.status, "fail")
        self.assertIn("provider", check.summary)


class BundleTest(unittest.TestCase):
    """--bundle：诊断包的脱敏、尾部截取、symlink 拒绝、0600。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_provider_headers_keep_only_names(self) -> None:
        """自定义 header 的值多半是令牌：诊断包里只留名字。"""
        got = diagnostics.redact_value(
            "XIAOYU_PROVIDER_RELAY_HEADERS", "Authorization=Bearer top-secret;X-Title=xiaoyu"
        )
        self.assertNotIn("top-secret", got)
        self.assertIn("Authorization", got)
        self.assertIn("X-Title", got)

    def test_redact_value_by_name_and_by_pattern(self) -> None:
        self.assertEqual(diagnostics.redact_value("XIAOYU_API_KEY", "abc"), "[REDACTED]")
        self.assertEqual(diagnostics.redact_value("XIAOYU_SERVE_TOKEN", "abc"), "[REDACTED]")
        self.assertEqual(diagnostics.redact_value("XIAOYU_MODEL", "deepseek-flash"), "deepseek-flash")
        self.assertNotIn(
            FAKE_KEY,
            diagnostics.redact_value("XIAOYU_BASE_URL", "https://x/?k=" + FAKE_KEY),
        )

    def test_tail_lines_respects_both_limits(self) -> None:
        path = self.root / "log.jsonl"
        path.write_text("".join(f"line {i}\n" for i in range(1000)), encoding="utf-8")
        self.assertEqual(diagnostics.tail_lines(path, 3, 10_000), ["line 997", "line 998", "line 999"])
        #  字节上限切在行中间：半截首行丢掉，剩下的都是完整行
        lines = diagnostics.tail_lines(path, 1000, 50)
        self.assertTrue(all(line.startswith("line ") for line in lines))
        self.assertEqual(lines[-1], "line 999")

    def test_bundle_redacts_secrets_and_refuses_symlink(self) -> None:
        session = self.root / "20260101-000000-1.jsonl"
        session.write_text(
            json.dumps({"event": "meta", "model": "m", "workspace": "/ws"}) + "\n"
            + json.dumps({"role": "user", "content": "token " + FAKE_KEY}) + "\n",
            encoding="utf-8",
        )
        checks = [diagnostics.Check("python", "ok", "fine")]
        #  effective_config 会 load_dotenv：指向不存在的文件关掉自动发现，否则 editable
        #  安装下读到的是开发者的真 .env，键会 setdefault 进 os.environ 泄给后面的用例
        env = {
            "XIAOYU_API_KEY": FAKE_KEY,
            "XIAOYU_MODEL": "m",
            "XIAOYU_ENV_FILE": str(self.root / "不存在.env"),
        }
        out = self.root / "bundle.json"
        with mock.patch.dict(os.environ, env), mock.patch(
            "xiaoyu.crash_guard._resolve_path", return_value=self.root / "crash.log"
        ):
            written = diagnostics.build_bundle(checks, self.root, session, out)
        self.assertEqual(written, out)
        payload = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(payload["config"]["env"]["XIAOYU_API_KEY"], "[REDACTED]")
        self.assertEqual(payload["config"]["env"]["XIAOYU_MODEL"], "m")
        self.assertEqual(payload["session"]["tail_lines"], 2)
        self.assertNotIn("sk-", json.dumps(payload))
        self.assertEqual(payload["doctor"]["checks"][0]["id"], "python")
        self.assertIn("分享前", payload["notice"])
        if os.name != "nt":
            self.assertEqual(out.stat().st_mode & 0o777, 0o600)
            link = self.root / "link.json"
            link.symlink_to(out)
            #  这一次调用同样会 load_dotenv，必须在同一份环境隔离里
            with mock.patch.dict(os.environ, env), mock.patch(
                "xiaoyu.crash_guard._resolve_path", return_value=self.root / "crash.log"
            ), self.assertRaises(ValueError):
                diagnostics.build_bundle(checks, self.root, session, link)


if __name__ == "__main__":
    unittest.main()
