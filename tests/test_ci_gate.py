"""scripts/ci_gate.py：判定与等待逻辑喂假运行记录（不打网络）。"""

from __future__ import annotations

import importlib.util
import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("ci_gate", REPO / "scripts" / "ci_gate.py")
ci_gate = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(ci_gate)


def run(status: str, conclusion: str | None = None, **extra) -> dict:
    return {"status": status, "conclusion": conclusion, "html_url": "u", **extra}


class DecideTest(unittest.TestCase):
    def test_success_wins(self):
        runs = [run("completed", "failure"), run("completed", "success"), run("in_progress")]
        self.assertEqual(ci_gate.decide(runs)[0], ci_gate.VERIFIED)

    def test_pending_beats_failure(self):
        """同一提交上有一次红的、又重跑了一次还在跑：等重跑的结论。"""
        runs = [run("completed", "failure"), run("queued")]
        self.assertEqual(ci_gate.decide(runs)[0], ci_gate.PENDING)

    def test_failure_kinds(self):
        for bad in ("failure", "cancelled", "timed_out", "startup_failure"):
            self.assertEqual(ci_gate.decide([run("completed", bad)])[0], ci_gate.FAILED, bad)

    def test_nothing_or_skipped_is_missing(self):
        self.assertEqual(ci_gate.decide([])[0], ci_gate.MISSING)
        self.assertEqual(ci_gate.decide([run("completed", "skipped")])[0], ci_gate.MISSING)


class WaitTest(unittest.TestCase):
    def _wait(self, answers, *, wait_seconds=600, missing_grace=120, step=30):
        clock = {"t": 0.0}
        calls = {"n": 0}

        def fetch(repo, workflow, sha):
            calls["n"] += 1
            if isinstance(answers, Exception):
                raise answers
            return answers[min(calls["n"] - 1, len(answers) - 1)]

        def sleep(seconds):
            clock["t"] += seconds

        with redirect_stdout(io.StringIO()), mock.patch.object(ci_gate.sys, "stderr", io.StringIO()):
            result, _ = ci_gate.wait("o/r", "ci.yml", "a" * 40, wait_seconds=wait_seconds, interval=step,
                                     missing_grace=missing_grace, sleep=sleep, now=lambda: clock["t"], fetch=fetch)
        return result, calls["n"], clock["t"]

    def test_waits_for_pending_then_verified(self):
        result, calls, elapsed = self._wait([[run("in_progress")], [run("in_progress")], [run("completed", "success")]])
        self.assertEqual((result, calls, elapsed), (ci_gate.VERIFIED, 3, 60))

    def test_failure_returns_immediately(self):
        result, calls, _ = self._wait([[run("completed", "failure")]])
        self.assertEqual((result, calls), (ci_gate.FAILED, 1))

    def test_missing_after_grace(self):
        result, calls, elapsed = self._wait([[]], missing_grace=120, step=30)
        self.assertEqual(result, ci_gate.MISSING)
        self.assertGreaterEqual(elapsed, 120)
        self.assertEqual(calls, 5)

    def test_pending_forever_times_out_as_missing(self):
        result, _, elapsed = self._wait([[run("queued")]], wait_seconds=300, step=60)
        self.assertEqual(result, ci_gate.MISSING)
        self.assertGreaterEqual(elapsed, 300)

    def test_api_errors_count_as_missing(self):
        result, _, _ = self._wait(RuntimeError("403"), missing_grace=0)
        self.assertEqual(result, ci_gate.MISSING)


class MainTest(unittest.TestCase):
    def test_failed_exits_nonzero_and_writes_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            with mock.patch.object(ci_gate, "wait", return_value=(ci_gate.FAILED, {"html_url": "x"})), \
                    mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out)}), redirect_stdout(io.StringIO()), \
                    mock.patch.object(ci_gate.sys, "stderr", io.StringIO()):
                code = ci_gate.main(["--repo", "o/r", "--sha", "a" * 40, "--workflow", "ci.yml"])
            self.assertEqual(code, 1)
            self.assertEqual(out.read_text(encoding="utf-8"), "result=failed\n")
        with mock.patch.object(ci_gate, "wait", return_value=(ci_gate.MISSING, None)), redirect_stdout(io.StringIO()):
            self.assertEqual(ci_gate.main(["--repo", "o/r", "--sha", "a" * 40, "--workflow", "ci.yml"]), 0)


if __name__ == "__main__":
    unittest.main()
