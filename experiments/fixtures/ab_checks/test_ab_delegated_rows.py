"""验收用例（题面里不出现）：子 agent 的请求单列，不并进主链；两个脚本的汇总表都有这一项。"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

EXPERIMENTS = Path(__file__).resolve().parents[1] / "experiments"


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


compare = load("session_compare_under_check", EXPERIMENTS / "session_compare.py")
tasks = load("ab_tasks", EXPERIMENTS / "fixtures" / "ab_tasks.py")
runner = load("task_ab_under_check", EXPERIMENTS / "task_ab.py")

RECORDS = [
    {"ts": "2026-01-01T10:00:00", "event": "meta", "format": 2, "model": "model-a"},
    {"ts": "2026-01-01T10:00:00", "role": "user", "content": "查一下"},
    {"ts": "2026-01-01T10:00:01", "event": "request", "provider": "p", "model": "model-a",
     "outcome": "ok", "prompt_tokens": 1000, "completion_tokens": 10},
    {"ts": "2026-01-01T10:00:02", "event": "explore_request", "run": "r", "step": 1,
     "prompt_tokens": 700, "completion_tokens": 5},
    {"ts": "2026-01-01T10:00:03", "event": "explore_request", "run": "r", "step": 2,
     "prompt_tokens": 800, "completion_tokens": 6},
    {"ts": "2026-01-01T10:00:04", "event": "request", "provider": "p", "model": "model-a",
     "outcome": "ok", "prompt_tokens": 1200, "completion_tokens": 20},
    {"ts": "2026-01-01T10:00:05", "role": "assistant", "content": "查到了。"},
]


class DelegatedRowsTest(TestCase):
    def summary(self, records: list[dict]) -> tuple[dict, str]:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "session.jsonl"
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        timeline = compare.load_yu(path)
        return compare.summarize(timeline), compare.render_summary([timeline])

    def test_sub_agent_requests_are_counted_apart(self) -> None:
        summary, table = self.summary(RECORDS)
        self.assertEqual((summary["delegated_requests"], summary["delegated_context_total"],
                          summary["delegated_output_total"]), (2, 1500, 11))
        #  主链各项不变
        self.assertEqual((summary["requests"], summary["context_total"], summary["output_total"]),
                         (2, 2200, 30))
        self.assertIn("2 / 1,500", table)
        self.assertIn("子 agent", table)

    def test_session_without_sub_agent_reports_zero(self) -> None:
        summary, _ = self.summary([r for r in RECORDS if r.get("event") != "explore_request"])
        self.assertEqual((summary["delegated_requests"], summary["delegated_context_total"]), (0, 0))

    def test_batch_table_has_the_row(self) -> None:
        run = {"done": True, "tool_steps": 4, "requests": 5, "said_requests": 1, "seconds": 30,
               "ends_with_text": True, "delegated_requests": 11, "delegated_context_total": 78002}
        table = runner.render({"t": {"yu": [run]}},
                              {"t": tasks.Task(name="t", category="locate", prompt="p", ref="x")})
        lines = [line for line in table.splitlines() if "子 agent" in line]
        self.assertTrue(any("11" in line for line in lines), table)
        self.assertTrue(any("78,002" in line for line in lines), table)
