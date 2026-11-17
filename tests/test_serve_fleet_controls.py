"""Durable member receipts and parent/member approval isolation, without models."""
from __future__ import annotations

import copy
import threading
import time
from pathlib import Path
from unittest.mock import Mock, patch

from tests.test_serve import ServeCase
from tests import test_serve_fleets as _fleets
fleet = _fleets.fleet
from xiaoyu.serve_fleet_controls import ControlLedger


class FleetControlTests(ServeCase):
    mcp = False
    token = "member-controls-token"
    boot = _fleets.FleetEndpointsTests.boot
    emit = _fleets.FleetEndpointsTests.emit
    view = _fleets.FleetEndpointsTests.view

    def prepare(self, **config):
        sid = self.boot(**config)
        session = self.client.app.state.sessions[sid]
        session.agent.chenshu._board_id = "board-one"
        self.emit(sid, fleet())
        def control(kind, **args):
            return {"operation_id": args["operation_id"], "kind": kind, "board_id": args["board_id"],
                    "member_name": args["name"], "member_run_id": args["run_id"],
                    "status": "queued" if kind == "message" else "requested", "reason": "", "updated_at": time.time()}
        mocked = Mock(side_effect=control)
        session.agent.chenshu.member_control = mocked
        return sid, session, mocked

    def send(self, sid, key="key-one", kind="messages", body=None):
        return self.client.post(f"/session/{sid}/fleets/board-one/members/worker-one/{kind}",
            headers={**self.headers(), "Idempotency-Key": key},
            json=body if body is not None else {"run_id": "worker-run-one", "text": "请检查边界"})

    def test_reserved_before_effect_and_duplicate_never_retargets_new_generation(self):
        sid, session, control = self.prepare()
        old = control.side_effect
        def checked(kind, **args):
            records = session.agent.session_log.path.read_text(encoding="utf-8")
            self.assertIn('"status": "reserved"', records)
            return old(kind, **args)
        control.side_effect = checked
        first = self.send(sid)
        self.assertEqual(first.status_code, 202, first.text)
        operation = first.json()["operation"]
        self.assertEqual(operation["status"], "queued")
        self.assertNotIn("key", operation)
        payload = fleet(revision=2)
        payload["members"][0]["run_id"] = "new-generation"
        self.emit(sid, payload)
        duplicate = self.send(sid).json()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["operation"]["operation_id"], operation["operation_id"])
        self.assertEqual(control.call_count, 1)
        conflict = self.send(sid, body={"run_id": "new-generation", "text": "请检查边界"})
        self.assertEqual(conflict.status_code, 409)
        lookup = self.client.get(f'/session/{sid}/fleet-operations/{operation["operation_id"]}', headers=self.headers())
        self.assertEqual(lookup.json()["operation"]["run_id"], "worker-run-one")
        self.assertEqual(self.view(sid)["fleets"][0]["members"][0]["operations"], [])

    def test_true_consumption_event_updates_receipt_and_persists_across_restart(self):
        state = Path(self.tmp) / "state"
        sid, session, control = self.prepare(state_dir=state)
        operation = self.send(sid).json()["operation"]
        payload = fleet(revision=2)
        payload["controls"] = [{**operation, "status": "accepted"}]
        self.emit(sid, payload)
        self.assertEqual(self.view(sid)["fleets"][0]["members"][0]["operations"][0]["status"], "accepted")
        self.client.__exit__(None, None, None)
        self.start("text: unused\n", state_dir=state)
        duplicate = self.send(sid).json()
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["operation"]["status"], "accepted")
        self.assertFalse(self.view(sid)["fleets"][0]["members"][0]["controls"]["can_message"])

    def test_restart_uncertain_receipt_never_replays_and_history_rejects_new_key(self):
        state = Path(self.tmp) / "state"
        sid, _, control = self.prepare(state_dir=state)
        self.send(sid)
        self.client.__exit__(None, None, None)
        self.start("text: unused\n", state_dir=state)
        duplicate = self.send(sid).json()
        self.assertEqual(duplicate["operation"]["status"], "unknown")
        self.assertEqual(self.send(sid, key="new-key").status_code, 409)
        self.assertEqual(control.call_count, 1)

    def test_reservation_failure_never_applies_and_retries_are_read_only(self):
        sid, session, control = self.prepare()
        with patch.object(session.agent.session_log, "commit_event", side_effect=OSError("full")):
            response = self.send(sid)
        self.assertEqual(response.status_code, 503)
        control.assert_not_called()
        retry = self.send(sid).json()
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["operation"]["status"], "unknown")
        self.assertEqual(self.send(sid, key="second").status_code, 409)

    def test_outcome_failure_is_unknown_not_false_success(self):
        sid, session, control = self.prepare()
        commit = session.agent.session_log.commit_event
        count = 0
        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("lost acknowledgement")
            return commit(*args, **kwargs)
        with patch.object(session.agent.session_log, "commit_event", side_effect=fail_second):
            result = self.send(sid).json()
        self.assertEqual(result["operation"]["status"], "unknown")
        self.assertEqual(self.send(sid).json()["operation"]["status"], "unknown")
        self.assertEqual(control.call_count, 1)

    def test_validation_cross_session_and_strict_core_rejection(self):
        sid, session, control = self.prepare()
        self.assertEqual(self.send(sid, key="").status_code, 400)
        self.assertEqual(self.send(sid, body={"run_id": "worker-run-one", "text": "  "}).status_code, 400)
        self.assertEqual(self.send(sid, body={"run_id": "worker-run-one", "text": "x", "path": "/tmp"}).status_code, 400)
        self.assertEqual(self.send(self.new_session()).status_code, 409)
        control.assert_not_called()
        control.side_effect = ValueError("成员代已结束")
        result = self.send(sid).json()
        self.assertEqual(result["operation"]["status"], "failed")
        self.assertIn("已结束", result["operation"]["reason"])
        self.send(sid)
        self.assertEqual(control.call_count, 1)

    def test_disabled_for_nonpersistent_or_faulted_history(self):
        sid, session, control = self.prepare(persist=False)
        self.assertEqual(self.send(sid).status_code, 409)
        control.assert_not_called()
        self.assertFalse(self.view(sid)["fleets"][0]["members"][0]["controls"]["can_message"])

    def test_lost_log_lease_blocks_message_but_precise_stop_is_volatile(self):
        sid, session, control = self.prepare()
        session.agent.session_log.release()
        self.assertEqual(self.send(sid).status_code, 409)
        control.assert_not_called()
        reply = self.send(sid, kind="stop", body={"run_id": "worker-run-one"})
        self.assertEqual(reply.status_code, 202, reply.text)
        operation = reply.json()["operation"]
        self.assertFalse(operation["persisted"])
        self.assertTrue(operation["warning"])
        self.assertEqual(operation["status"], "stop_requested")
        self.assertEqual(self.send(sid, kind="stop", body={"run_id": "worker-run-one"}).status_code, 200)
        self.assertEqual(control.call_count, 1)
        self.assertEqual(self.send(sid, key="another", kind="stop", body={"run_id": "worker-run-one"}).status_code, 409)
        self.assertEqual(control.call_count, 1)

    def test_stop_request_is_not_settled_until_core_reports_it(self):
        sid, session, _ = self.prepare()
        operation = self.send(sid, kind="stop", body={"run_id": "worker-run-one"}).json()["operation"]
        self.assertEqual(operation["status"], "stop_requested")
        payload = fleet(revision=2)
        payload["controls"] = [{**operation, "status": "settled"}]
        payload["members"][0].update(execution_state="settled", stop_requested=True)
        self.emit(sid, payload)
        member = self.view(sid)["fleets"][0]["members"][0]
        self.assertEqual(member["operations"][0]["status"], "stopped")
        self.assertFalse(member["controls"]["can_stop"])

    def start_approval(self, session, stop, name="worker-one"):
        identity = {"board_id": "board-one", "member_name": name, "member_run_id": name + "-run-one",
                    "mission_id": "mission-one", "workspace": str(self.root)}
        results = []
        thread = threading.Thread(target=lambda: results.append(session.agent.chenshu.member_approver(
            "write_file", {"path": "a.txt", "content": "content"}, identity, stop.is_set)))
        thread.start()
        self.addCleanup(lambda: (stop.set(), thread.join(4)))
        for _ in range(100):
            pending = session.snapshot_pending()
            matching = [item for item in pending if item.identity and item.identity["member_name"] == name]
            if matching:
                return matching[0], results, thread
            time.sleep(.01)
        self.fail("member approval never appeared")

    def test_member_approval_identity_allow_session_rejected_parent_abort_does_not_cancel(self):
        sid, session, _ = self.prepare()
        stop = threading.Event()
        pending, results, thread = self.start_approval(session, stop)
        value = pending.to_dict()
        self.assertEqual(value["identity"]["member_run_id"], "worker-one-run-one")
        self.assertEqual(value["allowed_decisions"], ["allow", "deny"])
        self.assertEqual(value["session_scopes"], [])
        self.client.post(f"/session/{sid}/abort", json={}, headers=self.headers())
        self.assertFalse(pending.done.is_set())
        completed = self.client.post(f"/session/{sid}/prompt", json={"text": "登记"}, headers=self.headers())
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertFalse(pending.done.is_set())
        url = f"/session/{sid}/permissions"
        for decision, extra in [("allow_session", {}), ("allow", {"updated_args": {}})]:
            response = self.client.post(url, headers=self.headers(), json={"request_id": pending.id, "decision": decision, **extra})
            self.assertEqual(response.status_code, 400)
            self.assertFalse(pending.done.is_set())
        response = self.client.post(url, headers=self.headers(), json={"request_id": pending.id, "decision": "allow"})
        self.assertEqual(response.status_code, 200)
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(results), 1)
        self.assertNotEqual(session.detail, "waiting_for_approval")

    def test_member_stop_and_close_wake_only_its_approval(self):
        sid, session, _ = self.prepare()
        stop = threading.Event()
        pending, results, thread = self.start_approval(session, stop)
        stop.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(pending.decision, "cancelled")
        self.assertFalse(session.snapshot_pending())
        stop2 = threading.Event()
        pending2, _, thread2 = self.start_approval(session, stop2)
        response = self.client.delete(f"/session/{sid}", headers=self.headers())
        self.assertEqual(response.status_code, 200)
        thread2.join(2)
        self.assertFalse(thread2.is_alive())
        self.assertTrue(pending2.done.is_set())

    def test_corrupt_and_torn_journal_fail_closed(self):
        path = Path(self.tmp) / "bad.jsonl"
        for data in ['{"event":', '{"event":"serve.chenshu.control","operation":{}}\n']:
            path.write_text(data, encoding="utf-8")
            ledger = ControlLedger()
            ledger.restore(path)
            self.assertFalse(ledger.integrity)


    def test_two_members_stop_does_not_wake_other_approval(self):
        _, session, _ = self.prepare()
        stop_one, stop_two = threading.Event(), threading.Event()
        first, _, worker_one = self.start_approval(session, stop_one)
        second, _, worker_two = self.start_approval(session, stop_two, name="worker-two")
        stop_one.set()
        worker_one.join(2)
        self.assertTrue(first.done.is_set())
        self.assertFalse(second.done.is_set())
        self.assertTrue(worker_two.is_alive())
        stop_two.set()
        worker_two.join(2)

    def test_concurrent_same_key_calls_once(self):
        from concurrent.futures import ThreadPoolExecutor
        sid, _, control = self.prepare()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.send(sid), range(2)))
        self.assertEqual(sorted(result.status_code for result in results), [200, 202])
        self.assertEqual(len({result.json()["operation"]["operation_id"] for result in results}), 1)
        self.assertEqual(control.call_count, 1)
