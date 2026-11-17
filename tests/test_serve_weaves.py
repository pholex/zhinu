"""Structured collaboration views survive event eviction and service restart."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_serve import ServeCase


def batch(identity="batch-1", revision=1, final=False):
    return {"kind": "qixiang.updated", "batch_id": identity, "spec": "reader",
            "revision": revision, "final": final, "cancelled": False, "items": [
                {"id": f"{identity}:0", "index": 0, "label": "第一项",
                 "status": "completed" if final else "running", "run_id": "child-1",
                 "answer": "完整结果" * 100 if final else "", "error": ""},
                {"id": f"{identity}:1", "index": 1, "label": "第二项",
                 "status": "failed" if final else "queued", "run_id": "child-2",
                 "answer": "", "error": "明确失败" if final else ""}]}


class WeaveEndpointsTests(ServeCase):
    mcp = False
    token = "weave-contract-token"

    def emit(self, sid, payload):
        session = self.client.app.state.sessions[sid]
        self.client.portal.call(session.publish_event, SimpleNamespace(to_dict=lambda: payload))

    def view(self, sid, **params):
        response = self.client.get(f"/session/{sid}/weaves", params=params, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_authenticated_snapshots_outlive_clipped_and_evicted_events(self):
        self.start("text: unused\n", buffer_limit=2, max_field=100)
        sid = self.new_session()
        self.emit(sid, batch())
        self.emit(sid, batch(revision=2, final=True))
        self.emit(sid, batch("batch-2"))
        self.emit(sid, batch("batch-3"))
        view = self.view(sid, cursor=2, limit=1)
        self.assertEqual(view["total"], 3)
        self.assertIsNone(view["next_cursor"])
        self.assertEqual(view["batches"][0]["items"][0]["answer"], "完整结果" * 100)
        self.assertTrue(view["persisted"])
        self.assertEqual(self.client.get(f"/session/{sid}/weaves").status_code, 401)
        self.assertNotIn("weave_protocol", self.client.get("/health").json())
        self.assertEqual(self.client.get("/health", headers=self.headers()).json()["weave_protocol"], 1)

    def test_restart_preserves_final_results_and_marks_unsettled_items(self):
        state = Path(self.tmp) / "state"
        self.start("text: unused\n", state_dir=state)
        sid = self.new_session()
        self.emit(sid, batch(revision=2, final=True))
        self.emit(sid, batch("unfinished"))
        self.client.__exit__(None, None, None)
        self.start("text: unused\n", state_dir=state)
        view = self.view(sid)
        self.assertEqual(view["batches"][1]["items"][0]["status"], "completed")
        self.assertEqual(view["batches"][1]["items"][1]["status"], "failed")
        self.assertTrue(view["batches"][0]["interrupted_by_restart"])
        self.assertEqual([item["status"] for item in view["batches"][0]["items"]],
                         ["interrupted_by_restart", "interrupted_by_restart"])
        self.assertEqual(self.events(sid)[-1]["kind"], "session.recovered")

    def test_failed_journal_keeps_visible_results_and_warns(self):
        self.start("text: unused\n")
        sid = self.new_session()
        log = self.client.app.state.sessions[sid].agent.session_log
        with patch.object(log, "commit_event", side_effect=OSError("write failed")):
            self.emit(sid, batch(revision=2, final=True))
        view = self.view(sid)
        self.assertFalse(view["persisted"])
        self.assertIn("保存失败", view["warning"])
        self.assertEqual(len(view["batches"]), 1)

    def test_older_revisions_and_corrupt_records_do_not_replace_new_results(self):
        from xiaoyu.serve_weaves import JOURNAL_EVENT, WeaveView
        path = Path(self.tmp) / "weaves.jsonl"
        path.write_text(json.dumps({"event": JOURNAL_EVENT, "batch": batch(revision=2, final=True)})
                        + "\ninvalid\n"
                        + json.dumps({"event": JOURNAL_EVENT, "batch": batch()}) + "\n",
                        encoding="utf-8")
        view = WeaveView()
        view.restore(path)
        self.assertEqual(view.snapshot()[0]["revision"], 2)
        self.assertTrue(view.snapshot()[0]["final"])
        self.assertIn("损坏", view.warning)
