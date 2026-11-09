"""Host observations coalesce with an injected clock and never poll on their own."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))
from xiaoyu_agent_sdk import Observation, ObservationMonitor


class MonitoringTests(unittest.TestCase):
    def test_changes_coalesce_and_repeated_cycles_have_distinct_keys(self):
        now, sent = [0.0], []
        monitor = ObservationMonitor(lambda text, **kw: sent.append((text, kw)), clock=lambda: now[0])
        self.assertFalse(monitor.observe(Observation("CI", "running", "started")))
        self.assertFalse(monitor.observe(Observation("CI", "running", "still running", 60)))
        self.assertEqual(monitor.flush(), 0)
        monitor.observe(Observation("CI", "failed", "two failures"))
        monitor.observe(Observation("CI", "failed", "one remaining failure"))
        self.assertEqual(monitor.flush(), 0)
        now[0] = 1
        self.assertEqual(monitor.flush(), 1)
        self.assertIn("one remaining", sent[0][0])
        monitor.observe(Observation("CI", "running", "rerun"))
        now[0] = 2
        monitor.flush()
        monitor.observe(Observation("CI", "failed", "failed again"))
        now[0] = 3
        monitor.flush()
        self.assertEqual(len({item[1]["key"] for item in sent}), 3)

    def test_reverted_changes_are_quiet_and_failed_delivery_is_retriable(self):
        sent = []
        def notify(text, **kwargs):
            if not sent:
                sent.append("fail")
                raise OSError("host unavailable")
            sent.append(kwargs)
        monitor = ObservationMonitor(notify, coalesce_seconds=0)
        monitor.observe(Observation("review", "waiting", "needs a person", 900))
        monitor.observe(Observation("review", "approved", "approved"))
        monitor.observe(Observation("review", "waiting", "approval withdrawn", 1800))
        self.assertEqual(monitor.flush(), 0)
        self.assertEqual(monitor.observations()[0].next_check_at, 1800)
        monitor.observe(Observation("review", "approved", "approved"))
        with self.assertRaises(OSError):
            monitor.flush()
        self.assertEqual(monitor.flush(), 1)
        self.assertEqual(monitor.flush(), 0)
        monitor.forget("review")
        self.assertEqual(monitor.observations(), ())
