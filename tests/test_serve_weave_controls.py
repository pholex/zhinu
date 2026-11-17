"""HTTP execution identity, durable input, safe stop and approval contracts."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from tests.test_serve import ServeCase
from tests import test_serve_weaves as _weaves
from tests import test_serve_commands as _commands
from xiaoyu.serve_weaves import WeaveView
from xiaoyu.serve_weave_controls import ControlLedger


class WeaveControlTests(ServeCase):
    mcp = False
    token = "weave-controls-token"
    emit = _weaves.WeaveEndpointsTests.emit
    view = _weaves.WeaveEndpointsTests.view

    def prepare(self, **config):
        sid = _commands.CommandTests.boot(self, script="text: unused\n", **config)
        session = self.client.app.state.sessions[sid]
        self.emit(sid, _weaves.batch())
        controller = session.agent._weave_controls
        execution, child = self.generation(controller)
        self.client.portal.call(lambda: None)
        return sid, session, controller, execution, child

    def generation(self, controller, item="batch-1:0", phase="primary", upstream=None):
        execution = controller.begin("batch-1", item, phase, "第一项", threading.Event(), upstream or (lambda: False))
        child = SimpleNamespace(config=SimpleNamespace(model="fixture", workspace=self.root),
                                sink=SimpleNamespace(emit=lambda event: None), _on_turn_input=None, steer=Mock())
        controller.register(execution, child)
        return execution, child

    def send(self, sid, execution, key="one", kind="messages", text="检查边界", **extra):
        eid = execution if isinstance(execution, str) else execution.execution_id
        body = {"execution_id": eid, **extra}
        if kind == "messages":
            body["text"] = text
        return self.client.post(f"/session/{sid}/weaves/batch-1/items/batch-1:0/{kind}",
            headers={**self.headers(), "Idempotency-Key": key}, json=body)

    def item(self, sid):
        return self.view(sid)["batches"][0]["items"][0]

    def test_real_queue_start_consumption_and_durable_before_effect(self):
        sid, session, controller, execution, child = self.prepare()
        original = controller.control
        def checked(*args, **kwargs):
            self.assertIn('"status": "reserved"', session.agent.session_log.path.read_text(encoding="utf-8"))
            return original(*args, **kwargs)
        with patch.object(controller, "control", side_effect=checked):
            response = self.send(sid, execution)
        self.assertEqual(response.status_code, 202, response.text)
        operation = response.json()["operation"]
        self.assertEqual(operation["status"], "queued")
        self.assertTrue(operation["persisted"])
        child.steer.assert_not_called()
        child._on_turn_input()
        child.steer.assert_called_once_with("检查边界")
        controller._accepted(execution, "检查边界")
        self.assertEqual(self.item(sid)["operations"][0]["status"], "accepted")
        self.assertEqual(self.send(sid, execution).json()["operation"]["status"], "accepted")

    def test_stop_requested_is_not_settled_and_never_targets_followup(self):
        sid, _, controller, execution, _ = self.prepare()
        response = self.send(sid, execution, kind="stop")
        self.assertEqual(response.json()["operation"]["status"], "requested")
        self.assertTrue(execution.stop.is_set())
        controller.finish(execution)
        self.assertEqual(self.item(sid)["operations"][0]["status"], "settled")
        second, _ = self.generation(controller, phase="follow_up")
        retry = self.send(sid, execution, kind="stop").json()
        self.assertTrue(retry["duplicate"])
        self.assertEqual(retry["operation"]["status"], "settled")
        self.assertFalse(second.stop.is_set())
        self.assertEqual(self.item(sid)["operations"], [])
        self.assertEqual(self.send(sid, execution, kind="stop", key="old-new-key").status_code, 409)
        self.assertEqual(self.send(sid, second, kind="stop").status_code, 409)

    def test_restart_preserves_consumed_and_marks_pending_unknown_without_replay(self):
        state = Path(self.tmp) / "state"
        sid, _, controller, execution, _ = self.prepare(state_dir=state)
        self.send(sid, execution)
        controller._accepted(execution, "检查边界")
        self.send(sid, execution, key="unconsumed", text="未消费")
        self.item(sid)
        self.client.__exit__(None, None, None)
        # Recovery projections are read-only; a service in this same process
        # cannot take over a still-live generation's retained execution lease.
        from xiaoyu.serve_weave_controls import ControlLedger
        from xiaoyu.serve_weaves import WeaveView
        from xiaoyu.session_log import SessionLockedError
        from xiaoyu.serve_ownership import StateOwner
        with self.assertRaises(SessionLockedError):
            StateOwner(state, True)
        log = self.client.app.state.sessions[sid].agent.session_log.path
        ledger = ControlLedger(); ledger.restore(log)
        self.assertEqual(ledger.operations[ledger.keys["one"]]["status"], "accepted")
        self.assertEqual(ledger.operations[ledger.keys["unconsumed"]]["status"], "unknown")
        view = WeaveView(); view.restore(log)
        item = view.snapshot()[0]["items"][0]
        self.assertTrue(item["recovered"])
        self.assertEqual(item["execution_state"], "unknown")

    def test_message_reservation_failure_no_effect_safe_stop_still_effects(self):
        sid, session, controller, execution, _ = self.prepare()
        with patch.object(session.agent.session_log, "commit_event", side_effect=OSError("disk full")):
            response = self.send(sid, execution)
            self.assertEqual(response.status_code, 503)
            self.assertEqual(controller.snapshot("batch-1", "batch-1:0")["controls"], [])
            retry = self.send(sid, execution).json()["operation"]
            self.assertEqual(retry["status"], "unknown")
            self.assertFalse(retry["persisted"])
            item = self.item(sid)
            self.assertFalse(item["controls"]["can_message"])
            self.assertTrue(item["controls"]["can_stop"])
            stop = self.send(sid, execution, key="stop", kind="stop").json()["operation"]
            self.assertEqual(stop["status"], "requested")
            self.assertFalse(stop["persisted"])
            self.assertTrue(stop["warning"])
            self.assertTrue(execution.stop.is_set())
        retry = self.send(sid, execution, key="stop", kind="stop").json()["operation"]
        self.assertEqual(retry["operation_id"], stop["operation_id"])
        self.assertEqual(retry["status"], "requested")
        self.assertFalse(retry["persisted"])

    def test_nonpersistent_safe_stop_but_no_messages(self):
        sid, _, _, execution, _ = self.prepare(persist=False)
        self.assertEqual(self.send(sid, execution).status_code, 409)
        operation = self.send(sid, execution, key="stop", kind="stop").json()["operation"]
        self.assertFalse(operation["persisted"])
        self.assertTrue(execution.stop.is_set())

    def test_outcome_write_failure_message_unknown_and_read_only_retry(self):
        sid, session, controller, execution, _ = self.prepare()
        commit = session.agent.session_log.commit_event
        count = 0
        def failing(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("full")
            return commit(*args, **kwargs)
        with patch.object(session.agent.session_log, "commit_event", side_effect=failing):
            result = self.send(sid, execution).json()["operation"]
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["persisted"])
        self.assertTrue(self.send(sid, execution).json()["duplicate"])
        self.assertEqual(len(controller.snapshot("batch-1", "batch-1:0")["controls"]), 1)

    def test_validation_auth_other_session_and_old_receipt_lookup(self):
        sid, _, _, execution, _ = self.prepare()
        self.assertEqual(self.send(sid, execution, key="").status_code, 400)
        self.assertEqual(self.send(sid, execution, text="  ").status_code, 400)
        self.assertEqual(self.send(sid, execution, run_id="fake").status_code, 400)
        self.assertEqual(self.send(self.new_session(), execution).status_code, 409)
        operation = self.send(sid, execution).json()["operation"]
        url = f'/session/{sid}/weave-operations/{operation["operation_id"]}'
        self.assertEqual(self.client.get(url).status_code, 401)
        self.assertEqual(self.client.get(url, headers=self.headers()).json()["operation"]["operation_id"], operation["operation_id"])
        self.assertEqual(self.send(sid, execution, text="不同").status_code, 409)
        self.assertEqual(self.client.get("/health", headers=self.headers()).json()["weave_control_protocol"], 1)
        self.assertNotIn("weave_control_protocol", self.client.get("/health").json())

    def test_concurrent_same_key_reserves_once(self):
        from concurrent.futures import ThreadPoolExecutor
        sid, _, controller, execution, _ = self.prepare()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.send(sid, execution), range(2)))
        self.assertEqual(sorted(r.status_code for r in results), [200, 202])
        self.assertEqual(len(controller.snapshot("batch-1", "batch-1:0")["controls"]), 1)

    def test_execution_facts_win_late_batch_and_old_generation_events(self):
        sid, session, controller, first, _ = self.prepare()
        self.send(sid, first)
        controller.finish(first)
        second, _ = self.generation(controller, phase="follow_up")
        controller._publish(first)
        late = _weaves.batch(revision=99)
        late["items"][0].update(execution_id=first.execution_id, execution_state="running", workspace="primary-worktree", model="primary-model")
        session.in_flight = {"id": "unrelated-new-run", "command": {"operation": "resume", "source_batch_id": "unrelated-source", "item_ids": ["other"]}}
        self.emit(sid, late)
        session.in_flight = None
        item = self.item(sid)
        self.assertEqual(item["execution_id"], second.execution_id)
        self.assertEqual(item["workspace"], "primary-worktree")
        self.assertEqual(item["execution_workspace"], str(self.root))
        self.assertEqual(item["model"], "primary-model")
        self.assertEqual(item["execution_model"], "fixture")
        self.assertTrue(item["live"])
        self.assertEqual(item["operations"], [])
        self.assertNotEqual(self.view(sid)["batches"][0]["source_run_id"], "unrelated-new-run")
        self.assertNotIn("source_batch_id", self.view(sid)["batches"][0])
        old = self.send(sid, first).json()["operation"]
        self.assertEqual(old["status"], "not_consumed")

    def test_approval_identity_one_time_only_and_stop_wakes_exact_instance(self):
        sid, session, controller, first, _ = self.prepare()
        second, _ = self.generation(controller, item="batch-1:1")
        results = []
        threads = []
        for execution in (first, second):
            thread = threading.Thread(target=lambda e=execution: results.append(controller.approve(
                e, "write_file", {"path": "a.txt", "content": "content"}, lambda *a: False)))
            thread.start()
            threads.append(thread)
        self.addCleanup(lambda: [t.join(2) for t in threads])
        self.addCleanup(lambda: [e.stop.set() for e in (first, second)])
        for _ in range(200):
            pending = session.snapshot_pending()
            if len(pending) == 2:
                break
            time.sleep(.01)
        self.assertEqual(len(pending), 2)
        request = next(p for p in pending if p.identity["execution_id"] == first.execution_id)
        self.assertEqual(request.identity["kind"], "qixiang")
        self.assertEqual(request.to_dict()["allowed_decisions"], ["allow", "deny"])
        invalid = self.client.post(f"/session/{sid}/permissions", headers=self.headers(),
            json={"request_id": request.id, "decision": "allow_session"})
        self.assertEqual(invalid.status_code, 400)
        self.send(sid, first, kind="stop")
        threads[0].join(2)
        self.assertFalse(threads[0].is_alive())
        self.assertTrue(threads[1].is_alive())
        self.assertEqual(len(session.snapshot_pending()), 1)
        second.stop.set()
        threads[1].join(2)

    def test_journal_truncation_is_not_integrity_failure_but_bad_records_are(self):
        sid, session, _, _, _ = self.prepare()
        for index in range(70):
            self.emit(sid, _weaves.batch(f"other-{index}", final=True))
        self.assertTrue(session.weaves.integrity)
        self.assertTrue(self.view(sid)["truncated"])
        self.assertTrue(self.view(sid)["persisted"])
        path = Path(self.tmp) / "bad.jsonl"
        path.write_text('{"event":', encoding="utf-8")
        view, ledger = WeaveView(), ControlLedger()
        view.restore(path)
        ledger.restore(path)
        self.assertFalse(view.integrity)
        self.assertFalse(ledger.integrity)

    def test_execution_write_failure_marks_visible_operation_not_persisted(self):
        sid, session, controller, execution, _ = self.prepare()
        self.send(sid, execution)
        with patch.object(session.agent.session_log, "commit_event", side_effect=OSError("full")):
            controller._accepted(execution, "检查边界")
            item = self.item(sid)
        self.assertEqual(item["operations"][0]["status"], "accepted")
        self.assertFalse(item["operations"][0]["persisted"])
        self.assertFalse(item["controls"]["can_message"])
        self.assertTrue(item["controls"]["can_stop"])

    def test_message_limit_does_not_block_one_precise_stop(self):
        sid, _, _, execution, _ = self.prepare()
        with patch("xiaoyu.serve_weave_controls.MAX_OPERATIONS", 1):
            self.assertEqual(self.send(sid, execution).status_code, 202)
            self.assertEqual(self.send(sid, execution, key="another").status_code, 409)
            self.assertEqual(self.send(sid, execution, key="stop", kind="stop").status_code, 202)
            self.assertEqual(self.send(sid, execution, key="stop-again", kind="stop").status_code, 409)
            self.assertTrue(self.send(sid, execution, key="stop", kind="stop").json()["duplicate"])

    def test_stale_execution_revisions_do_not_regress_receipts(self):
        sid, session, _, execution, _ = self.prepare()
        operation = self.send(sid, execution).json()["operation"]
        event = {"kind": "qixiang.execution.updated", "batch_id": "batch-1", "item_id": "batch-1:0",
                 "execution_id": execution.execution_id, "execution_phase": "primary",
                 "execution_state": "running", "revision": 100, "controls": [{**operation, "status": "accepted"}]}
        self.emit(sid, event)
        self.emit(sid, {**event, "revision": 99, "controls": [{**operation, "status": "queued"}]})
        self.assertEqual(self.send(sid, execution).json()["operation"]["status"], "accepted")
        restored = ControlLedger()
        restored.restore(session.agent.session_log.path)
        self.assertEqual(restored.operations[operation["operation_id"]]["status"], "accepted")

    def test_shutdown_without_questions_interrupts_real_batch_and_wakes_approval(self):
        import os
        from tests.test_chenshu import git
        state = Path(self.tmp) / "state"
        script = 'tool_call: {"name":"write_file","arguments":{"path":"result.txt","content":"fixture"}}\n---\ntext: ' + _commands.LONG + "\n"
        self.start(script, state_dir=state)
        os.environ["XIAOYU_ENABLE_AGENTS"] = "1"
        os.environ["XIAOYU_QIXIANG_CONCURRENCY"] = "1"
        self.addCleanup(os.environ.pop, "XIAOYU_QIXIANG_CONCURRENCY", None)
        directory = self.root / ".xiaoyu" / "agents"
        directory.mkdir(parents=True)
        (directory / "writer.toml").write_text('description = "写入夹具"\nsystem_prompt = "完成任务"\ntools = ["write_file"]\n', encoding="utf-8")
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "README.md").write_text("fixture", encoding="utf-8")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-qm", "fixture")
        sid = self.new_session()
        session = self.client.app.state.sessions[sid]
        self.assertIsNone(session.questions)
        (self.root / "README.md").write_text("fixture", encoding="utf-8")
        body = {"spec": "writer", "prompt_template": "{{item}}", "items": ["甲", "乙"]}
        preview = self.client.post(f"/session/{sid}/weaves/preview", headers=self.headers(), json=body)
        self.assertEqual(preview.status_code, 200, preview.text)
        response = self.client.post(f"/session/{sid}/weaves", headers={**self.headers(), "Idempotency-Key": "batch"},
            json={**body, "preview_token": preview.json()["preview_token"]})
        self.assertEqual(response.status_code, 202, response.text)
        for _ in range(300):
            pending = session.snapshot_pending()
            if pending and pending[0].identity:
                break
            time.sleep(.01)
        else:
            self.fail(f"real child did not request approval: {self.status(sid)!r}; {self.events(sid)[-8:]!r}")
        identity = pending[0].identity
        self.assertEqual(identity["kind"], "qixiang")
        self.client.__exit__(None, None, None)
        self.assertTrue(pending[0].done.is_set())
        self.assertTrue(session.agent.interrupt_requested())
        self.assertFalse(session._task and not session._task.done())
        from xiaoyu.serve_ownership import StateOwner
        from xiaoyu.session_log import SessionLockedError
        with self.assertRaises(SessionLockedError):
            StateOwner(state, True)
        self.assertTrue(session.agent.session_log.locked)

    def test_malformed_batch_or_execution_is_not_exposed_as_valid_live_state(self):
        sid, session, _, execution, _ = self.prepare()
        self.emit(sid, {**_weaves.batch(revision=50), "items": [{}]})
        self.assertFalse(session.weaves.integrity)
        item = self.item(sid)
        self.assertFalse(item["controls"]["can_message"])
        self.assertTrue(item["controls"]["can_stop"])
        self.emit(sid, {"kind": "qixiang.execution.updated", "execution_id": execution.execution_id,
                       "batch_id": "batch-1", "item_id": "batch-1:0", "revision": 100,
                       "execution_phase": "primary", "execution_state": "running", "controls": [None]})
        self.assertTrue(self.item(sid)["live"])
        self.assertFalse(self.view(sid)["persisted"])
