"""自诊断：计量器自注册与配对、进程快照、doctor 各项判定与汇总。"""

from __future__ import annotations

import io
import json
import os
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


class DoctorCommandTest(unittest.TestCase):
    def test_runs_in_isolated_home_and_exits_by_status(self) -> None:
        from xiaoyu.cli import doctor_command

        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "HOME": tmp, "USERPROFILE": tmp,
                "XDG_CONFIG_HOME": str(Path(tmp) / "config"), "APPDATA": str(Path(tmp) / "config"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                out = io.StringIO()
                with redirect_stdout(out):
                    code = doctor_command(["--json", "--workspace", tmp])
        payload = json.loads(out.getvalue())
        ids = [check["id"] for check in payload["checks"]]
        self.assertEqual(
            ids,
            ["python", "config_dir", "disk", "providers", "sandbox", "bash_parser", "tools", "mcp_config", "sessions"],
        )
        self.assertEqual(code, 1 if payload["status"] == "fail" else 0)
        self.assertIn("gauges", payload["diagnostics"])


if __name__ == "__main__":
    unittest.main()
