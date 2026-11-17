"""Session-scoped orchestration observations never adopt workspace runtimes."""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.test_serve import ServeCase
from xiaoyu import serve_fleets


def fleet(board_id="board-one", revision=1):
    return {"kind": "chenshu.updated", "board_id": board_id, "revision": revision,
            "active": True, "base_branch": "main", "observed_at": 1000 + revision,
            "source": {"runtime_id": board_id, "state_id": "shared-protocol-state", "origin": "new",
                       "workspace": "/fixture/project", "protocol_root": "/fixture/project/.xiaoyu/chenshu"},
            "missions": [{"id": "mission-one", "title": "实现模块", "kind": "build", "scope": ["src/a.py"],
                          "deps": [], "status": "active", "owner": "worker-one", "branch": "member-one",
                          "worktree": "/fixture/worker", "notes": ["独立分工"]}],
            "members": [{"name": "worker-one", "kind": "worker", "mission_id": "mission-one", "review_target": "",
                         "status": "running", "execution_state": "running", "execution_outcome": "running",
                         "thread_alive": True, "run_id": "worker-run-one", "model": "fixture-model", "effort": "",
                         "handoff": "", "handoff_truncated": False, "stop_reason": "", "error": ""}],
            "reviews": [{"id": "review-one", "mission_id": "mission-one", "from": "reviewer-one", "round": 1,
                         "verdict": "approve", "summary": "已核对所记录版本", "reviewed_commit": "recorded-commit",
                         "recorded_at": 1000, "commit_check": "not_rechecked"}],
            "findings": [{"id": "finding-one", "from": "worker-one", "mission_id": "mission-one", "kind": "note",
                          "title": "发现", "body": "仍需处理验收边界", "recorded_at": 1000}],
            "activity": [{"id": "activity-one", "at": 1000, "actor": "worker-one", "action": "started", "details": {"mission_id": "mission-one"}}],
            "limits": {"missions": 0, "members": 0, "reviews": 0, "findings": 0, "activity": 0}, "gaps": []}


class FleetEndpointsTests(ServeCase):
    mcp = False
    token = "fleet-fixture-token"

    def boot(self, enabled=True, script=None, **config):
        self.start(script or "text: unused\n", **config)
        previous = os.environ.get("XIAOYU_ENABLE_CHENSHU")
        os.environ["XIAOYU_ENABLE_CHENSHU"] = "1" if enabled else "0"
        self.addCleanup(lambda: os.environ.pop("XIAOYU_ENABLE_CHENSHU", None) if previous is None
                        else os.environ.__setitem__("XIAOYU_ENABLE_CHENSHU", previous))
        return self.new_session()

    def emit(self, sid, payload):
        session = self.client.app.state.sessions[sid]
        self.client.portal.call(session.publish_event, SimpleNamespace(to_dict=lambda: copy.deepcopy(payload)))

    def view(self, sid, **params):
        response = self.client.get(f"/session/{sid}/fleets", params=params, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_empty_and_disabled_views_do_not_read_or_adopt_protocol_state(self):
        sid = self.boot()
        session = self.client.app.state.sessions[sid]
        runtime = session.agent.chenshu
        protocol = self.root / ".xiaoyu" / "chenshu"
        protocol.mkdir(parents=True)
        state = protocol / "state.json"
        state.write_text('{"private":"must not be adopted"}', encoding="utf-8")
        before = state.read_bytes()
        with patch.object(runtime, "init", side_effect=AssertionError("GET must not init")), \
                patch.object(runtime, "_load", side_effect=AssertionError("GET must not load protocol")), \
                patch.object(runtime, "status", side_effect=AssertionError("GET must not inspect git")):
            view = self.view(sid)
        self.assertTrue(view["available"])
        self.assertEqual(view["fleets"], [])
        self.assertIn("尚无", view["reason"])
        self.assertFalse(runtime.active)
        self.assertEqual(state.read_bytes(), before)
        self.assertEqual(sorted(path.name for path in protocol.iterdir()), ["state.json"])
        os.environ["XIAOYU_ENABLE_CHENSHU"] = "0"
        disabled = self.new_session()
        disabled_view = self.view(disabled)
        self.assertFalse(disabled_view["available"])
        self.assertIn("未启用", disabled_view["reason"])
        self.assertEqual(disabled_view["fleets"], [])

    def test_full_snapshots_outlive_clipped_events_and_preserve_first_origin(self):
        sid = self.boot(buffer_limit=2, max_field=80)
        session = self.client.app.state.sessions[sid]
        session.in_flight = {"id": "initial-parent-run"}
        self.emit(sid, fleet())
        session.in_flight = {"id": "unrelated-parent-run"}
        updated = fleet(revision=2)
        updated["members"][0]["handoff"] = "完整交接正文" * 300
        updated["gaps"] = ["历史发现尚未纳入快照"]
        updated["limits"]["findings"] = 5
        self.emit(sid, updated)
        session.in_flight = None
        self.emit(sid, fleet("second"))
        self.emit(sid, fleet("third"))
        page = self.view(sid, cursor=2, limit=1)
        row = page["fleets"][0]
        self.assertEqual((page["total"], page["next_cursor"]), (3, None))
        self.assertEqual(row["source_run_id"], "initial-parent-run")
        self.assertEqual(row["members"][0]["handoff"], updated["members"][0]["handoff"])
        self.assertEqual(row["gaps"], updated["gaps"])
        self.assertEqual(row["limits"]["findings"], 5)
        self.assertTrue(row["live"])
        self.assertFalse(row["recovered"])
        self.assertEqual(row["runtime_state"], "observed")
        self.assertTrue(page["persisted"])
        self.assertEqual(row["reviews"][0]["commit_check"], "not_rechecked")
        self.assertFalse(session.busy)
        self.assertTrue(row["members"][0]["thread_alive"])

    def test_restart_marks_only_unsettled_execution_unknown_and_keeps_original_facts(self):
        state_dir = Path(self.tmp) / "state"
        sid = self.boot(state_dir=state_dir)
        payload = fleet()
        settled = copy.deepcopy(payload["members"][0])
        settled.update(name="settled-member", status="done", execution_state="settled", execution_outcome="completed", thread_alive=True, handoff="可靠交接")
        payload["members"].append(settled)
        self.emit(sid, payload)
        self.client.__exit__(None, None, None)
        self.start("text: unused\n", state_dir=state_dir)
        view = self.view(sid)
        row = view["fleets"][0]
        self.assertFalse(row["live"])
        self.assertTrue(row["recovered"])
        self.assertEqual(row["runtime_state"], "unknown_after_restart")
        self.assertTrue(row["active"])
        member = row["members"][0]
        self.assertEqual(member["status"], "running")
        self.assertEqual(member["execution_state"], "unknown")
        self.assertEqual(member["execution_outcome"], "unknown")
        self.assertIsNone(member["thread_alive"])
        self.assertEqual(member["recorded_execution_state"], "running")
        self.assertTrue(member["recorded_thread_alive"])
        self.assertEqual(row["members"][1]["execution_outcome"], "completed")
        self.assertEqual(row["members"][1]["execution_state"], "settled")
        self.assertTrue(row["members"][1]["recorded_thread_alive"])
        self.assertIsNone(row["members"][1]["thread_alive"])
        self.assertEqual(row["reviews"], payload["reviews"])
        self.assertIn("历史", view["reason"])
        self.assertFalse(self.client.app.state.sessions[sid].agent.chenshu.active)

    def test_runtime_adoption_is_separate_board_with_explicit_common_state(self):
        sid = self.boot()
        self.emit(sid, fleet("older-runtime"))
        adopted = fleet("new-runtime")
        adopted["source"]["origin"] = "adopted"
        self.emit(sid, adopted)
        rows = self.view(sid)["fleets"]
        self.assertEqual([row["board_id"] for row in rows], ["new-runtime", "older-runtime"])
        self.assertEqual(rows[0]["source"]["state_id"], rows[1]["source"]["state_id"])
        self.assertEqual(rows[0]["source"]["origin"], "adopted")

    def test_failed_persistence_keeps_observation_with_warning_and_false_persisted(self):
        sid = self.boot()
        session = self.client.app.state.sessions[sid]
        with patch.object(session.agent.session_log, "commit_event", side_effect=OSError("disk unavailable")):
            self.emit(sid, fleet())
        view = self.view(sid)
        self.assertEqual(len(view["fleets"]), 1)
        self.assertFalse(view["persisted"])
        self.assertIn("保存失败", view["warning"])
        self.assertTrue(view["fleets"][0]["live"])

    def test_get_is_session_scoped_and_all_writes_are_absent(self):
        sid = self.boot()
        other = self.new_session()
        self.emit(sid, fleet())
        self.assertEqual(self.view(other)["fleets"], [])
        self.assertEqual(self.client.get(f"/session/{sid}/fleets").status_code, 401)
        self.assertEqual(self.client.get("/session/missing/fleets", headers=self.headers()).status_code, 404)
        for path in (f"/session/{sid}/fleets", f"/session/{sid}/fleets/init", f"/session/{sid}/fleets/board-one/stop"):
            self.assertIn(self.client.post(path, json={}, headers=self.headers()).status_code, (404, 405))
        self.assertEqual(self.client.get(f"/session/{sid}/fleets?limit=65", headers=self.headers()).status_code, 422)
        self.assertEqual(self.client.get(f"/session/{sid}/fleets?cursor=-1", headers=self.headers()).status_code, 422)
        self.assertNotIn("fleet_protocol", self.client.get("/health").json())
        self.assertEqual(self.client.get("/health", headers=self.headers()).json()["fleet_protocol"], 1)

    def test_disabled_capability_does_not_hide_saved_history(self):
        sid = self.boot(enabled=False)
        self.emit(sid, fleet())
        view = self.view(sid)
        self.assertFalse(view["available"])
        self.assertEqual(len(view["fleets"]), 1)
        self.assertIn("历史", view["reason"])


    def test_real_worker_survives_parent_idle_and_aborted_wait_then_settles(self):
        import shutil
        import threading
        import time
        from tests.test_agent_paths import FakeClient, chunk
        from tests.test_chenshu import git
        from xiaoyu.providers import Registry
        if shutil.which("git") is None:
            self.skipTest("git is required")

        def call(name, arguments):
            return "tool_call: " + json.dumps({"name": name, "arguments": arguments}) + "\n"

        script = "---\n".join([
            call("chenshu_init", {}),
            call("chenshu_plan", {"missions": [{"title": "独立调研", "kind": "survey"}]}),
            call("chenshu_spawn", {"name": "researcher", "kind": "worker", "mission_id": "M1"}),
            "text: 本轮登记结束。\n",
            call("chenshu_wait", {"timeout": 60}),
        ])
        self.approval = "allow_all"
        sid = self.boot(script=script)
        git(self.root, "init", "-q", "-b", "main")
        (self.root / "README.md").write_text("isolated orchestration fixture\n", encoding="utf-8")
        git(self.root, "add", "README.md")
        git(self.root, "commit", "-qm", "fixture")
        session = self.client.app.state.sessions[sid]
        runtime = session.agent.chenshu
        entered, release = threading.Event(), threading.Event()
        child_client = FakeClient([[chunk(content="已核对仓库边界并整理独立调研结论。" * 20)]])
        create = child_client.completions.create

        def gated(**kwargs):
            entered.set()
            if not release.wait(10):
                raise RuntimeError("test did not release worker")
            return create(**kwargs)

        child_client.completions.create = gated
        runtime.registry = Registry.for_client(child_client)
        try:
            response = self.client.post(f"/session/{sid}/prompt", json={"text": "分派一次只读调研"}, headers=self.headers())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(entered.wait(3))
            self.assertFalse(self.status(sid)["busy"])
            row = self.view(sid)["fleets"][0]
            self.assertEqual(row["members"][0]["execution_state"], "running")
            self.assertTrue(row["members"][0]["thread_alive"])
            origin = row["source_run_id"]
            self.assertEqual(origin, session.runs[0]["id"])
            response = self.client.post(f"/session/{sid}/prompt_async", json={"text": "等待成员完成"}, headers=self.headers())
            self.assertEqual(response.status_code, 202, response.text)
            for _ in range(300):
                if any(event.get("kind") == "tool.pending" and event.get("name") == "chenshu_wait" for event in self.events(sid)):
                    break
                time.sleep(.01)
            else:
                self.fail("parent did not enter the wait tool")
            self.client.post(f"/session/{sid}/abort", json={"turn": 2}, headers=self.headers())
            self._wait_for(sid, "interrupted")
            row = self.view(sid)["fleets"][0]
            self.assertEqual(row["members"][0]["execution_state"], "running")
            self.assertTrue(runtime._threads["researcher"].is_alive())
            self.assertEqual(row["source_run_id"], origin)
        finally:
            release.set()
            for thread in runtime._threads.values():
                thread.join(timeout=5)
        for _ in range(300):
            row = self.view(sid)["fleets"][0]
            if row["members"][0]["execution_state"] == "settled":
                break
            time.sleep(.01)
        else:
            self.fail("worker outcome did not reach session observation after parent idle")
        self.assertTrue(row["live"])
        self.assertEqual(row["members"][0]["execution_outcome"], "completed")
        self.assertEqual(row["source_run_id"], origin)
        self.assertFalse(self.status(sid)["busy"])


class FleetJournalTests(unittest.TestCase):
    def test_bad_tail_and_invalid_records_report_integrity_without_losing_good_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "log.jsonl"
            payload = {**fleet(), "source_run_id": "parent-run"}
            path.write_bytes((json.dumps({"event": serve_fleets.JOURNAL_EVENT, "fleet": payload})
                              + '\ninvalid\n{"role":"user","content":"').encode())
            view = serve_fleets.FleetView()
            view.restore(path)
            self.assertEqual(len(view.snapshot()), 1)
            self.assertFalse(view.integrity)
            self.assertTrue(view.warning)
            self.assertFalse(view.snapshot()[0]["live"])
            self.assertFalse(view.accept({**fleet("bad"), "members": "wrong"}))
            self.assertFalse(view.accept({**fleet("bad"), "active": "true"}))
            self.assertFalse(view.accept({**fleet("bad"), "revision": True}))

    def test_stale_snapshots_do_not_replace_latest_and_board_limit_is_explicit(self):
        view = serve_fleets.FleetView()
        self.assertTrue(view.accept(fleet(revision=5)))
        self.assertFalse(view.accept(fleet(revision=4)))
        self.assertFalse(view.accept(fleet(revision=5)))
        with patch.object(serve_fleets, "MAX_FLEETS", 2):
            view.accept(fleet("second"))
            view.accept(fleet("third"))
        self.assertEqual([item["board_id"] for item in view.snapshot()], ["third", "second"])
        self.assertTrue(view.integrity)
        self.assertTrue(view.truncated)
        self.assertIn("仅显示", view.warning_text())
        snapshot = view.snapshot()
        snapshot[0]["members"].clear()
        self.assertEqual(len(view.snapshot()[0]["members"]), 1)
