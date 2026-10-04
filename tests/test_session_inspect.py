"""诊断读取保留行号、归属和坏行；不修改日志，输出先脱敏再截断。"""

import contextlib
import io
import json
import tempfile
from pathlib import Path
from unittest import TestCase

from xiaoyu.cli import sessions_command
from xiaoyu.session_inspect import inspect_session, render_report


class InspectTest(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "session.jsonl"
        records = [
            {"event": "meta", "format": 2},
            {"role": "user", "content": "修文件"},
            {"event": "request", "model": "m", "attempt": 1, "outcome": "error", "wait_s": 1},
            {"event": "request", "model": "m", "attempt": 2, "outcome": "ok", "total_ms": 20},
            {"role": "assistant", "tool_calls": [{"id": "c1", "function": {
                "name": "bash", "arguments": '{"command":"false"}'}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "exit_status: 1\nstderr:\nfailed"},
            {"event": "compact_start"},
            {"event": "compact_end", "ok": False},
            {"role": "user", "content": "改用别的方法"},
            {"role": "assistant", "tool_calls": [{"id": "c2", "function": {
                "name": "write_file", "arguments": '{"path":"a.txt"}'}}]},
            {"role": "tool", "tool_call_id": "c2", "content": "用户拒绝了这次工具调用。"},
        ]
        self.path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")

    def test_request_and_tool_attribution(self):
        before = self.path.read_bytes()
        report = inspect_session(self.path, tool_call="c1", raw=True)
        self.assertEqual([r["line"] for r in report["rows"]], [5, 6])
        self.assertTrue(all(r["request"] == 2 and r["turn"] == 1 for r in report["rows"]))
        self.assertTrue(report["rows"][-1]["error"])
        self.assertEqual(report["rows"][0]["data"]["role"], "assistant")
        self.assertEqual(self.path.read_bytes(), before)
        # 旧记录没有 request 事件时，不能归给上一段的模型请求。
        report = inspect_session(self.path, tool_call="c2")
        self.assertTrue(all(r["request"] is None for r in report["rows"]))

    def test_filters_and_last_limit(self):
        report = inspect_session(self.path, errors_only=True, limit=2)
        self.assertEqual(report["matched"], 4)
        self.assertEqual([r["line"] for r in report["rows"]], [8, 11])
        self.assertEqual(inspect_session(self.path, kinds=("compact",))["matched"], 2)
        self.assertEqual(inspect_session(self.path, kinds=("approval",), turn=2)["matched"], 1)
        self.assertEqual(inspect_session(self.path, kinds=("request",), request=1)["matched"], 1)

    def test_malformed_and_torn_tail_are_visible(self):
        with self.path.open("ab") as handle:
            handle.write(b'bad\n{"role":"user","content":"\xff"}\n{"role":')
        report = inspect_session(self.path)
        self.assertEqual(len(report["warnings"]), 2)
        self.assertIn("L12", report["warnings"][0])
        self.assertIn("末尾", report["warnings"][1])
        self.assertTrue(any("\ufffd" in row["summary"] for row in report["rows"]))

    def test_malformed_tool_calls_do_not_hide_later_records(self):
        with self.path.open("a", encoding="utf-8") as handle:
            for calls in (42, {"id": "bad"}, "bad"):
                handle.write(json.dumps({"role": "assistant", "tool_calls": calls}) + "\n")
            handle.write(json.dumps({"role": "user", "content": "still readable"}) + "\n")
        report = inspect_session(self.path)
        self.assertEqual(len(report["warnings"]), 3)
        self.assertEqual(report["rows"][-1]["summary"], "still readable")

    def test_surrogate_in_log_can_be_rendered_as_utf8(self):
        self.path.write_text(json.dumps({"role": "user", "content": "bad\ud800text"}) + "\n", encoding="utf-8")
        report = inspect_session(self.path, raw=True)
        output = render_report(report, raw=True).encode("utf-8")
        self.assertIn(b"bad?text", output)

    def test_raw_output_and_summary_redact_before_truncation(self):
        secret = "x" * 500
        self.path.write_text(json.dumps({"event": "error", "api_key": secret,
            "message": f"https://{secret}@host.example/a\u001b[31m"}) + "\n", encoding="utf-8")
        report = inspect_session(self.path, raw=True)
        output = json.dumps(report) + render_report(report, raw=True)
        self.assertNotIn("x" * 20, output)
        self.assertNotIn("\x1b", output)
        self.assertEqual(report["rows"][0]["data"]["api_key"], "[REDACTED]")
        self.assertIn("host.example/a", output)

    def test_cli_accepts_external_path_and_json(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sessions_command(["inspect", str(self.path), "--errors", "--json", "--limit", "1"])
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["rows"][0]["line"], 11)
        self.assertNotIn("data", report["rows"][0])

    def test_cli_rejects_directory_and_invalid_limit(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sessions_command(["inspect", str(self.path.parent)]), 2)
            with self.assertRaises(SystemExit):
                sessions_command(["inspect", str(self.path), "--limit", "0"])
