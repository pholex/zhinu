"""Application state must survive restarts without overwriting newer or corrupt data."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from labs.desktop.state import DesktopState, StateConflict, install_state_routes


class DesktopStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "state" / "desktop.json"
        self.store = DesktopState(self.path)

    def test_collaboration_creation_receipt_survives_restart(self):
        for workflow in ("weave", "contest", "fleet"):
            with self.subTest(workflow=workflow):
                state = self.store.read()
                receipt = {"kind": "create", "workflow": workflow, "path": "/session",
                           "body": {"workspace": "/tmp/work"}, "key": "original-key", "state": "ready"}
                state["outbox"] = {"create": receipt}
                saved = self.store.save(state)
                self.assertEqual(DesktopState(self.path).read()["outbox"]["create"], receipt)
                for invalid in (None, "", "auto", [], {}, "unknown"):
                    with self.assertRaises(ValueError):
                        self.store.save({**saved, "outbox": {"create": {**receipt, "workflow": invalid}}})
                with self.assertRaises(ValueError):
                    self.store.save({**saved, "outbox": {"create": {**receipt, "kind": "prompt"}}})
                self.assertEqual(self.store.read(), saved)

    def test_view_receipt_identity_roundtrips_and_rejects_invalid_identifiers(self):
        receipt = {"kind": "prompt", "sid": "session", "body": {"text": "draft"}, "key": "original-request", "view_id": "11111111-1111-4111-8111-111111111111"}
        saved = self.store.save({"revision": 0, "drafts": {}, "outbox": {"session:prompt:": receipt}})
        self.assertEqual(DesktopState(self.path).read()["outbox"]["session:prompt:"], receipt)
        for invalid in (None, "", "not-a-view", "x" * 36, 7, [], {}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.store.save({**saved, "outbox": {"session:prompt:": {**receipt, "view_id": invalid}}})
        self.assertEqual(self.store.read(), saved)

    def test_view_namespace_preserves_legacy_key_and_count_limits(self):
        owner = "11111111-1111-4111-8111-111111111111"
        drafts = {f"view:{owner}:" + str(index).zfill(200): "legacy" for index in range(500)}
        drafts["view:legacy-owner"] = owner
        saved = self.store.save({"revision": 0, "drafts": drafts, "outbox": {}})
        self.assertEqual(len(saved["drafts"]), 501)
        for key, value in (("view:legacy-owner", "invalid"), ("x" * 201, "text"), (f"view:{owner}:" + "x" * 201, "text")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.store.save({**saved, "drafts": {key: value}})
        with self.assertRaises(ValueError):
            self.store.save({**saved, "drafts": {**drafts, "extra": "too many"}})
        self.assertEqual(self.store.read(), saved)

    def test_restart_and_revision_conflict_preserve_original(self):
        state = self.store.read()
        state["drafts"]["session"] = "跨应用重启保留草稿"
        saved = self.store.save(state)
        self.assertEqual(DesktopState(self.path).read(), saved)
        self.assertEqual(saved["revision"], 1)
        with self.assertRaises(StateConflict):
            self.store.save({**state, "drafts": {"session": "stale"}})
        self.assertEqual(self.store.read(), saved)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_atomic_replace_failure_keeps_existing_state(self):
        saved = self.store.save(self.store.read())
        with patch("labs.desktop.state.os.replace", side_effect=OSError("disk failed")):
            with self.assertRaises(OSError):
                self.store.save({**saved, "drafts": {"s": "new text"}})
        self.assertEqual(self.store.read(), saved)
        self.assertEqual(list(self.path.parent.glob(".desktop-*")), [])

    def test_write_guard_loss_at_each_boundary_preserves_original_and_cleans_temporary(self):
        original = self.store.save(self.store.read())
        for boundary in (1, 2, 3):
            with self.subTest(boundary=boundary):
                count = 0
                def guard():
                    nonlocal count
                    count += 1
                    if count == boundary:
                        raise OSError("fixture ownership lost")
                store = DesktopState(self.path, write_guard=guard)
                with self.assertRaises(OSError):
                    store.save({**original, "drafts": {"s": "must not replace"}})
                self.assertEqual(store.read(), original)
                self.assertFalse(list(self.path.parent.glob(".desktop-*")))

    def test_weave_receipt_keeps_preview_and_corresponding_draft_after_restart(self):
        state = self.store.read()
        state["drafts"]["weave-compose:s"] = '{"items":"one\\ntwo"}'
        state["outbox"]["weave:s"] = {"kind": "weave", "key": "once", "sid": "s",
            "body": {"items": ["one", "two"], "preview_token": "review-digest"},
            "draftKey": "weave-compose:s", "draftText": state["drafts"]["weave-compose:s"], "state": "unknown"}
        saved = self.store.save(state)
        self.assertEqual(DesktopState(self.path).read(), saved)
        with self.assertRaises(ValueError):
            self.store.save({**saved, "outbox": {"x": {**saved["outbox"]["weave:s"], "draftText": []}}})

    def test_contest_and_selection_receipts_survive_restart_without_credentials(self):
        state = self.store.read()
        state["drafts"]["contest-compose:s"] = '{"task":"比较两种方案"}'
        for kind, body in (("contest", {"preview_token": "digest", "models": ["a", "b"]}),
                           ("selection", {"candidate_id": "c:1", "reason": "理由", "expected_revision": 0})):
            state["outbox"][kind] = {"kind": kind, "key": kind + "-key", "body": body, "state": "unknown"}
        state["outbox"]["selection"]["contestId"] = "contest-1"
        saved = self.store.save(state)
        self.assertEqual(DesktopState(self.path).read(), saved)
        invalid = {**saved, "outbox": {"selection": {**saved["outbox"]["selection"], "contestId": []}}}
        with self.assertRaises(ValueError):
            self.store.save(invalid)

    def test_member_receipts_and_generation_drafts_survive_restart(self):
        state = self.store.read()
        draft_key = "fleet-message:session:board:worker"
        drafts = json.dumps({"old-run": "保留旧任务草稿", "new-run": "当前草稿"}, ensure_ascii=False)
        state["drafts"][draft_key] = drafts
        for kind, suffix, body in (
            ("fleet_message", "messages", {"run_id": "old-run", "text": "补充范围"}),
            ("fleet_stop", "stop", {"run_id": "old-run"}),
        ):
            state["outbox"][kind] = {"kind": kind, "key": kind + "-key", "sid": "session",
                "path": f"/session/session/fleets/board/members/worker/{suffix}",
                "body": body, "state": "unknown", "draftKey": draft_key, "draftText": drafts}
        saved = self.store.save(state)
        self.assertEqual(DesktopState(self.path).read(), saved)
        self.assertEqual(json.loads(saved["drafts"][draft_key])["new-run"], "当前草稿")

    def test_fleet_command_review_and_draft_survive_restart(self):
        state = self.store.read()
        state["drafts"]["fleet-command:s:init"] = '{"base_branch":"main"}'
        state["outbox"]["command"] = {"kind": "fleet_command", "key": "command-key", "sid": "s",
            "path": "/session/s/fleet-commands", "state": "unknown",
            "body": {"action": "init", "mode": "new", "preview_token": "reviewed"},
            "draftKey": "fleet-command:s:init", "draftText": state["drafts"]["fleet-command:s:init"]}
        saved = self.store.save(state)
        self.assertEqual(DesktopState(self.path).read(), saved)

    def test_weave_controls_retain_exact_execution_and_receipt_warning(self):
        state = self.store.read()
        state["outbox"]["one"] = {"kind": "weave_stop", "key": "same-key", "sid": "s",
            "path": "/session/s/weaves/b/items/i/stop", "body": {"execution_id": "exact-child"},
            "state": "accepted", "result": {"operation_id": "op-one", "operation_status": "stop_requested",
                "operation_persisted": False, "operation_warning": "仅本进程核对"}}
        saved = self.store.save(state)
        self.assertEqual(DesktopState(self.path).read(), saved)

    def test_corrupt_state_is_not_replaced_with_an_empty_copy(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"{\xffbroken")
        with self.assertRaises(ValueError):
            self.store.read()
        with self.assertRaises(ValueError):
            self.store.save({"revision": 0, "drafts": {}, "outbox": {}})
        self.assertEqual(self.path.read_bytes(), b"{\xffbroken")

    def test_connection_credentials_are_not_valid_application_fields(self):
        state = self.store.read()
        with self.assertRaises(ValueError):
            self.store.save({**state, "token": "must stay in memory"})
        state["outbox"] = {"p": {"kind": "prompt", "key": "same-key", "body": {"text": "work"},
                                  "token": "must stay in memory"}}
        with self.assertRaises(ValueError):
            self.store.save(state)


class DesktopStateApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_revision_and_corruption_are_explicit(self):
        import httpx
        from fastapi import FastAPI
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "desktop.json"
            app = FastAPI()
            app.state.execution_owner = SimpleNamespace(held=True)
            install_state_routes(app, path, "local-state-token")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                self.assertEqual((await client.get("/desktop/state")).status_code, 401)
                client.headers["Authorization"] = "Bearer local-state-token"
                response = await client.get("/desktop/state")
                self.assertEqual(response.headers["cache-control"], "no-store")
                state = response.json()
                state["drafts"]["s"] = "保留"
                saved = await client.post("/desktop/state", json=state)
                self.assertEqual(saved.json(), {"revision": 1})
                self.assertEqual((await client.post("/desktop/state", json=state)).status_code, 409)
                self.assertNotIn("local-state-token", path.read_text(encoding="utf-8"))
                self.assertEqual((await client.post("/desktop/state", content=b"not json")).status_code, 422)
                path.write_text("broken", encoding="utf-8")
                self.assertEqual((await client.get("/desktop/state")).status_code, 503)
                self.assertEqual((await client.post("/desktop/state", json=state)).status_code, 503)
                self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    async def test_owner_loss_keeps_reads_and_session_fault_does_not_block_safety_receipts(self):
        import httpx
        from fastapi import FastAPI
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "desktop.json"
            app = FastAPI()
            app.state.execution_owner = SimpleNamespace(held=True)
            app.state.sessions = {"s": SimpleNamespace(agent=SimpleNamespace(session_log=SimpleNamespace(locked=False)))}
            install_state_routes(app, path, "local-state-token")
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                    headers={"Authorization": "Bearer local-state-token"}) as client:
                state = {"revision": 0, "drafts": {}, "outbox": {"stop": {
                    "kind": "weave_stop", "key": "precise-stop", "body": {"execution_id": "one"}}}}
                self.assertEqual((await client.post("/desktop/state", json=state)).status_code, 200)
                saved = (await client.get("/desktop/state")).json()
                for owner in (SimpleNamespace(held=False), None):
                    app.state.execution_owner = owner
                    rejected = await client.post("/desktop/state", json={**saved, "drafts": {"s": "new"}})
                    self.assertEqual(rejected.status_code, 503)
                    self.assertIn("所有权", rejected.json()["detail"])
                    self.assertEqual((await client.get("/desktop/state")).json(), saved)
                app.state.execution_owner = SimpleNamespace(held=True)
                self.assertEqual((await client.post("/desktop/state", json={**saved, "drafts": {"s": "new"}})).status_code, 200)


if __name__ == "__main__":
    unittest.main()
