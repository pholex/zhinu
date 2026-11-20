"""验收用例（题面里不出现）：--turn 只留一段，汇总只算这一段，时间从这一段的输入起算。"""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

SCRIPT = Path(__file__).resolve().parents[1] / "experiments" / "session_compare.py"
spec = importlib.util.spec_from_file_location("session_compare_under_check", SCRIPT)
compare = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = compare
spec.loader.exec_module(compare)


def call(call_id: str, command: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": command})}}


RECORDS = [
    {"ts": "2026-01-01T10:00:00", "event": "meta", "format": 2, "model": "model-a"},
    {"ts": "2026-01-01T10:00:00", "role": "user", "content": "第一件事"},
    {"ts": "2026-01-01T10:00:01", "event": "request", "provider": "p", "model": "model-a",
     "outcome": "ok", "prompt_tokens": 100, "completion_tokens": 1},
    {"ts": "2026-01-01T10:00:01", "role": "assistant", "content": None, "tool_calls": [call("c1", "ls")]},
    {"ts": "2026-01-01T10:00:02", "role": "tool", "tool_call_id": "c1", "content": "exit_status: 0"},
    {"ts": "2026-01-01T10:01:00", "role": "user", "content": "第二件事"},
    {"ts": "2026-01-01T10:01:02", "event": "request", "provider": "p", "model": "model-a",
     "outcome": "ok", "prompt_tokens": 900, "completion_tokens": 2},
    {"ts": "2026-01-01T10:01:02", "role": "assistant", "content": None,
     "tool_calls": [call("c2", "pwd"), call("c3", "date")]},
    {"ts": "2026-01-01T10:01:05", "role": "tool", "tool_call_id": "c2", "content": "exit_status: 0"},
    {"ts": "2026-01-01T10:01:05", "role": "tool", "tool_call_id": "c3", "content": "exit_status: 0"},
    {"ts": "2026-01-01T10:01:06", "event": "request", "provider": "p", "model": "model-a",
     "outcome": "ok", "prompt_tokens": 950, "completion_tokens": 3},
    {"ts": "2026-01-01T10:01:07", "role": "assistant", "content": "办完了。"},
]


class TurnOptionTest(TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "session.jsonl"
        self.path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in RECORDS),
                             encoding="utf-8")

    def run_json(self, *extra: str) -> dict:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(compare.main(["--yu", str(self.path), "--json", *extra]), 0)
        return json.loads(out.getvalue())[0]

    def test_second_turn_only(self) -> None:
        data = self.run_json("--turn", "2")
        self.assertEqual([s["detail"] for s in data["steps"] if s["kind"] == "user"], ["第二件事"])
        self.assertEqual([s["detail"] for s in data["steps"] if s["kind"] == "tool"], ["pwd", "date"])
        summary = data["summary"]
        self.assertEqual((summary["tool_steps"], summary["requests"], summary["user_inputs"]), (2, 2, 1))
        self.assertEqual(summary["context_max"], 950)
        self.assertTrue(summary["ends_with_text"])

    def test_time_restarts_at_the_selected_turn(self) -> None:
        data = self.run_json("--turn", "2")
        self.assertEqual(data["steps"][0]["at"], 0)
        self.assertEqual(data["summary"]["seconds"], 7)

    def test_first_turn_stops_before_the_next_input(self) -> None:
        data = self.run_json("--turn", "1")
        self.assertEqual([s["detail"] for s in data["steps"]], ["第一件事", "ls"])
        self.assertEqual(data["summary"]["requests"], 1)

    def test_without_the_option_everything_is_kept(self) -> None:
        self.assertEqual(self.run_json()["summary"]["tool_steps"], 3)

    def test_out_of_range_fails(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                code = compare.main(["--yu", str(self.path), "--turn", "3"])
            except SystemExit as stop:
                code = stop.code
        self.assertNotIn(code, (0, None))
