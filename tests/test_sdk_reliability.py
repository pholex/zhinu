"""Reliability reporting must retain platform skips and actual failures."""
from pathlib import Path
import json
import tempfile
import unittest
import weakref
from unittest.mock import patch

from scripts import sdk_reliability


class ReliabilityReportTests(unittest.TestCase):
    def test_lifecycle_releases_harness_workers_before_resource_sampling(self):
        executors = []
        factory = sdk_reliability.ThreadPoolExecutor

        def executor(**kwargs):
            pool = factory(**kwargs)
            executors.append(weakref.ref(pool))
            return pool

        def sample():
            self.assertTrue(all(ref() is None for ref in executors))
            return {"fds": 10, "children": 0}

        config = {"baseline_trials": 1, "baseline_sessions": 1,
                  "turns_per_session": 1, "concurrency": 2,
                  "lifecycle_sessions": 4, "thresholds": {"max_fd_growth": 8}}
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(sdk_reliability, "ThreadPoolExecutor", executor), \
                    patch.object(sdk_reliability, "metrics", sample):
                report = sdk_reliability.lifecycle(config, Path(tmp))
        self.assertEqual(len(executors), 2)
        self.assertEqual(report["status"], "passed")

    def run_batch(self, *, fail=False):
        class Fixture(unittest.TestCase):
            def skipped(self):
                self.skipTest("POSIX-only fixture")

            def failed(self):
                self.fail("injected contract failure")

        suite = unittest.TestSuite([Fixture("skipped")] + ([Fixture("failed")] if fail else []))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(unittest.defaultTestLoader, "loadTestsFromNames", return_value=suite):
                report = sdk_reliability.contracts({"contract_repeats": 1, "contract_scenarios": ["R03"]}, root)
            saved = json.loads((root / "trials.json").read_text())
            self.assertEqual(saved, report["trials"])
            self.assertIn("POSIX-only fixture", (root / saved[0]["log"]).read_text())
        return report

    def test_platform_skip_is_serializable_and_never_counted_as_passed(self):
        report = self.run_batch()
        self.assertEqual(report["status"], "blocked")
        trial = report["trials"][0]
        self.assertEqual(trial["status"], "blocked")
        self.assertEqual(trial["skipped"][0]["reason"], "POSIX-only fixture")
        self.assertTrue(trial["skipped"][0]["test"].endswith(".skipped"))

    def test_actual_failure_takes_precedence_over_platform_skip(self):
        report = self.run_batch(fail=True)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["trials"][0]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
