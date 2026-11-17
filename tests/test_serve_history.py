"""Durable transcript, settlement and live-feed boundary contracts."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_serve import ServeCase
from xiaoyu import serve_history


class OriginalHistoryTests(unittest.TestCase):
    def test_compaction_does_not_replace_original_messages_and_lines_are_stable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            records = [
                {"event": "meta"},
                {"role": "user", "content": "original question"},
                {"role": "assistant", "content": "original reply"},
                {"event": "compact", "replacement": [{"role": "user", "content": "summary"}]},
                {"role": "user", "content": [{"type": "text", "text": "second question"}]},
                {"event": "sdk.question.delivery", "message": {"role": "user", "content": "answer"}},
                {"role": "system", "content": "never exposed"},
                {"role": "user", "content": "private instruction", "_operator": True},
                {"role": "user", "content": "background notification", "_injected": True},
                {"role": "user", "content": "peer content", "_peer": True},
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records))
            view = serve_history.read(path)
            self.assertEqual([item["content"] for item in view["messages"]],
                             ["original question", "original reply", "second question", "answer"])
            self.assertEqual([item["id"] for item in view["messages"]], ["log:2", "log:3", "log:5", "log:6"])
            self.assertIn("旧消息", view["warning"])
            self.assertEqual(view["runs"], [])

    def test_corrupt_and_torn_records_are_reported_without_exposing_invalid_data(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_bytes(b'{"role":"user","content":"ok"}\ninvalid\n{"role":')
            view = serve_history.read(path)
            self.assertEqual(len(view["messages"]), 1)
            self.assertIn("损坏", view["warning"])
            self.assertIn("尾部", view["warning"])


class HistoryEndpointsTests(ServeCase):
    mcp = False

    def history(self, sid, **params):
        response = self.client.get(f"/session/{sid}/history", params=params, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def runs(self, sid):
        return self.client.get(f"/session/{sid}/runs", headers=self.headers()).json()

    def test_history_outlives_event_buffer_with_stable_pagination_and_title(self):
        self.start("text: first reply\n---\ntext: second reply\n", buffer_limit=2, max_field=100)
        sid = self.new_session()
        first_prompt = "first " * 100
        for text in (first_prompt, "second"):
            self.client.post(f"/session/{sid}/prompt", json={"text": text})
        first = self.history(sid, limit=2)
        second = self.history(sid, cursor=first["next_cursor"], limit=2)
        self.assertEqual([m["content"] for m in first["messages"] + second["messages"]],
                         [first_prompt, "first reply", "second", "second reply"])
        self.assertEqual((first["total"], second["next_cursor"]), (4, None))
        self.assertEqual(len(set(m["id"] for m in first["messages"] + second["messages"])), 4)
        self.assertEqual(first["live_from_seq"], self.status(sid)["next_seq"])
        self.assertEqual(self.status(sid)["title"], " ".join(first_prompt.split())[:80])
        self.assertEqual(first["warning"], "")
        self.assertTrue(first["persisted"])
        self.assertEqual([run["turn"] for run in self.runs(sid)["runs"]], [1, 2])
        self.assertTrue(self.client.get("/health").json()["history_protocol"])

    def test_in_flight_body_is_excluded_and_live_boundary_starts_with_prompt(self):
        self.start('tool_call: {"name":"write_file","arguments":{"path":"report.txt","content":"x"}}\n---\ntext: done\n')
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt_async", json={"text": "do work"})
        self._wait_for(sid, "waiting_for_approval")
        view = self.history(sid)
        self.assertEqual(view["messages"], [])
        events = self.events(sid, **{"from": view["live_from_seq"]})
        self.assertEqual(events[0]["kind"], "run.started")
        self.assertEqual(events[0]["prompt"], "do work")
        self.assertEqual(self.runs(sid)["runs"][0]["status"], "running")
        self.client.post(f"/session/{sid}/abort", json={"turn": 1})
        self._wait_for(sid, "interrupted")
        self.assertEqual(self.history(sid)["messages"][0]["content"], "do work")
        self.assertEqual(self.runs(sid)["runs"][0]["status"], "interrupted")

    def test_results_title_updated_time_and_failure_survive_restart(self):
        state = Path(self.tmp) / "state"
        self.start("text: done\n---\nerror: deliberate failure\n", state_dir=state)
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt", json={"text": "first task"})
        original = self.status(sid)
        original_runs = self.runs(sid)["runs"]
        self.client.__exit__(None, None, None)
        self.start("error: deliberate failure\n", state_dir=state)
        restored = self.status(sid)
        self.assertEqual(restored["last_result"], original["last_result"])
        self.assertEqual(restored["title"], original["title"])
        self.assertEqual(restored["updated_at"], original["updated_at"])
        self.assertEqual(self.runs(sid)["runs"], original_runs)
        self.client.post(f"/session/{sid}/prompt", json={"text": "fail"})
        failed = self.runs(sid)["runs"][-1]
        self.assertEqual(failed["status"], "failed")
        self.client.__exit__(None, None, None)
        self.start("text: unused\n", state_dir=state)
        self.assertEqual(self.status(sid)["detail"], "failed")
        self.assertEqual(self.runs(sid)["runs"][-1], failed)

    def test_settlement_write_failure_is_visible_and_receipt_prevents_reexecution(self):
        self.start("text: done\n")
        sid = self.new_session()
        log = self.client.app.state.sessions[sid].agent.session_log
        commit = log.commit_event

        def failing(kind, **fields):
            if kind == serve_history.SETTLED:
                raise OSError("disk full")
            return commit(kind, **fields)

        headers = {"Idempotency-Key": "once"}
        with patch.object(log, "commit_event", side_effect=failing):
            response = self.client.post(f"/session/{sid}/prompt", json={"text": "work"}, headers=headers)
        self.assertEqual(response.json()["detail"], "persistence_failed")
        self.assertFalse(self.history(sid)["persisted"])
        self.assertIn("结算保存失败", self.history(sid)["warning"])
        repeated = self.client.post(f"/session/{sid}/prompt", json={"text": "work"}, headers=headers)
        self.assertTrue(repeated.json()["duplicate"])
        self.assertEqual(len(self.runs(sid)["runs"]), 1)

    def test_failed_start_journal_keeps_reserved_receipt_and_never_calls_model(self):
        self.start("error: must not run\n")
        sid = self.new_session()
        log = self.client.app.state.sessions[sid].agent.session_log
        headers = {"Idempotency-Key": "reserved"}
        with patch.object(log, "commit_event", side_effect=OSError("disk full")):
            first = self.client.post(f"/session/{sid}/prompt", json={"text": "work"}, headers=headers)
        self.assertEqual(first.status_code, 503)
        again = self.client.post(f"/session/{sid}/prompt", json={"text": "work"}, headers=headers)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["detail"]["code"], "submission_recorded")
        self.assertNotIn("run.started", self.kinds(sid))

    def test_corrupt_history_is_not_reported_as_persisted(self):
        self.start("text: done\n")
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt", json={"text": "work"})
        log = self.client.app.state.sessions[sid].agent.session_log
        with log.path.open("ab") as handle:
            handle.write(b"invalid record\n")
        self.assertFalse(self.history(sid)["persisted"])
        self.assertFalse(self.runs(sid)["persisted"])
        self.assertIn("损坏", self.history(sid)["warning"])

    def test_settled_journal_wins_over_stale_manifest_after_crash(self):
        state = Path(self.tmp) / "state"
        self.start("text: done\n", state_dir=state)
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt", json={"text": "work"})
        run = self.runs(sid)["runs"][0]
        self.client.__exit__(None, None, None)
        path = state / "sessions" / f"{sid}.json"
        manifest = json.loads(path.read_text())
        manifest["turns"] = 0
        manifest["in_flight"] = {"id": run["id"], "turn": 1, "started_at": run["started_at"]}
        path.write_text(json.dumps(manifest))
        self.start("text: unused\n", state_dir=state)
        self.assertIsNone(self.status(sid)["lost_turn"])
        self.assertEqual(self.status(sid)["turns"], 1)
        self.assertEqual(self.status(sid)["last_result"]["text"], "done")

    def test_history_and_runs_require_existing_session_and_auth(self):
        self.token = "history-access"
        self.start("text: unused\n")
        sid = self.new_session()
        for route in ("history", "runs"):
            self.assertEqual(self.client.get(f"/session/{sid}/{route}").status_code, 401)
            self.assertEqual(self.client.get(f"/session/missing/{route}", headers=self.headers()).status_code, 404)
            self.assertEqual(self.client.get(f"/session/{sid}/{route}?cursor=-1", headers=self.headers()).status_code, 422)

    def test_empty_artifacts_do_not_schedule_capture(self):
        from xiaoyu import artifact_views
        self.start("text: done\n")
        sid = self.new_session()
        with patch.object(artifact_views, "capture_session", side_effect=AssertionError("no files to capture")) as capture:
            response = self.client.post(f"/session/{sid}/prompt", json={"text": "answer only"})
        self.assertEqual(response.status_code, 200, response.text)
        capture.assert_not_called()
        self.assertEqual(self.runs(sid)["runs"][0]["status"], "completed")

    def test_executor_shutdown_preserves_settlement_with_unavailable_snapshots(self):
        import asyncio
        from xiaoyu import artifact_views
        self.approval = "allow_all"
        state = Path(self.tmp) / "state"
        self.start('tool_call: {"name":"write_file","arguments":{"path":"report.txt","content":"delivery"}}\n---\ntext: done\n', state_dir=state)
        sid = self.new_session()

        async def current_loop():
            return asyncio.get_running_loop()

        loop = self.client.portal.call(current_loop)
        schedule = loop.run_in_executor

        def closing_executor(executor, function, *args):
            if function is artifact_views.capture_session:
                raise RuntimeError("cannot schedule new futures after shutdown")
            return schedule(executor, function, *args)

        with patch.object(loop, "run_in_executor", side_effect=closing_executor):
            response = self.client.post(f"/session/{sid}/prompt", json={"text": "write report"})
        self.assertEqual(response.status_code, 200, response.text)
        session = self.client.app.state.sessions[sid]
        run = session.runs[0]
        self.assertEqual(run["status"], "completed")
        self.assertFalse(session.busy)
        self.assertIsNone(session.in_flight)
        snapshot = run["artifact_previews"][0]
        self.assertFalse(snapshot["delivered"]["available"])
        self.assertFalse(snapshot["before"]["available"])
        self.assertIn("服务关闭", snapshot["note"])
        self.assertNotIn("base64", snapshot["delivered"])
        identity, artifact_id = run["id"], snapshot["id"]
        self.client.__exit__(None, None, None)
        self.start("text: unused\n", state_dir=state)
        restored = self.client.app.state.sessions[sid].runs[0]
        self.assertEqual(restored["artifact_previews"], [snapshot])
        self.assertEqual(restored["status"], "completed")
        preview = self.client.get(f"/session/{sid}/artifacts/{artifact_id}", params={"run_id": identity}).json()
        import os
        supported = hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd
        self.assertEqual(preview["delivered"]["available"], supported)
        self.assertFalse(preview["diff"]["available"])
        (self.root / "report.txt").write_text("later changed", encoding="utf-8")
        changed = self.client.get(f"/session/{sid}/artifacts/{artifact_id}", params={"run_id": identity}).json()
        self.assertFalse(changed["delivered"]["available"])

    def test_running_capture_failure_is_not_mistaken_for_scheduling_failure(self):
        from xiaoyu import artifact_views
        self.approval = "allow_all"
        self.start('tool_call: {"name":"write_file","arguments":{"path":"report.txt","content":"delivery"}}\n---\ntext: done\n')
        sid = self.new_session()
        with patch.object(artifact_views, "capture_session", side_effect=RuntimeError("capture bug after shutdown")):
            with self.assertRaisesRegex(RuntimeError, "capture bug"):
                self.client.post(f"/session/{sid}/prompt", json={"text": "write report"})
        session = self.client.app.state.sessions[sid]
        self.assertNotIn("artifact_previews", session.runs[0])
        self.assertIsNotNone(session.in_flight)

class ActivityHistoryTests(unittest.TestCase):
    def test_opt_in_activity_preserves_order_and_existing_message_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'session.jsonl'
            records=[
                {'event':'meta','model':'first'},
                {'role':'user','content':'check'},
                {'role':'assistant','content':'Checking','tool_calls':[{'id':'call1','function':{'name':'status','arguments':'{"query":"version"}'}}]},
                {'role':'tool','tool_call_id':'call1','content':'{"version":1}'},
                {'role':'assistant','content':'Done'},
                {'event':'model','from_model':'first','model':'unused'},
                {'event':'model','from_model':'unused','model':'second'},
                {'role':'user','content':'next'},
                {'role':'assistant','content':'answer'},
            ]
            path.write_text(''.join(json.dumps(r)+'\n' for r in records))
            ordinary=serve_history.read(path)
            self.assertEqual([m['role'] for m in ordinary['messages']],['user','assistant','assistant','user','assistant'])
            rows=serve_history.read(path,include_activity=True)['messages']
            self.assertEqual([m['role'] for m in rows],['user','assistant','tool','assistant','model','user','assistant'])
            self.assertEqual(rows[2]['name'],'status')
            self.assertEqual(rows[2]['args'],{'query':'version'})
            self.assertEqual(rows[4]['from_model'],'first')
            self.assertEqual(rows[4]['model'],'second')
