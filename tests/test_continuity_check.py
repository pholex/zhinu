"""Experiment oracles must reject bad outcomes; process tests use no model/network."""
from contextlib import closing, redirect_stderr
import io
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import continuity_check
import continuity_faults
import continuity_memory
import continuity_soak
from continuity_support import read, report, save


class ContinuityTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def test_answer_oracle_rejects_wrong_missing_and_extra_fields(self):
        for bad in (None, {}, {"answer": "old"}, {"answer": "new", "extra": 1}, "new"):
            self.assertFalse(continuity_memory.score(bad, "new"))
        self.assertTrue(continuity_memory.score({"answer": "new"}, "new"))

    def test_invoice_oracle_rejects_bad_rounding_before_accepting_fix(self):
        path = self.root / "test_contract.py"
        path.write_text(continuity_soak.CONTRACT, encoding="utf-8")
        for value, expected in (("ROUND_HALF_EVEN", 1), ("ROUND_HALF_UP", 0), ("bogus", 1)):
            save(self.root / "invoice.json", {"rounding": value})
            proc = subprocess.run([sys.executable, "-I", str(path)], cwd=self.root,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            self.assertEqual(proc.returncode, expected)

    def test_real_process_fault_matrix(self):
        result = continuity_faults.run(self.root)
        self.assertEqual(len(result["checks"]), 11)
        self.assertEqual(result["status"], "pass", result)

    def test_memory_pair_uses_new_session_and_excludes_old_scope(self):
        for kind in continuity_memory.KINDS:
            path = self.root / kind
            continuity_memory.fixture(path, kind)
            continuity_memory.worker(path, False, False)
            continuity_memory.worker(path, True, False)
            on, off = read(path / "on.json"), read(path / "off.json")
            self.assertTrue(on["correct"], kind)
            self.assertNotEqual(on["session_id"], off["session_id"])
            self.assertEqual(off["correct"], kind not in continuity_memory.KINDS[:4], kind)

    def test_extraction_stays_pending_and_replay_avoids_model(self):
        path = self.root / "extraction"
        continuity_memory.extraction(path, False)
        result = read(path / "extraction.json")
        self.assertTrue(result["valid_pending_and_replay"])
        self.assertEqual(len(result["calls"]), 1)

    def test_budget_failure_prevents_live_callbacks(self):
        with patch("continuity_memory.child", side_effect=AssertionError("Must not call model")):
            with self.assertRaises(ValueError):
                continuity_memory.run(self.root, live=True, repeats=2, max_requests=41)

    def test_empty_or_running_report_cannot_claim_pass(self):
        self.assertEqual(report(self.root, "empty", "offline", 0, [])["status"], "fail")
        result = report(self.root, "partial", "offline", 0, [{"name": "one", "status": "pass"}], status="running")
        self.assertEqual(result["status"], "running")

    def test_resume_rejects_changed_code_before_running_worker(self):
        save(self.root / "campaign.json", {"code_digest": "old", "live": False})
        with patch("continuity_soak.child", side_effect=AssertionError("Must not launch")):
            with self.assertRaisesRegex(ValueError, "Source changed"):
                continuity_soak.run(self.root, seconds=1, interval=1, live=False,
                                    max_cycles=1, max_model_requests=1, resume=True)

    def test_soak_reserved_model_budget_survives_worker_restarts(self):
        save(self.root / "campaign.json", {"code_digest": continuity_soak.code_digest(),
             "max_model_requests": 1, "max_cycles": 2, "interval": .1})
        with closing(sqlite3.connect(self.root / "requests.sqlite")) as db, db:
            db.execute("CREATE TABLE requests (key TEXT, at REAL)")
            db.execute("INSERT INTO requests VALUES ('previous-interrupted-request', 1)")
        with patch("continuity_soak.ask", side_effect=AssertionError("Budget already spent")):
            for _ in range(2):
                continuity_soak.worker(self.root, True, False)
                self.assertEqual(read(self.root / "tick.json")["state"], "paused")
        with closing(sqlite3.connect(self.root / "requests.sqlite")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM requests").fetchone()[0], 1)

    def test_cli_does_not_overwrite_results_or_accept_nonfinite_duration(self):
        for args in (["faults", "--out", str(self.root)],
                     ["soak", "--out", str(self.root / "new"), "--seconds", "nan"]):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                continuity_check.main(args)
            self.assertEqual(error.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
