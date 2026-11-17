"""Durable desktop request receipts and fencing through real HTTP contracts."""
from pathlib import Path
import json
from unittest.mock import patch

from tests.test_serve import ServeCase

WRITE = 'tool_call: {"name":"write_file","arguments":{"path":"once.txt","content":"once"}}\n---\ntext: Finished\n'


class DesktopSubmissionTests(ServeCase):
    mcp = False

    def test_create_receipt_survives_restart_and_rejects_changed_body(self):
        state = Path(self.tmp) / "state"
        self.start("text: Ready\n", state_dir=state)
        headers = {"Idempotency-Key": "create-1"}
        body = {"workspace": str(self.root), "questions": {}}
        first = self.client.post("/session", json=body, headers=headers)
        sid = first.json()["session_id"]
        self.assertEqual(self.client.post("/session", json=body, headers=headers).json()["session_id"], sid)
        self.client.__exit__(None, None, None)
        self.start("text: Ready\n", state_dir=state)
        again = self.client.post("/session", json=body, headers=headers)
        self.assertEqual(again.json()["session_id"], sid)
        self.assertTrue(again.json()["duplicate"])
        self.assertEqual(len(self.client.get("/session").json()["sessions"]), 1)
        self.assertEqual(self.client.post("/session", json={**body, "mode": "plan"}, headers=headers).status_code, 409)

    def test_unrestored_session_does_not_get_created_again(self):
        state = Path(self.tmp) / "state"
        self.start("text: Ready\n", state_dir=state)
        headers = {"Idempotency-Key": "create-2"}
        sid = self.client.post("/session", json={}, headers=headers).json()["session_id"]
        self.client.__exit__(None, None, None)
        manifest = state / "sessions" / f"{sid}.json"
        data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
        data["session_log"] = str(Path(self.tmp) / "unavailable.jsonl")
        manifest.write_text(json.dumps(data), encoding="utf-8")
        self.start("text: Ready\n", state_dir=state)
        self.assertEqual(self.client.post("/session", json={}, headers=headers).status_code, 409)
        self.assertEqual(self.client.get("/session").json()["sessions"], [])

    def test_finished_prompt_is_not_reexecuted_after_restart(self):
        self.approval = "allow_all"
        state = Path(self.tmp) / "state"
        self.start(WRITE, state_dir=state)
        sid = self.new_session()
        headers = {"Idempotency-Key": "write-once"}
        first = self.client.post(f"/session/{sid}/prompt", json={"text": "write"}, headers=headers)
        self.assertEqual(first.status_code, 200)
        self.client.__exit__(None, None, None)
        self.start('error: must never be called\n', state_dir=state)
        again = self.client.post(f"/session/{sid}/prompt_async", json={"text": "write"}, headers=headers)
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.json()["detail"]["code"], "submission_recorded")
        self.assertEqual(self.status(sid)["turns"], 1)
        self.assertEqual((self.root / "once.txt").read_text(), "once")
        self.assertNotIn("run.started", self.kinds(sid))

    def test_evicted_result_still_has_a_nonreplayable_receipt(self):
        self.start("text: first\n---\ntext: second\n---\nerror: duplicate\n")
        sid = self.new_session()
        with patch("xiaoyu.serve.IDEMPOTENCY_KEEP", 1):
            for key in ("one", "two"):
                response = self.client.post(f"/session/{sid}/prompt", json={"text": key}, headers={"Idempotency-Key": key})
                self.assertEqual(response.status_code, 200)
        response = self.client.post(f"/session/{sid}/prompt_async", json={"text": "one"}, headers={"Idempotency-Key": "one"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.status(sid)["turns"], 2)

    def test_receipt_is_on_disk_before_approval_or_model_side_effect(self):
        state = Path(self.tmp) / "state"
        self.start(WRITE, state_dir=state)
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt_async", json={"text": "write"}, headers={"Idempotency-Key": "pending"})
        self._wait_for(sid, "waiting_for_approval")
        data = json.loads((state / "sessions" / f"{sid}.json").read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(data["submission_receipts"]["pending"]["turn"], 1)
        self.assertFalse((self.root / "once.txt").exists())
        self.client.post(f"/session/{sid}/abort", json={"turn": 1})
        self._wait_for(sid, "interrupted")

    def test_old_instance_abort_and_wrong_turn_steer_do_not_touch_active_work(self):
        self.start(WRITE)
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt_async", json={"text": "write"})
        self._wait_for(sid, "waiting_for_approval")
        headers = {"X-Xiaoyu-Instance": "old-process"}
        self.assertFalse(self.client.post(f"/session/{sid}/abort", json={"turn": 1}, headers=headers).json()["aborted"])
        self.assertEqual(self.client.post(f"/session/{sid}/steer", json={"text": "late", "turn": 1}, headers=headers).status_code, 409)
        self.assertEqual(self.client.post(f"/session/{sid}/steer", json={"text": "late", "turn": 2}).status_code, 409)
        self.assertTrue(self.status(sid)["busy"])
        self.client.post(f"/session/{sid}/abort", json={"turn": 1})
        self._wait_for(sid, "interrupted")

    def test_browser_preflight_allows_request_identity_headers_only_from_approved_origin(self):
        self.start("text: Ready\n", cors_origins=("http://127.0.0.1:8421",))
        headers = {"Origin": "http://127.0.0.1:8421", "Access-Control-Request-Method": "POST",
                   "Access-Control-Request-Headers": "content-type,idempotency-key,x-xiaoyu-instance"}
        self.assertEqual(self.client.options("/session", headers=headers).status_code, 200)
        self.assertNotEqual(self.client.options("/session", headers={**headers, "Origin": "https://outside.example"}).status_code, 200)

    def test_failed_receipt_write_does_not_start_and_requires_reconciliation(self):
        self.start("text: Executed once\n")
        sid = self.new_session()
        headers = {"Idempotency-Key": "storage-retry"}
        with patch("xiaoyu.serve_state.SessionStore.save", side_effect=OSError("disk full")):
            response = self.client.post(f"/session/{sid}/prompt_async", json={"text": "work"}, headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(self.status(sid)["busy"])
        self.assertNotIn("run.started", self.kinds(sid))
        response = self.client.post(f"/session/{sid}/prompt", json={"text": "work"}, headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.status(sid)["turns"], 0)
        self.assertFalse(self.status(sid)["writable"])

    def test_failed_creation_receipt_is_persisted_before_duplicate_acknowledgement(self):
        state = Path(self.tmp) / "state"
        self.start("text: Ready\n", state_dir=state)
        headers = {"Idempotency-Key": "create-storage-retry"}
        with patch("xiaoyu.serve_state.SessionStore.save", side_effect=OSError("disk full")):
            for _ in range(2):
                self.assertEqual(self.client.post("/session", json={}, headers=headers).status_code, 503)
        response = self.client.post("/session", json={}, headers=headers)
        sid = response.json()["session_id"]
        self.assertTrue(response.json()["duplicate"])
        self.assertEqual(len(self.client.get("/session").json()["sessions"]), 1)
        manifest = json.loads((state / "sessions" / f"{sid}.json").read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(manifest["creation_receipt"]["key"], headers["Idempotency-Key"])


class DeferredConversationTests(ServeCase):
    mcp = False

    def test_desktop_empty_shell_is_not_restored_but_first_submission_is(self):
        state = Path(self.tmp) / "state"
        self.start("text: Ready\n", state_dir=state, persist_empty_sessions=False)
        empty = self.new_session()
        used = self.new_session()
        self.assertFalse((state / "sessions" / f"{empty}.json").exists())
        self.assertEqual(self.client.post(f"/session/{used}/prompt", json={"text": "hello"}).status_code, 200)
        self.assertTrue((state / "sessions" / f"{used}.json").exists())
        self.client.__exit__(None, None, None)
        self.assertFalse((state / "sessions" / f"{empty}.json").exists())
        self.start("error: no model call expected\n", state_dir=state, persist_empty_sessions=False)
        self.assertEqual([s["session_id"] for s in self.client.get("/session").json()["sessions"]], [used])

    def test_first_submission_receipt_and_fork_are_retained(self):
        state = Path(self.tmp) / "state"
        self.start(WRITE, state_dir=state, persist_empty_sessions=False)
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt_async", json={"text": "write"}, headers={"Idempotency-Key": "first"})
        self._wait_for(sid, "waiting_for_approval")
        manifest = json.loads((state / "sessions" / f"{sid}.json").read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(manifest["submission_receipts"]["first"]["turn"], 1)
        self.assertFalse((self.root / "once.txt").exists())
        self.client.post(f"/session/{sid}/abort", json={"turn": 1})
        self._wait_for(sid, "interrupted")
        fork = self.new_session(fork_from=sid)
        self.assertTrue((state / "sessions" / f"{fork}.json").exists())
