"""Real Git diffs, controlled merge and separate safe cleanup through HTTP."""
from __future__ import annotations

from pathlib import Path

from tests import test_serve_fleet_commands_http as _commands
from tests.test_serve import ServeCase
from tests.test_chenshu import git, LONG, text_turn
from tests.test_agent_paths import FakeClient
from xiaoyu.providers import Registry


class FleetReviewHttpTests(ServeCase):
    mcp = False
    token = "fleet-review-fixture"
    boot = _commands.FleetCommandHttpTests.boot
    caps = _commands.FleetCommandHttpTests.caps
    preview = _commands.FleetCommandHttpTests.preview
    submit = _commands.FleetCommandHttpTests.submit
    wait = _commands.FleetCommandHttpTests.wait
    init = _commands.FleetCommandHttpTests.init
    command = _commands.FleetCommandHttpTests.command

    def prepare_delivery(self, *, review=True, **config):
        sid = self.boot(**config)
        git(self.root, "config", "user.email", "fixture@example.invalid")
        git(self.root, "config", "user.name", "Fixture")
        session = self.client.app.state.sessions[sid]
        runtime = session.agent.chenshu
        runtime.registry = Registry.for_client(FakeClient([text_turn(LONG)]))
        self.init(sid)
        board = self.caps(sid)["board_id"]
        self.command(sid, {"action": "plan", "board_id": board,
            "missions": [{"title": "实现模块", "kind": "build", "scope": ["src/"], "deps": []}]}, "plan")
        self.command(sid, {"action": "spawn", "board_id": board, "name": "worker",
                           "kind": "worker", "mission_id": "M1"}, "spawn")
        runtime._threads["worker"].join(5)
        self.assertFalse(runtime._threads["worker"].is_alive())
        mission = runtime.mission("M1")
        path = Path(mission.worktree)
        (path / "src").mkdir(exist_ok=True)
        (path / "src" / "answer.txt").write_text("delivered content\n", encoding="utf-8")
        git(path, "add", "src/answer.txt")
        git(path, "commit", "-qm", "deliver fixture")
        mission.status = "completed"
        runtime._save()
        if review:
            runtime.submit_review("chenshu", "M1", "clean", "已核对本次提交。")
        runtime._publish_snapshot()
        return sid, runtime, board, path

    def changes(self, sid, board, mission="M1"):
        return self.client.get(f"/session/{sid}/fleets/{board}/missions/{mission}/changes", headers=self.headers())

    def approve_command(self, sid, body, key, tool):
        if tool == "chenshu_teardown":
            from xiaoyu.permissions import Permissions, parse_rule
            self.client.app.state.sessions[sid].agent.permissions = Permissions(
                self.root, [parse_rule("ask chenshu_teardown")])
        request = self.preview(sid, body)
        response = self.submit(sid, request, key)
        self.assertEqual(response.status_code, 202, response.text)
        pending = self._wait_for(sid, "waiting_for_approval")["pending_approvals"][0]
        self.assertEqual(pending["tool"], tool)
        self.client.post(f"/session/{sid}/permissions", headers=self.headers(),
                         json={"request_id": pending["request_id"], "decision": "allow"})
        return request, self.wait(sid)

    def test_current_diff_then_merge_then_explicit_cleanup_preserves_branch_archive(self):
        sid, runtime, board, path = self.prepare_delivery()
        branch = runtime.mission("M1").branch
        before = git(self.root, "rev-parse", "HEAD")
        response = self.changes(sid, board)
        self.assertEqual(response.status_code, 200, response.text)
        facts = response.json()
        self.assertTrue(facts["can_merge"], facts)
        self.assertTrue(facts["merge"]["review"]["current"])
        self.assertIn("delivered content", facts["merge"]["diff"]["text"])
        self.assertEqual(facts["merge"]["conflict_check"], "not_checked")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), before)
        self.assertFalse((self.root / "src" / "answer.txt").exists())
        reviewed, state = self.approve_command(sid, {"action": "merge", "board_id": board,
            "mission_id": "M1"}, "merge-once", "chenshu_merge")
        self.assertEqual(state["last_result"]["stopped"], "done", state)
        self.assertEqual((self.root / "src" / "answer.txt").read_text(), "delivered content\n")
        self.assertTrue(path.is_dir(), "merge must not implicitly remove a worktree")
        merged_head = git(self.root, "rev-parse", "HEAD")
        self.assertTrue(self.submit(sid, reviewed, "merge-once").json()["duplicate"])
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), merged_head)
        _, state = self.approve_command(sid, {"action": "teardown", "board_id": board}, "cleanup", "chenshu_teardown")
        self.assertEqual(state["last_result"]["stopped"], "done", state)
        self.assertFalse(path.exists())
        self.assertFalse(runtime.active)
        self.assertEqual(runtime.mission("M1").worktree, "")
        self.assertTrue(runtime._archive_path("worker").is_file())
        self.assertTrue(git(self.root, "rev-parse", "--verify", f"refs/heads/{branch}"))

    def test_missing_and_stale_review_return_readable_blockers_without_execution(self):
        sid, runtime, board, path = self.prepare_delivery(review=False)
        facts = self.changes(sid, board).json()
        self.assertFalse(facts["can_merge"], facts)
        self.assertTrue(facts["blockers"])
        response = self.client.post(f"/session/{sid}/fleet-commands/preview", headers=self.headers(),
            json={"action": "merge", "board_id": board, "mission_id": "M1"})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertFalse(preview["can_execute"])
        request = {"action": "merge", "board_id": board, "mission_id": "M1", "preview_token": preview["preview_token"]}
        self.assertEqual(self.submit(sid, request, "blocked").status_code, 409)
        runtime.submit_review("chenshu", "M1", "clean", "已核对。")
        (path / "src" / "answer.txt").write_text("new tip\n", encoding="utf-8")
        git(path, "add", "src/answer.txt")
        git(path, "commit", "-qm", "new tip")
        facts = self.changes(sid, board).json()
        self.assertFalse(facts["can_merge"])
        self.assertFalse(facts["merge"]["review"]["current"])

    def test_merge_approval_revalidates_tip_and_denial_preserves_checkout(self):
        sid, runtime, board, path = self.prepare_delivery()
        before = git(self.root, "rev-parse", "HEAD")
        body = self.preview(sid, {"action": "merge", "board_id": board, "mission_id": "M1"})
        self.submit(sid, body, "changed-tip")
        pending = self._wait_for(sid, "waiting_for_approval")["pending_approvals"][0]
        (path / "src" / "answer.txt").write_text("changed after preview\n", encoding="utf-8")
        git(path, "add", "src/answer.txt")
        git(path, "commit", "-qm", "changed after preview")
        self.client.post(f"/session/{sid}/permissions", headers=self.headers(),
                         json={"request_id": pending["request_id"], "decision": "allow"})
        self.assertEqual(self.wait(sid)["last_result"]["stopped"], "tool_failed")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), before)
        self.assertNotEqual(runtime.mission("M1").status, "merged")

    def test_cleanup_dirty_and_ignored_block_all_and_force_is_not_accepted(self):
        sid, runtime, board, path = self.prepare_delivery()
        (path / "src" / "untracked.txt").write_text("retain me\n", encoding="utf-8")
        response = self.client.post(f"/session/{sid}/fleet-commands/preview", headers=self.headers(),
                                   json={"action": "teardown", "board_id": board})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["can_execute"])
        self.assertTrue(response.json()["blockers"])
        self.assertTrue(path.exists())
        force = self.client.post(f"/session/{sid}/fleet-commands/preview", headers=self.headers(),
                                json={"action": "teardown", "board_id": board, "force": True})
        self.assertEqual(force.status_code, 400)
        (path / "src" / "untracked.txt").unlink()
        exclude = Path(git(path, "rev-parse", "--git-path", "info/exclude"))
        if not exclude.is_absolute():
            exclude = path / exclude
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write("\nsrc/ignored.tmp\n")
        (path / "src" / "ignored.tmp").write_text("must retain ignored content\n", encoding="utf-8")
        response = self.client.post(f"/session/{sid}/fleet-commands/preview", headers=self.headers(),
                                   json={"action": "teardown", "board_id": board})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["can_execute"])
        self.assertTrue(runtime.active)
        self.assertTrue(path.exists())

    def test_history_and_other_session_cannot_read_live_repository_projection(self):
        state_dir = Path(self.tmp) / "state"
        sid, _, board, _ = self.prepare_delivery(state_dir=state_dir)
        other = self.new_session()
        self.assertIn(self.changes(other, board).status_code, (404, 409))
        self.assertIn(self.changes(sid, "other-board").status_code, (404, 409))
        self.assertEqual(self.changes(sid, board, "missing").status_code, 404)
        self.client.__exit__(None, None, None)
        self.start("error: parent model must not run\n", state_dir=state_dir)
        response = self.changes(sid, board)
        self.assertIn(response.status_code, (404, 409), response.text)
        self.assertFalse(self.client.app.state.sessions[sid].agent.chenshu.active)

    def test_merge_receipt_survives_restart_without_remerging(self):
        state_dir = Path(self.tmp) / "state"
        sid, runtime, board, _ = self.prepare_delivery(state_dir=state_dir)
        body, state = self.approve_command(sid, {"action": "merge", "board_id": board,
            "mission_id": "M1"}, "merge-once", "chenshu_merge")
        self.assertEqual(state["last_result"]["stopped"], "done")
        head = git(self.root, "rev-parse", "HEAD")
        self.client.__exit__(None, None, None)
        self.start("error: parent model must not run\n", state_dir=state_dir)
        response = self.submit(sid, body, "merge-once")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "submission_recorded")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), head)

    def test_merge_approval_revalidates_base_head_even_when_review_tip_unchanged(self):
        sid, runtime, board, _ = self.prepare_delivery()
        body = self.preview(sid, {"action": "merge", "board_id": board, "mission_id": "M1"})
        self.submit(sid, body, "base-moved")
        pending = self._wait_for(sid, "waiting_for_approval")["pending_approvals"][0]
        (self.root / "unrelated.txt").write_text("new base commit\n", encoding="utf-8")
        git(self.root, "add", "unrelated.txt")
        git(self.root, "commit", "-qm", "base moved")
        changed_head = git(self.root, "rev-parse", "HEAD")
        self.client.post(f"/session/{sid}/permissions", headers=self.headers(),
                         json={"request_id": pending["request_id"], "decision": "allow"})
        self.assertEqual(self.wait(sid)["last_result"]["stopped"], "tool_failed")
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), changed_head)
        self.assertFalse((self.root / "src" / "answer.txt").exists())
        self.assertNotEqual(runtime.mission("M1").status, "merged")

    def test_cleanup_approval_revalidates_new_local_file_and_keeps_runtime_active(self):
        from xiaoyu.permissions import Permissions, parse_rule
        sid, runtime, board, path = self.prepare_delivery()
        self.client.app.state.sessions[sid].agent.permissions = Permissions(
            self.root, [parse_rule("ask chenshu_teardown")])
        body = self.preview(sid, {"action": "teardown", "board_id": board})
        self.submit(sid, body, "cleanup-changed")
        pending = self._wait_for(sid, "waiting_for_approval")["pending_approvals"][0]
        (path / "src" / "later.txt").write_text("created after preview\n", encoding="utf-8")
        self.client.post(f"/session/{sid}/permissions", headers=self.headers(),
                         json={"request_id": pending["request_id"], "decision": "allow"})
        self.assertEqual(self.wait(sid)["last_result"]["stopped"], "tool_failed")
        self.assertTrue(path.is_dir())
        self.assertTrue((path / "src" / "later.txt").is_file())
        self.assertTrue(runtime.active)
        self.assertEqual(runtime.mission("M1").worktree, str(path))
