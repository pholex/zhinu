"""实验判据的离线自证：失败不能被算作收益，原始载荷不能被成对比较改坏。"""
import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


def load(name):
    path = Path(__file__).resolve().parents[1] / "experiments" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


probe = load("reasoning_replay_probe")
surface = load("prompt_surface")


class ProbeContractTest(unittest.TestCase):
    def test_live_cli_records_failed_pairs_without_hiding_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "report.json"
            response = {"choices": [{"message": {"content": "UNKNOWN"}}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": 1}}
            argv = ["probe", "--protocol", "chat", "--model", "m", "--live",
                    "--base-url", "https://unused.invalid/v1", "--key-env", "TEST_PROBE_KEY",
                    "--output", str(output)]
            with mock.patch("sys.argv", argv), mock.patch.dict("os.environ", {"TEST_PROBE_KEY": "dummy"}), \
                    mock.patch.object(probe, "request", side_effect=[TimeoutError(), response, response, response]) as call, \
                    contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(probe.main(), 1)
            report = json.loads(output.read_text(encoding="utf-8", errors="replace"))
            self.assertEqual(call.call_count, report["request_cap"])
            self.assertEqual(len(report["results"]), 2)
            self.assertFalse(report["results"][0]["paired"])
            self.assertTrue(report["results"][1]["paired"])
            self.assertNotIn("dummy", str(report))

    def test_canary_exists_only_in_carrier_and_baseline_is_independent(self):
        for position in ("in_loop", "cross_turn"):
            base, replay = probe.chat_pair("m", position, "CANARY", 256)
            self.assertNotIn("CANARY", str(base))
            self.assertIn("CANARY", str(replay))
            copy_replay = copy.deepcopy(replay)
            del copy_replay["messages"][2]["reasoning_content"]
            self.assertEqual(base, copy_replay)

    def test_opaque_seed_is_preserved_and_tool_is_never_executed(self):
        request = probe.responses_seed("m", 2048)
        response = {"output": [{"type": "reasoning", "id": "r", "encrypted_content": "opaque"},
                               {"type": "function_call", "call_id": "c", "name": "report_value", "arguments": '{"value":1591}'}]}
        original = copy.deepcopy(response)
        base, replay = probe.responses_pair(request, response, "cross_turn")
        self.assertNotIn("encrypted_content", str(base))
        self.assertIn(response["output"][0], replay["input"])
        self.assertEqual(response, original)
        self.assertTrue(any(i.get("type") == "function_call_output" for i in replay["input"]))
        with self.assertRaises(ValueError):
            probe.responses_pair(request, {"output": []}, "in_loop")

    def test_only_actual_answer_can_pass_recall(self):
        response = {"choices": [{"message": {"content": "UNKNOWN"}}], "usage": {"prompt_tokens": 3}}
        self.assertFalse(probe.observation(response, "chat", "canary")["expected_recalled"])
        response["choices"][0]["message"]["content"] = "canary"
        self.assertTrue(probe.observation(response, "chat", "canary")["expected_recalled"])
        response["choices"][0]["finish_reason"] = "length"
        self.assertEqual(probe.observation(response, "chat", "canary")["status"], "incomplete")
        with self.assertRaises(ValueError):
            probe.observation({"error": "failure"}, "chat", "canary")

    def test_failed_baseline_never_counts_as_improvement(self):
        rows = {"baseline": {"status": "error"},
                "replay": {"status": "accepted", "expected_recalled": True, "input_tokens": 42}}
        report = probe.pair_report(rows)
        self.assertFalse(report["paired"] or report["recall_improved"])
        self.assertIsNone(report["input_token_delta"])
        rows["baseline"] = {"status": "accepted", "expected_recalled": False, "input_tokens": 12}
        self.assertTrue(probe.pair_report(rows)["recall_improved"])
        self.assertEqual(probe.pair_report(rows)["input_token_delta"], 30)

    def test_surface_diff_reports_cost_and_schema_changes(self):
        before = {"surfaces": {"default": {"system": "a", "tools": [{"function": {"name": "read", "description": "a"}}],
                   "system_tokens_estimate": 5, "tool_tokens_estimate": 6, "tool_count": 1}}}
        after = copy.deepcopy(before)
        after["surfaces"]["default"]["tools"][0]["function"]["description"] = "b"
        after["surfaces"]["default"]["tool_tokens_estimate"] = 9
        row = surface.compare(before, after)["surfaces"]["default"]
        self.assertEqual(row["changed_tools"], ["read"])
        self.assertEqual(row["tool_tokens_estimate_delta"], 3)
        self.assertFalse(row["system_changed"])
        with self.assertRaises(ValueError):
            surface.compare(before, {"surfaces": {}})
