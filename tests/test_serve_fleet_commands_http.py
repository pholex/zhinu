"""Structured orchestration commands use the measured host-tool lifecycle."""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

from tests.test_serve import ServeCase
from tests.test_chenshu import git, LONG, text_turn
from tests.test_agent_paths import FakeClient
from xiaoyu.providers import Registry


class FleetCommandHttpTests(ServeCase):
    mcp = False
    token = "fleet-command-fixture"

    def boot(self, **config):
        self.start("error: parent model must never be called\n", **config)
        prior = os.environ.get("XIAOYU_ENABLE_CHENSHU")
        os.environ["XIAOYU_ENABLE_CHENSHU"] = "1"
        self.addCleanup(lambda: os.environ.pop("XIAOYU_ENABLE_CHENSHU", None) if prior is None
                        else os.environ.__setitem__("XIAOYU_ENABLE_CHENSHU", prior))
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "README.md").write_text("fixture\n", encoding="utf-8")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-qm", "fixture")
        return self.new_session()

    def caps(self, sid):
        response = self.client.get(f"/session/{sid}/fleet-commands/capabilities", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def preview(self, sid, body):
        response = self.client.post(f"/session/{sid}/fleet-commands/preview", headers=self.headers(), json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return {**body, "preview_token": response.json()["preview_token"]}

    def submit(self, sid, body, key):
        return self.client.post(f"/session/{sid}/fleet-commands", headers={**self.headers(), "Idempotency-Key": key}, json=body)

    def wait(self, sid):
        for _ in range(500):
            state = self.status(sid)
            if not state["busy"]:
                return state
            time.sleep(.01)
        self.fail("host command did not settle")

    def init(self, sid, mode="new"):
        body = self.preview(sid, {"action": "init", "mode": mode})
        response = self.submit(sid, body, "init-" + mode)
        self.assertEqual(response.status_code, 202, response.text)
        state = self._wait_for(sid, "waiting_for_approval")
        self.assertEqual(state["pending_approvals"][0]["tool"], "chenshu_init")
        request_id = state["pending_approvals"][0]["request_id"]
        response = self.client.post(f"/session/{sid}/permissions", headers=self.headers(),
                                    json={"request_id": request_id, "decision": "allow"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.wait(sid)["last_result"]["stopped"], "done")
        return body

    def command(self, sid, body, key):
        reviewed = self.preview(sid, body)
        response = self.submit(sid, reviewed, key)
        self.assertEqual(response.status_code, 202, response.text)
        state = self.wait(sid)
        self.assertEqual(state["last_result"]["stopped"], "done", state)
        return reviewed, response.json()

    def test_init_plan_spawn_resume_real_member_and_zero_parent_model_calls(self):
        sid = self.boot()
        session = self.client.app.state.sessions[sid]
        child = FakeClient([text_turn(LONG), text_turn(LONG)])
        runtime = session.agent.chenshu
        runtime.registry = Registry.for_client(child)
        self.assertTrue(self.caps(sid)["can_init"])
        self.assertFalse((self.root / ".xiaoyu" / "chenshu" / "state.json").exists())
        initial = self.init(sid)
        board_id = self.caps(sid)["board_id"]
        self.command(sid, {"action": "plan", "board_id": board_id,
            "missions": [{"title": "只读核对", "kind": "survey", "scope": [], "deps": []}]}, "plan")
        _, submission = self.command(sid, {"action": "spawn", "board_id": board_id,
            "name": "reader", "kind": "worker", "mission_id": "M1"}, "spawn")
        runtime._threads["reader"].join(5)
        self.assertFalse(runtime._threads["reader"].is_alive())
        first_run = runtime.member("reader").run_id
        caps = self.caps(sid)
        self.assertTrue(next(member for member in caps["members"] if member["name"] == "reader")["can_resume"], caps)
        self.command(sid, {"action": "resume", "board_id": board_id, "name": "reader",
                           "run_id": first_run, "instructions": "补充核对"}, "resume")
        runtime._threads["reader"].join(5)
        self.assertNotEqual(runtime.member("reader").run_id, first_run)
        duplicate = self.submit(sid, initial, "init-new")
        self.assertTrue(duplicate.json()["duplicate"])
        self.assertEqual(self.status(sid)["turns"], 4)
        runs = self.client.get(f"/session/{sid}/runs", headers=self.headers()).json()["runs"]
        self.assertEqual([run["command"]["action"] for run in runs], ["init", "plan", "spawn", "resume"])
        self.assertEqual(len(child.completions.calls), 2)
        self.assertEqual(submission["in_flight"]["command"]["kind"], "chenshu")

    def test_init_denial_has_no_state_and_retry_does_not_reask(self):
        sid = self.boot()
        body = self.preview(sid, {"action": "init", "mode": "new"})
        self.assertEqual(self.submit(sid, body, "denied-init").status_code, 202)
        pending = self._wait_for(sid, "waiting_for_approval")["pending_approvals"][0]
        self.client.post(f"/session/{sid}/permissions", headers=self.headers(),
                         json={"request_id": pending["request_id"], "decision": "deny", "reason": "本次不启动"})
        self.assertEqual(self.wait(sid)["last_result"]["stopped"], "tool_failed")
        self.assertFalse((self.root / ".xiaoyu" / "chenshu" / "state.json").exists())
        self.assertTrue(self.submit(sid, body, "denied-init").json()["duplicate"])
        self.assertEqual(self.status(sid)["turns"], 1)

    def test_approval_delay_revalidates_head_before_mutation(self):
        sid = self.boot()
        body = self.preview(sid, {"action": "init", "mode": "new"})
        self.submit(sid, body, "changed-head")
        pending = self._wait_for(sid, "waiting_for_approval")["pending_approvals"][0]
        (self.root / "README.md").write_text("changed\n", encoding="utf-8")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-qm", "changed")
        self.client.post(f"/session/{sid}/permissions", headers=self.headers(),
                         json={"request_id": pending["request_id"], "decision": "allow"})
        state = self.wait(sid)
        self.assertNotEqual(state["last_result"]["stopped"], "done", state)
        self.assertFalse((self.root / ".xiaoyu" / "chenshu" / "state.json").exists())
        self.assertTrue(self.submit(sid, body, "changed-head").json()["duplicate"])

    def test_restart_old_key_is_recorded_never_implicitly_adopts(self):
        state_dir = Path(self.tmp) / "state"
        sid = self.boot(state_dir=state_dir)
        body = self.init(sid)
        self.client.__exit__(None, None, None)
        self.start("error: do not call model\n", state_dir=state_dir)
        response = self.submit(sid, body, "init-new")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "submission_recorded")
        runtime = self.client.app.state.sessions[sid].agent.chenshu
        self.assertFalse(runtime.active)
        self.assertEqual(self.status(sid)["turns"], 1)

    def test_same_workspace_other_active_session_cannot_adopt(self):
        sid = self.boot()
        self.init(sid)
        second = self.new_session()
        caps = self.caps(second)
        self.assertFalse(caps["can_adopt"], caps)
        response = self.client.post(f"/session/{second}/fleet-commands/preview", headers=self.headers(),
                                   json={"action": "init", "mode": "adopt"})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.client.app.state.sessions[second].agent.chenshu.active)

    def test_persistence_reservation_failure_does_not_call_tool(self):
        sid = self.boot()
        session = self.client.app.state.sessions[sid]
        body = self.preview(sid, {"action": "init", "mode": "new"})
        with patch.object(session.agent.session_log, "commit_event", side_effect=OSError("full")):
            response = self.submit(sid, body, "failed-reservation")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(session.busy)
        self.assertFalse(session.agent.chenshu.active)
        self.assertEqual(self.submit(sid, body, "failed-reservation").status_code, 409)

    def test_validation_missing_key_busy_and_plan_mode(self):
        sid = self.boot()
        body = self.preview(sid, {"action": "init", "mode": "new"})
        self.assertEqual(self.submit(sid, body, "").status_code, 400)
        bad = self.client.post(f"/session/{sid}/fleet-commands/preview", headers=self.headers(), json={"action": "init", "mode": "new", "path": "/tmp"})
        self.assertEqual(bad.status_code, 400)
        self.submit(sid, body, "waiting")
        self._wait_for(sid, "waiting_for_approval")
        self.assertFalse(self.caps(sid)["can_init"])
        duplicate = self.submit(sid, body, "waiting")
        self.assertTrue(duplicate.json()["duplicate"])
        self.client.post(f"/session/{sid}/abort", json={}, headers=self.headers())
        self.wait(sid)
        session = self.client.app.state.sessions[sid]
        session.agent.set_mode("plan")
        self.assertFalse(self.caps(sid)["can_init"])
        self.assertEqual(self.submit(sid, body, "plan-mode").status_code, 409)

    def test_concurrent_same_key_reserves_one_host_turn(self):
        from concurrent.futures import ThreadPoolExecutor
        sid = self.boot()
        body = self.preview(sid, {"action": "init", "mode": "new"})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.submit(sid, body, "concurrent"), range(2)))
        self.assertTrue(all(response.status_code == 202 for response in results), [r.text for r in results])
        self.assertEqual(len({response.json()["run_id"] for response in results}), 1)
        pending = self._wait_for(sid, "waiting_for_approval")["pending_approvals"]
        self.assertEqual(len(pending), 1)
        self.client.post(f"/session/{sid}/permissions", headers=self.headers(),
                         json={"request_id": pending[0]["request_id"], "decision": "allow"})
        self.assertEqual(self.wait(sid)["turns"], 1)

    def test_nonpersistent_commands_unavailable(self):
        sid = self.boot(persist=False)
        self.assertFalse(self.caps(sid)["can_init"])
        response = self.client.post(f"/session/{sid}/fleet-commands/preview", headers=self.headers(),
                                   json={"action": "init", "mode": "new"})
        self.assertEqual(response.status_code, 409, response.text)

    def test_two_previews_cannot_acquire_same_workspace_after_approval(self):
        first = self.boot()
        second = self.new_session()
        body_one = self.preview(first, {"action": "init", "mode": "new"})
        body_two = self.preview(second, {"action": "init", "mode": "new"})
        self.assertEqual(self.submit(first, body_one, "owner-one").status_code, 202)
        self.assertEqual(self.submit(second, body_two, "owner-two").status_code, 202)
        pending_one = self._wait_for(first, "waiting_for_approval")["pending_approvals"][0]
        pending_two = self._wait_for(second, "waiting_for_approval")["pending_approvals"][0]
        self.client.post(f"/session/{first}/permissions", headers=self.headers(),
                         json={"request_id": pending_one["request_id"], "decision": "allow"})
        self.assertEqual(self.wait(first)["last_result"]["stopped"], "done")
        self.client.post(f"/session/{second}/permissions", headers=self.headers(),
                         json={"request_id": pending_two["request_id"], "decision": "allow"})
        self.assertEqual(self.wait(second)["last_result"]["stopped"], "tool_failed")
        self.assertFalse(self.client.app.state.sessions[second].agent.chenshu.active)
