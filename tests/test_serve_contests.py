"""Competition commands and human decisions are separate durable facts."""
from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_serve import ServeCase
from xiaoyu import serve_contests

LONG = "已完成本席独立方案并核对范围与验收条件。" * 20


def fixture(cid="contest-one", final=True):
    return {"kind": "douqiao.updated", "contest_id": cid, "spec": "reader",
            "task": "比较两个方案", "task_truncated": False, "criteria": "正确性优先", "criteria_truncated": False,
            "revision": 3, "phase": "settled" if final else "competing", "final": final, "cancelled": False,
            "candidates": [{"id": f"{cid}:{index}", "index": index, "status": "completed" if final else "running",
                            "model": f"model-{index}", "run_id": f"child-{index}", "workspace": "",
                            "worktree": None, "answer": f"方案{index}", "answer_truncated": False,
                            "error": "", "started": True, "artifacts": [], "artifact_previews": []} for index in (1, 2)],
            "judge": {"status": "completed" if final else "pending", "model": "judge-model", "run_id": "judge-run",
                      "answer": "胜者：#2", "answer_truncated": False, "reason": "", "recommendation_id": f"{cid}:2" if final else ""}}


class ContestTests(ServeCase):
    mcp = False
    token = "contest-fixture-token"

    def boot(self, script=None, **config):
        self.start(script or f"text: {LONG}\n---\ntext: {LONG}\n---\ntext: 胜者：#2\n", **config)
        os.environ["XIAOYU_ENABLE_AGENTS"] = "1"
        previous = os.environ.get("XIAOYU_QIXIANG_CONCURRENCY")
        os.environ["XIAOYU_QIXIANG_CONCURRENCY"] = "1"
        self.addCleanup(lambda: os.environ.pop("XIAOYU_QIXIANG_CONCURRENCY", None) if previous is None
                        else os.environ.__setitem__("XIAOYU_QIXIANG_CONCURRENCY", previous))
        directory = self.root / ".xiaoyu" / "agents"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "reader.toml").write_text('description = "读取并核对"\nsystem_prompt = "完成本项读取。"\ntools = ["read_file"]\n', encoding="utf-8")
        return self.new_session()

    def emit(self, sid, payload):
        session = self.client.app.state.sessions[sid]
        self.client.portal.call(session.publish_event, SimpleNamespace(to_dict=lambda: copy.deepcopy(payload)))

    def request(self, sid):
        model = self.client.app.state.sessions[sid].agent.config.model
        return {"spec": "reader", "task": "比较方案", "models": [model, model], "criteria": "正确性", "judge_model": model}

    def preview(self, sid, body):
        response = self.client.post(f"/session/{sid}/contests/preview", json=body, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return {**body, "preview_token": response.json()["preview_token"]}

    def submit(self, sid, body, key="contest-key"):
        return self.client.post(f"/session/{sid}/contests", json=body,
                                headers={**self.headers(), "Idempotency-Key": key})

    def selection(self, sid, cid, candidate, revision=0, key="selection-key", reason="更符合任务"):
        return self.client.post(f"/session/{sid}/contests/{cid}/selection",
                                json={"candidate_id": candidate, "reason": reason, "expected_revision": revision},
                                headers={**self.headers(), "Idempotency-Key": key})

    def view(self, sid, **params):
        response = self.client.get(f"/session/{sid}/contests", params=params, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def wait(self, sid):
        for _ in range(500):
            state = self.status(sid)
            if not state["busy"]:
                return state
            time.sleep(.01)
        self.fail("contest did not settle")

    def test_real_command_candidate_judge_and_duplicate_submission(self):
        sid = self.boot()
        caps = self.client.get(f"/session/{sid}/contests/capabilities", headers=self.headers()).json()
        self.assertTrue(caps["can_start"], caps)
        self.assertEqual((caps["min_candidates"], caps["max_candidates"]), (2, 6))
        reviewed = self.preview(sid, self.request(sid))
        response = self.submit(sid, reviewed)
        self.assertEqual(response.status_code, 202, response.text)
        state = self.wait(sid)
        self.assertEqual(state["last_result"]["stopped"], "done", state)
        contest = self.view(sid)["contests"][0]
        self.assertEqual([item["status"] for item in contest["candidates"]], ["completed", "completed"])
        self.assertEqual(contest["judge"]["status"], "completed")
        self.assertEqual(contest["judge"]["recommendation_id"], contest["candidates"][1]["id"])
        self.assertIsNone(contest["selection"])
        retry = self.submit(sid, reviewed)
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertTrue(retry.json()["duplicate"])
        self.assertEqual(retry.json()["run_id"], response.json()["run_id"])
        self.assertEqual(len(self.view(sid)["contests"]), 1)

    def test_selection_cas_audit_and_old_receipt_survive_restart(self):
        state_dir = Path(self.tmp) / "state"
        sid = self.boot(state_dir=state_dir)
        self.emit(sid, fixture())
        first = self.selection(sid, "contest-one", "contest-one:1")
        self.assertEqual(first.status_code, 200, first.text)
        chosen = first.json()["selection"]
        self.assertEqual((chosen["source"], chosen["sequence"]), ("user", 1))
        self.assertEqual(self.selection(sid, "contest-one", "contest-one:2", key="stale").status_code, 409)
        second = self.selection(sid, "contest-one", "contest-one:2", revision=1, key="second")
        self.assertEqual(second.status_code, 200, second.text)
        retry = self.selection(sid, "contest-one", "contest-one:1")
        self.assertTrue(retry.json()["duplicate"])
        self.assertEqual(retry.json()["selection"], chosen)
        before = self.view(sid)["contests"][0]
        self.assertEqual(before["selection_revision"], 2)
        self.assertEqual(before["revision"], 3)
        self.assertEqual(len(before["selections"]), 2)
        self.assertEqual(before["judge"], fixture()["judge"])
        self.assertEqual(before["selection"]["candidate_id"], "contest-one:2")
        self.client.__exit__(None, None, None)
        self.start("error: no model should run\n", state_dir=state_dir)
        self.assertEqual(self.view(sid)["contests"][0], before)
        recovered = self.selection(sid, "contest-one", "contest-one:1")
        self.assertEqual(recovered.json()["selection"], chosen)
        self.assertTrue(recovered.json()["duplicate"])
        self.assertEqual(self.selection(sid, "contest-one", "contest-one:2").status_code, 409)
        self.assertEqual(self.status(sid)["turns"], 0)

    def test_preflight_rejects_entire_invalid_request_before_receipt(self):
        sid = self.boot("error: must not execute\n")
        body = self.request(sid)
        for invalid in ({**body, "models": [body["models"][0]]}, {**body, "models": [True, False]},
                        {**body, "models": body["models"] * 4}, {**body, "task": ""},
                        {**body, "source_path": "/tmp/private"}, {**body, "spec": "missing"}):
            response = self.client.post(f"/session/{sid}/contests/preview", json=invalid, headers=self.headers())
            self.assertEqual(response.status_code, 400, response.text)
        prepared = self.preview(sid, body)
        self.assertEqual(self.submit(sid, {**prepared, "task": "different"}).status_code, 409)
        self.assertEqual(self.client.app.state.sessions[sid].submission_receipts, {})
        self.assertEqual(self.view(sid)["contests"], [])

    def test_judge_failure_keeps_candidates_and_allows_explicit_user_selection(self):
        sid = self.boot(f"text: {LONG}\n---\ntext: {LONG}\n---\nerror: judge deliberately failed\n")
        response = self.submit(sid, self.preview(sid, self.request(sid)))
        self.assertEqual(response.status_code, 202, response.text)
        state = self.wait(sid)
        contest = self.view(sid)["contests"][0]
        self.assertEqual(state["last_result"]["stopped"], "contest_incomplete")
        self.assertEqual(contest["judge"]["status"], "failed")
        self.assertEqual(contest["judge"]["recommendation_id"], "")
        self.assertEqual(self.selection(sid, contest["contest_id"], contest["candidates"][0]["id"]).status_code, 200)
        self.assertEqual(self.view(sid)["contests"][0]["judge"], contest["judge"])

    def test_selection_rejects_partial_foreign_unsettled_busy_and_overlong_reason(self):
        sid = self.boot()
        unfinished = fixture(final=False)
        self.emit(sid, unfinished)
        self.assertEqual(self.selection(sid, "contest-one", "contest-one:1").status_code, 409)
        partial = fixture()
        partial["revision"] = 4
        partial["candidates"][0]["status"] = "partial"
        self.emit(sid, partial)
        self.assertEqual(self.selection(sid, "contest-one", "contest-one:1").status_code, 409)
        self.assertEqual(self.selection(sid, "contest-one", "another:2").status_code, 409)
        self.assertEqual(self.selection(sid, "contest-one", "contest-one:2", revision=True).status_code, 400)
        self.assertEqual(self.selection(sid, "contest-one", "contest-one:2", reason="x" * 4001).status_code, 400)
        session = self.client.app.state.sessions[sid]
        session.busy = True
        try:
            self.assertEqual(self.selection(sid, "contest-one", "contest-one:2").status_code, 409)
        finally:
            session.busy = False
        self.assertEqual(session.contests.receipts, {})
        self.assertEqual(self.selection(sid, "contest-one", "contest-one:2").status_code, 200)

    def test_selection_append_ack_loss_stops_writes_and_recovers_exact_decision(self):
        state_dir = Path(self.tmp) / "state"
        sid = self.boot(state_dir=state_dir)
        self.emit(sid, fixture())
        session = self.client.app.state.sessions[sid]
        log, original_events = session.agent.session_log, self.events(sid)
        commit = log.commit_event

        def lost_ack(kind, **fields):
            commit(kind, **fields)
            raise OSError("lost acknowledgement after append")

        with patch.object(log, "commit_event", side_effect=lost_ack):
            response = self.selection(sid, "contest-one", "contest-one:1")
        self.assertEqual(response.status_code, 503, response.text)
        self.assertIsNone(self.view(sid)["contests"][0]["selection"])
        self.assertEqual(self.events(sid), original_events)
        self.assertEqual(self.selection(sid, "contest-one", "contest-one:1").status_code, 503)
        self.client.__exit__(None, None, None)
        self.start("error: no model should run\n", state_dir=state_dir)
        retry = self.selection(sid, "contest-one", "contest-one:1")
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertTrue(retry.json()["duplicate"])
        self.assertEqual(len(self.view(sid)["contests"][0]["selections"]), 1)

    def test_snapshot_survives_event_eviction_and_restart_marks_unsettled(self):
        state_dir = Path(self.tmp) / "state"
        sid = self.boot(state_dir=state_dir, buffer_limit=2, max_field=100)
        payload = fixture()
        payload["candidates"][0]["answer"] = LONG
        payload["candidates"][0]["artifact_previews"] = [{"id": "artifact", "delivered": {"base64": "private-snapshot"}}]
        self.emit(sid, payload)
        self.emit(sid, fixture("second", final=False))
        self.emit(sid, fixture("third"))
        page = self.view(sid, cursor=2, limit=1)
        self.assertEqual(page["contests"][0]["candidates"][0]["answer"], LONG)
        self.assertNotIn("artifact_previews", page["contests"][0]["candidates"][0])
        self.assertNotIn("private-snapshot", json.dumps(self.events(sid)))
        session = self.client.app.state.sessions[sid]
        self.assertTrue(session.contests.contests["contest-one"]["candidates"][0]["artifact_previews"])
        self.client.__exit__(None, None, None)
        self.start("text: unused\n", state_dir=state_dir)
        second = next(item for item in self.view(sid)["contests"] if item["contest_id"] == "second")
        self.assertTrue(second["interrupted_by_restart"])
        self.assertEqual(second["judge"]["status"], "aborted")
        self.assertEqual(second["candidates"][0]["status"], "interrupted_by_restart")
        self.assertEqual(self.selection(sid, "second", "second:1").status_code, 409)

    def test_persistent_selection_limit_never_drops_old_receipts(self):
        sid = self.boot()
        self.emit(sid, fixture())
        self.assertEqual(self.selection(sid, "contest-one", "contest-one:1").status_code, 200)
        with patch.object(serve_contests, "MAX_SELECTIONS", 1):
            self.assertEqual(self.selection(sid, "contest-one", "contest-one:2", revision=1, key="second").status_code, 409)
            self.assertTrue(self.selection(sid, "contest-one", "contest-one:1").json()["duplicate"])
        self.assertEqual(len(self.view(sid)["contests"][0]["selections"]), 1)

    def test_selection_visible_contract_keyless_snapshot(self):
        sid = self.boot()
        self.emit(sid, fixture())
        self.selection(sid, "contest-one", "contest-one:1", reason="独立核对后采用第一席")
        self.selection(sid, "contest-one", "contest-one:2", revision=1, key="reconsider", reason="补充验收后改选第二席")
        actual = self.view(sid)["contests"][0]
        for decision in [actual["selection"], *actual["selections"]]:
            decision["id"] = f"<SELECTION:{decision['sequence']}>"
            decision["created_at"] = "<TIME>"
        text = json.dumps(actual, ensure_ascii=False, indent=2) + "\n"
        golden = Path(__file__).parent / "fixtures" / "contest_decisions.expected.json"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            golden.write_text(text, encoding="utf-8")
        self.assertEqual(text, golden.read_text(encoding="utf-8", errors="replace"))

    def test_routes_require_auth_and_existing_session(self):
        sid = self.boot()
        for path in (f"/session/{sid}/contests", f"/session/{sid}/contests/capabilities"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.post(f"/session/{sid}/contests/preview", json=self.request(sid)).status_code, 401)
        self.assertEqual(self.client.post(f"/session/{sid}/contests/contest-one/selection", json={}).status_code, 401)
        self.assertEqual(self.client.get("/session/missing/contests", headers=self.headers()).status_code, 404)
        self.assertNotIn("contest_protocol", self.client.get("/health").json())
        self.assertEqual(self.client.get("/health", headers=self.headers()).json()["contest_protocol"], 1)

    def test_selection_denies_empty_completed_record_and_corrupt_replay(self):
        sid = self.boot()
        payload = fixture()
        payload["candidates"][0]["answer"] = ""
        self.emit(sid, payload)
        self.assertEqual(self.selection(sid, "contest-one", "contest-one:1").status_code, 409)
        path = Path(self.tmp) / "corrupt.jsonl"
        path.write_text(json.dumps({"event": serve_contests.JOURNAL_EVENT, "contest": fixture()}) + "\ninvalid\n", encoding="utf-8")
        view = serve_contests.ContestView()
        view.restore(path)
        self.assertTrue(view.selection_fault)
        self.assertIn("损坏", view.warning)
        self.assertFalse(view.accept({**fixture("bad-shape"), "final": "false"}))

    def test_direct_tool_deny_and_budget_use_existing_policy_without_candidates(self):
        from xiaoyu.permissions import Permissions, parse_rule
        sid = self.boot("error: model must not execute\n")
        reviewed = self.preview(sid, self.request(sid))
        session = self.client.app.state.sessions[sid]
        with patch.object(session, "check_budget", return_value="test budget"):
            self.assertEqual(self.submit(sid, reviewed).status_code, 409)
        self.assertEqual(session.submission_receipts, {})
        session.agent.permissions = Permissions(self.root, [parse_rule("deny douqiao")])
        response = self.submit(sid, reviewed)
        self.assertEqual(response.status_code, 202, response.text)
        state = self.wait(sid)
        self.assertEqual(state["last_result"]["stopped"], "tool_failed")
        self.assertEqual(self.view(sid)["contests"], [])

    def test_delayed_preflight_error_returns_concurrent_accepted_receipt(self):
        import concurrent.futures
        import threading
        from xiaoyu import serve_commands
        sid = self.boot()
        for path, module in (("contests", serve_contests), ("weaves", serve_commands)):
            with self.subTest(command=path):
                if path == "weaves":
                    sid = self.new_session()
                    request = {"spec": "reader", "prompt_template": "{{item}}", "items": ["甲", "乙"]}
                else:
                    request = self.request(sid)
                preview = self.client.post(f"/session/{sid}/{path}/preview", json=request, headers=self.headers())
                self.assertEqual(preview.status_code, 200, preview.text)
                body = {**request, "preview_token": preview.json()["preview_token"]}
                session = self.client.app.state.sessions[sid]
                second_entered, first_accepted, finish_execution = threading.Event(), threading.Event(), threading.Event()
                lock, calls = threading.Lock(), [0]
                original_prepare, original_send = module.prepare, session.agent._send_host_tool

                def delayed_prepare(*args, **kwargs):
                    with lock:
                        calls[0] += 1
                        index = calls[0]
                    if index == 1:
                        self.assertTrue(second_entered.wait(3))
                    else:
                        second_entered.set()
                        self.assertTrue(first_accepted.wait(3))
                    return original_prepare(*args, **kwargs)

                def held_execution(*args, **kwargs):
                    self.assertTrue(finish_execution.wait(3))
                    return original_send(*args, **kwargs)

                def submit():
                    return self.client.post(f"/session/{sid}/{path}", json=body,
                                            headers={**self.headers(), "Idempotency-Key": "parallel-key"})

                with patch.object(module, "prepare", side_effect=delayed_prepare), \
                        patch.object(session.agent, "_send_host_tool", side_effect=held_execution), \
                        concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                    try:
                        pending = [pool.submit(submit) for _ in range(2)]
                        done, _ = concurrent.futures.wait(pending, timeout=3, return_when=concurrent.futures.FIRST_COMPLETED)
                        self.assertEqual(len(done), 1)
                        first = next(iter(done)).result()
                        self.assertEqual(first.status_code, 202, first.text)
                        self.assertTrue(session.busy)
                        first_accepted.set()
                        replies = [future.result(timeout=3) for future in pending]
                        self.assertEqual([response.status_code for response in replies], [202, 202])
                        self.assertEqual({response.json()["run_id"] for response in replies}, {first.json()["run_id"]})
                        self.assertEqual(sum(bool(response.json().get("duplicate")) for response in replies), 1)
                    finally:
                        first_accepted.set()
                        finish_execution.set()
                self.wait(sid)
                self.assertEqual(self.status(sid)["turns"], 1)
