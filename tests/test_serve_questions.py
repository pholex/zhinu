"""HTTP question ownership, durable recovery and foreground control admission."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
from unittest.mock import patch

from tests.test_serve import ServeCase


ASK = 'tool_call: {"name":"ask_user","arguments":{"questions":[{"question":"Which color?","options":["Blue","Green"]}]}}\n'
SCRIPT = ASK + '---\ntext: Waiting for your answer.\n---\ntext: Answer received.\n'


class ServeQuestionTests(ServeCase):
    mcp = False

    def request(self, method, path, **kwargs):
        return self.client.request(method, path, headers=self.headers(), **kwargs)

    def pending(self, sid):
        result = self.request("GET", f"/session/{sid}/questions")
        self.assertEqual(result.status_code, 200, result.text)
        return result.json()["questions"]

    def wait_question(self, sid):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            items = self.pending(sid)
            if items:
                return items[0]
            time.sleep(0.01)
        self.fail("No question appeared")

    def ask(self, **kwargs):
        sid = self.new_session(questions=kwargs)
        response = self.request("POST", f"/session/{sid}/prompt", json={"text": "Ask a preference"})
        self.assertEqual(response.status_code, 200, response.text)
        q, = self.pending(sid)
        return sid, q

    def answer(self, sid, q, key="answer", choice="Blue"):
        return self.request("POST", f"/session/{sid}/questions/{q['question_id']}/answers", json={
            "answers": [{"item_id": q["items"][0]["item_id"], "selected": [choice]}],
            "idempotency_key": key})

    def test_idle_submission_idempotence_conflicts_and_no_automatic_run(self):
        self.start(SCRIPT)
        sid, q = self.ask()
        self.assertTrue(q["tool_call_id"])
        receipt = self.answer(sid, q)
        self.assertEqual(receipt.status_code, 202, receipt.text)
        self.assertEqual(receipt.json()["question"]["state"], "queued")
        self.assertEqual(self.answer(sid, q).json(), receipt.json())
        self.assertEqual(self.answer(sid, q, choice="Green").status_code, 409)
        self.assertEqual(self.answer(sid, q, key="other").status_code, 409)
        self.assertEqual(self.status(sid)["turns"], 1)
        self.assertFalse(self.status(sid)["busy"])
        self.request("POST", f"/session/{sid}/prompt", json={"text": "Continue"})
        replay = self.answer(sid, q)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.json()["question"]["state"], "answered")
        self.assertEqual(self.pending(sid), [])
        agent = self.client.app.state.sessions[sid].agent
        self.assertEqual(str(agent.messages).count(receipt.json()["question"]["answer_id"]), 1)

    def test_foreground_answer_works_when_all_model_workers_are_waiting(self):
        self.start(SCRIPT, max_sessions=1)
        sid = self.new_session(questions={"foreground_timeout_seconds": 60})
        self.request("POST", f"/session/{sid}/prompt_async", json={"text": "Ask"})
        q = self.wait_question(sid)
        self.assertEqual(q["state"], "open")
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                receipt = pool.submit(self.answer, sid, q).result(timeout=3)
            self.assertEqual(receipt.status_code, 202, receipt.text)
            self._wait_for(sid, "finished", timeout=3)
            self.assertEqual(self.pending(sid), [])
        finally:
            self.request("POST", f"/session/{sid}/abort", json={})

    def test_question_cancel_and_abort_are_distinct(self):
        for action in ("cancel", "abort"):
            with self.subTest(action=action):
                self.start(SCRIPT)
                sid = self.new_session(questions={"foreground_timeout_seconds": 60})
                self.request("POST", f"/session/{sid}/prompt_async", json={"text": "Ask"})
                q = self.wait_question(sid)
                if action == "cancel":
                    url = f"/session/{sid}/questions/{q['question_id']}"
                    self.assertEqual(self.request("DELETE", url).status_code, 204)
                    self.assertEqual(self.request("DELETE", url).status_code, 204)
                    self._wait_for(sid, "finished", timeout=3)
                    self.assertEqual(self.answer(sid, q).status_code, 409)
                else:
                    self.request("POST", f"/session/{sid}/abort", json={})
                    self._wait_for(sid, "interrupted", timeout=3)
                    self.assertEqual(self.pending(sid)[0]["state"], "pending")
                self.client.__exit__(None, None, None)

    def test_restart_keeps_queued_and_replays_delivery_exactly_once(self):
        state = Path(self.tmp) / "state"
        self.start(SCRIPT, state_dir=state)
        sid, q = self.ask()
        receipt = self.answer(sid, q).json()["question"]
        self.client.__exit__(None, None, None)
        self.start("text: Received\n", state_dir=state)
        self.assertEqual(self.pending(sid)[0], receipt)
        self.request("POST", f"/session/{sid}/prompt", json={"text": "Continue"})
        self.client.__exit__(None, None, None)
        self.start("text: Later\n", state_dir=state)
        self.assertEqual(self.pending(sid), [])
        self.assertEqual(str(self.client.app.state.sessions[sid].agent.messages).count(receipt["answer_id"]), 1)
        self.assertEqual(self.answer(sid, q).status_code, 200)

    def test_shutdown_open_wait_releases_lease_and_restores_pending(self):
        state = Path(self.tmp) / "state"
        self.start(SCRIPT, state_dir=state)
        sid = self.new_session(questions={"foreground_timeout_seconds": 60})
        self.request("POST", f"/session/{sid}/prompt_async", json={"text": "Ask"})
        self.assertEqual(self.wait_question(sid)["state"], "open")
        self.client.__exit__(None, None, None)
        self.start("text: Received\n", state_dir=state)
        q, = self.pending(sid)
        self.assertEqual((q["state"], q["version"]), ("pending", 2))
        self.assertEqual(self.answer(sid, q).status_code, 202)

    def test_delete_while_waiting_cancels_questions_without_resurrecting_manifest(self):
        state = Path(self.tmp) / "state"
        self.start(SCRIPT, state_dir=state)
        sid = self.new_session(questions={"foreground_timeout_seconds": 60})
        self.request("POST", f"/session/{sid}/prompt_async", json={"text": "Ask"})
        q = self.wait_question(sid)
        session = self.client.app.state.sessions[sid]
        log = session.agent.session_log
        self.assertEqual(self.request("DELETE", f"/session/{sid}").status_code, 200)
        self.assertEqual(self.answer(sid, q).status_code, 404)
        self.client.__exit__(None, None, None)
        self.assertFalse(log.locked)
        self.assertFalse((state / "sessions" / f"{sid}.json").exists())
        states = [json.loads(line)["question"]["state"] for line in log.path.read_text(errors="replace").splitlines()
                  if '"event": "sdk.question"' in line]
        self.assertEqual(states[-1], "cancelled")
        self.start("text: Hello\n", state_dir=state)
        self.assertNotIn(sid, self.client.app.state.sessions)

    def test_auth_scope_and_question_ids_cannot_resolve_approvals(self):
        self.token = "test-secret"
        self.start(SCRIPT)
        sid, q = self.ask()
        url = f"/session/{sid}/questions/{q['question_id']}"
        for method, path in (("GET", url), ("GET", f"/session/{sid}/questions"),
                             ("POST", url + "/answers"), ("DELETE", url)):
            self.assertEqual(self.client.request(method, path).status_code, 401)
        other = self.new_session(questions={})
        self.assertEqual(self.answer(other, q).status_code, 404)
        self.assertEqual(self.request("POST", f"/session/{sid}/permissions", json={
            "request_id": q["question_id"], "decision": "allow"}).status_code, 404)
        self.assertEqual(self.pending(sid)[0]["state"], "pending")

    def test_local_origin_guard_covers_question_mutations(self):
        self.start(SCRIPT)
        sid, q = self.ask()
        response = self.client.delete(f"/session/{sid}/questions/{q['question_id']}",
                                      headers={"Origin": "https://untrusted.example"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.pending(sid)[0]["state"], "pending")

    def test_strict_payload_validation_and_request_size_limit(self):
        self.start(SCRIPT)
        sid, q = self.ask()
        url = f"/session/{sid}/questions/{q['question_id']}/answers"
        item = q["items"][0]["item_id"]
        invalid = [None, [], {}, {"answers": [], "idempotency_key": "x"},
                   {"answers": [{"item_id": item, "selected": "Blue"}], "idempotency_key": "x"},
                   {"answers": [{"item_id": item, "selected": ["Blue"], "skipped": True}], "idempotency_key": "x"},
                   {"answers": [{"item_id": item, "custom": "x" * 4001}], "idempotency_key": "x"},
                   {"answers": [{"item_id": item, "selected": ["Unknown"]}], "idempotency_key": "x"},
                   {"answers": [{"item_id": item, "skipped": True}], "idempotency_key": ""}]
        for body in invalid:
            self.assertEqual(self.request("POST", url, content=json.dumps(body)).status_code, 400)
        self.assertEqual(self.request("POST", url, content=b"x" * 131073).status_code, 413)
        self.assertEqual(self.pending(sid)[0], q)

    def test_snapshot_cursor_sse_and_terminal_reconnect(self):
        self.start(SCRIPT, buffer_limit=4)
        sid, q = self.ask()
        snapshot = self.request("GET", f"/session/{sid}/questions?include_terminal=true").json()
        self.answer(sid, q)
        events = self.events(sid, **{"from": snapshot["next_seq"]})
        queued = next(e for e in events if e["kind"] == "question.reply_queued")
        self.assertEqual((queued["question"]["question_id"], queued["question"]["version"]), (q["question_id"], 2))
        stream = self.request("GET", f"/session/{sid}/events/stream?follow=false")
        self.assertEqual(stream.status_code, 200)
        self.assertIn('"kind": "question.reply_queued"', stream.text)
        self.request("POST", f"/session/{sid}/prompt", json={"text": "Continue"})
        response = self.request("GET", f"/session/{sid}/questions?include_terminal=true").json()
        self.assertEqual(response["questions"][0]["state"], "answered")
        self.assertGreater(self.status(sid)["dropped_events"], 0)

    def test_disabled_memory_config_and_fork_boundaries(self):
        self.start(SCRIPT)
        disabled = self.new_session()
        self.assertEqual(self.request("GET", f"/session/{disabled}/questions").status_code, 409)
        for options in ({"unknown": 1}, {"foreground_timeout_seconds": True}, {"foreground_timeout_seconds": -1}):
            self.assertEqual(self.request("POST", "/session", json={"questions": options}).status_code, 400)
        sid, q = self.ask()
        fork = self.new_session(fork_from=sid, questions={})
        self.assertEqual(self.pending(fork), [])
        self.assertEqual(self.answer(fork, q).status_code, 404)
        self.client.__exit__(None, None, None)
        self.start(SCRIPT, persist=False)
        self.assertEqual(self.request("POST", "/session", json={"questions": {}}).status_code, 400)

    def test_submission_write_acknowledgement_loss_is_reconciled_on_restart(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                state = Path(self.tmp) / str(committed)
                self.start(SCRIPT, state_dir=state)
                sid, q = self.ask()
                log = self.client.app.state.sessions[sid].agent.session_log
                original = log.commit_event
                def fail(kind, **fields):
                    if committed:
                        original(kind, **fields)
                    raise OSError("lost acknowledgement")
                with patch.object(log, "commit_event", side_effect=fail):
                    self.assertEqual(self.answer(sid, q).status_code, 503)
                self.assertEqual(self.request("POST", f"/session/{sid}/prompt", json={"text": "Continue"}).status_code, 503)
                self.client.__exit__(None, None, None)
                self.start("text: Received\n", state_dir=state)
                self.assertEqual(self.pending(sid)[0]["state"], "queued" if committed else "pending")
                receipt = self.answer(sid, q).json()["question"]
                self.request("POST", f"/session/{sid}/prompt", json={"text": "Continue"})
                self.assertEqual(str(self.client.app.state.sessions[sid].agent.messages).count(receipt["answer_id"]), 1)
                self.client.__exit__(None, None, None)

    def test_timely_answer_does_not_approve_a_following_write(self):
        write = 'tool_call: {"name":"write_file","arguments":{"path":"unapproved.txt","content":"x"}}\n'
        self.start(ASK + '---\n' + write + '---\ntext: Denied\n')
        sid = self.new_session(questions={"foreground_timeout_seconds": 60})
        self.request("POST", f"/session/{sid}/prompt_async", json={"text": "Ask and work"})
        q = self.wait_question(sid)
        self.assertEqual(self.answer(sid, q).status_code, 202)
        status = self._wait_for(sid, "waiting_for_approval", timeout=3)
        self.assertFalse((self.root / "unapproved.txt").exists())
        approval, = status["pending_approvals"]
        self.request("POST", f"/session/{sid}/permissions", json={
            "request_id": approval["request_id"], "decision": "deny"})
        self._wait_for(sid, "finished", timeout=3)
        self.assertFalse((self.root / "unapproved.txt").exists())

    def test_spent_budget_retains_queued_answer(self):
        self.start('usage: {"prompt_tokens":100,"completion_tokens":10}\n' + SCRIPT)
        sid, q = self.ask()
        self.request("POST", f"/session/{sid}/budget", json={"budget": {"tokens": 1}})
        self.assertEqual(self.answer(sid, q).status_code, 202)
        response = self.request("POST", f"/session/{sid}/prompt", json={"text": "Continue"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.pending(sid)[0]["state"], "queued")

    def test_journal_requires_lock_and_fsync_before_receipt(self):
        self.start(SCRIPT)
        sid, q = self.ask()
        log = self.client.app.state.sessions[sid].agent.session_log
        with patch("xiaoyu.session_log.os.fsync", side_effect=OSError("sync failed")):
            self.assertEqual(self.answer(sid, q).status_code, 503)
        self.assertFalse(log.complete)
        self.client.__exit__(None, None, None)
        self.start(SCRIPT)
        sid, q = self.ask()
        self.client.app.state.sessions[sid].agent.session_log.release()
        self.assertEqual(self.answer(sid, q).status_code, 503)

    def test_delivery_lost_acknowledgement_and_torn_tail_recovery(self):
        for committed in (False, True):
            with self.subTest(committed=committed):
                state = Path(self.tmp) / str(committed)
                self.start(SCRIPT, state_dir=state)
                sid, q = self.ask()
                receipt = self.answer(sid, q).json()["question"]
                log = self.client.app.state.sessions[sid].agent.session_log
                original = log.commit_event
                def fail(kind, **fields):
                    if kind == "sdk.question.delivery":
                        if committed:
                            original(kind, **fields)
                        raise OSError("lost delivery acknowledgement")
                    original(kind, **fields)
                with patch.object(log, "commit_event", side_effect=fail):
                    self.request("POST", f"/session/{sid}/prompt", json={"text": "Continue"})
                self.assertEqual(self.status(sid)["status"], "error")
                self.client.__exit__(None, None, None)
                with log.path.open("ab") as handle:
                    handle.write(b'{"event":"sdk.question.delivery","question":')
                self.start("text: Received\n", state_dir=state)
                self.assertEqual(bool(self.pending(sid)), not committed)
                self.request("POST", f"/session/{sid}/prompt", json={"text": "Continue"})
                self.assertEqual(str(self.client.app.state.sessions[sid].agent.messages).count(receipt["answer_id"]), 1)
                self.client.__exit__(None, None, None)
