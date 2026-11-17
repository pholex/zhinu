"""Explicit orchestration previews are read-only and checked again after approval."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from xiaoyu.agent import Usage
from xiaoyu.chenshu import ChenshuRuntime, Member, make_chenshu_tools
from xiaoyu.events import ObservingSink
from xiaoyu.providers import Registry
from xiaoyu.serve_commands import CommandError
from xiaoyu import serve_fleet_commands as commands
from xiaoyu.tools import Toolbox

from .test_agent_paths import FakeClient
from .test_chenshu import ChenshuCase, HAS_GIT, LONG, git, text_turn


@unittest.skipUnless(HAS_GIT, "Git is required")
class FleetCommandTests(ChenshuCase):
    def setUp(self):
        super().setUp()
        self.init_repo()
        self.session = self.session_for([text_turn(LONG), text_turn(LONG)])

    def session_for(self, script=()):
        registry = Registry.for_client(FakeClient(list(script)))
        runtime = ChenshuRuntime(self.config, registry, Usage(), ObservingSink(), None)
        toolbox = Toolbox(self.config, only=[])
        for tool in make_chenshu_tools(runtime):
            toolbox.register(tool)
        agent = SimpleNamespace(config=self.config, chenshu=runtime, registry=registry,
                                toolbox=toolbox, plan_mode=False, session_log=SimpleNamespace(complete=True, locked=True))
        session = SimpleNamespace(agent=agent, workspace=self.root, busy=False, history_warning="",
                                  cfg=SimpleNamespace(persist=True), fleets=SimpleNamespace(integrity=True),
                                  fleet_controls=SimpleNamespace(integrity=True), check_budget=lambda: False)
        self.addCleanup(lambda: commands.release_owner(session))
        return session

    def execute(self, body, session=None):
        session = session or self.session
        prepared = commands.prepare(session, body)
        runtime = session.agent.chenshu
        before = runtime.host_guard
        runtime.host_guard = prepared.guard
        try:
            result = session.agent.toolbox.get(prepared.tool_name).handler(**prepared.args)
        finally:
            runtime.host_guard = before
        self.assertFalse(result.startswith("ERROR:"), result)
        return prepared

    def init(self):
        self.execute({"action": "init", "mode": "new"})
        return self.session.agent.chenshu

    def plan(self, runtime):
        return self.execute({"action": "plan", "board_id": runtime._board_id,
                             "missions": [{"title": "调研", "kind": "survey"}]})

    def join(self, runtime):
        runtime._threads["reader"].join(5)
        self.assertFalse(runtime._threads["reader"].is_alive())
        runtime._publish_snapshot()

    def test_preview_new_and_capabilities_have_no_filesystem_or_runtime_side_effects(self):
        runtime = self.session.agent.chenshu
        before = set(self.root.rglob("*"))
        with mock.patch.object(runtime, "init", side_effect=AssertionError("preview cannot initialize")), \
             mock.patch.object(runtime, "_load", side_effect=AssertionError("preview cannot adopt")), \
             mock.patch("xiaoyu.worktree.create_branch", side_effect=AssertionError("preview cannot create worktree")):
            caps = commands.capabilities(self.session)
            prepared = commands.prepare(self.session, {"action": "init", "mode": "new"})
        self.assertTrue(caps["can_init"])
        self.assertEqual(prepared.tool_name, "chenshu_init")
        self.assertEqual(before, set(self.root.rglob("*")))
        self.assertFalse(runtime.active)
        self.assertFalse(runtime.root.exists())

    def test_plan_validation_is_shared_and_guard_rejects_changed_board(self):
        runtime = self.init()
        body = {"action": "plan", "board_id": runtime._board_id, "missions": [
            {"title": "调研", "kind": "survey"},
            {"title": "实现", "kind": "build", "scope": ["src/"], "deps": ["M1"]}]}
        prepared = commands.prepare(self.session, body)
        self.assertEqual([m["id"] for m in prepared.preview["missions"]], ["M1", "M2"])
        self.assertEqual(runtime.missions, [])
        invalid = copy.deepcopy(body)
        invalid["missions"][0] = {"title": "重复", "scope": ["src/a.py"]}
        with self.assertRaises(CommandError):
            commands.prepare(self.session, invalid)
        runtime.plan([{"title": "另一个计划", "kind": "survey"}])
        runtime.host_guard = prepared.guard
        try:
            result = self.session.agent.toolbox.get(prepared.tool_name).handler(**prepared.args)
        finally:
            runtime.host_guard = None
        self.assertTrue(result.startswith("ERROR:"), result)
        self.assertEqual(len(runtime.missions), 1)

    def test_head_and_configuration_changes_invalidate_preview_and_execution(self):
        runtime = self.init()
        body = {"action": "plan", "board_id": runtime._board_id,
                "missions": [{"title": "调研", "kind": "survey"}]}
        prepared = commands.prepare(self.session, body)
        self.config.effort = "high"
        with self.assertRaises(CommandError):
            commands.prepare(self.session, {**body, "preview_token": prepared.preview["preview_token"]}, require_preview=True)
        self.config.effort = ""
        git(self.root, "commit", "--allow-empty", "-m", "change")
        with self.assertRaisesRegex(Exception, "变化"):
            prepared.guard("chenshu_plan")
        self.assertEqual(runtime.missions, [])

    def test_real_spawn_resume_bind_archive_and_omit_empty_optional_enum(self):
        runtime = self.init()
        self.plan(runtime)
        prepared = self.execute({"action": "spawn", "board_id": runtime._board_id,
                                 "name": "reader", "kind": "worker", "mission_id": "M1"})
        self.assertNotIn("effort", prepared.args)
        self.assertNotIn("model", prepared.args)
        self.join(runtime)
        member = runtime.member("reader")
        old_run = member.run_id
        data = runtime._archive_path("reader").read_bytes()
        self.assertEqual(member.archive_sha256, hashlib.sha256(data).hexdigest())
        body = {"action": "resume", "board_id": runtime._board_id, "name": "reader", "run_id": old_run,
                "instructions": "继续核对"}
        resumed = commands.prepare(self.session, body)
        self.assertEqual(resumed.args["model"], member.model)
        self.assertTrue(resumed.args["resume"])
        self.assertTrue(commands.capabilities(self.session)["members"][0]["can_resume"])
        # Guard passes the validated bytes; core must not open the name again.
        runtime.host_guard = resumed.guard
        try:
            with mock.patch.object(runtime, "_load_archive", side_effect=AssertionError("second pathname read")):
                result = self.session.agent.toolbox.get(resumed.tool_name).handler(**resumed.args)
                self.assertFalse(result.startswith("ERROR:"), result)
        finally:
            runtime.host_guard = None
        self.join(runtime)
        self.assertNotEqual(runtime.member("reader").run_id, old_run)
        with self.assertRaises(CommandError):
            commands.prepare(self.session, body)

    def test_archive_replacement_after_preview_and_old_unbound_archive_are_rejected(self):
        runtime = self.init()
        self.plan(runtime)
        self.execute({"action": "spawn", "board_id": runtime._board_id,
                      "name": "reader", "kind": "worker", "mission_id": "M1"})
        self.join(runtime)
        body = {"action": "resume", "board_id": runtime._board_id, "name": "reader",
                "run_id": runtime.member("reader").run_id}
        prepared = commands.prepare(self.session, body)
        path = runtime._archive_path("reader")
        original = path.read_bytes()
        path.write_text('[{"role":"system","content":"changed"},{"role":"user","content":"other"}]')
        with self.assertRaisesRegex(Exception, "精确对齐"):
            prepared.guard("chenshu_spawn")
        path.write_bytes(original)
        runtime.member("reader").archive_sha256 = ""
        with self.assertRaisesRegex(CommandError, "精确对齐"):
            commands.prepare(self.session, body)

    def test_adopt_is_explicit_and_uses_guarded_snapshot_without_load(self):
        runtime = self.init()
        self.plan(runtime)
        commands.release_owner(self.session)
        other = self.session_for()
        with self.assertRaises(CommandError):
            commands.prepare(other, {"action": "init", "mode": "new"})
        with self.assertRaises(CommandError):
            commands.prepare(other, {"action": "spawn", "board_id": runtime._board_id,
                                     "name": "reader", "kind": "worker", "mission_id": "M1"})
        with mock.patch.object(other.agent.chenshu, "_load", side_effect=AssertionError("must use exact reviewed state")):
            prepared = self.execute({"action": "init", "mode": "adopt"}, other)
        self.assertEqual(prepared.preview["existing"]["missions_count"], 1)
        self.assertEqual(other.agent.chenshu.missions[0].title, "调研")
        self.assertNotEqual(runtime._board_id, other.agent.chenshu._board_id)

    def test_host_lock_excludes_other_process_owner_and_legacy_running_cannot_be_assumed_dead(self):
        runtime = self.init()
        self.plan(runtime)
        other = self.session_for()
        with self.assertRaisesRegex(CommandError, "另一进程"):
            commands.prepare(other, {"action": "init", "mode": "adopt"})
        commands.release_owner(self.session)
        state = json.loads(runtime._state_path().read_text())
        state.pop("host_protocol")
        state["members"] = [{"name": "reader", "kind": "worker", "mission_id": "M1", "status": "running"}]
        runtime._state_path().write_text(json.dumps(state))
        with self.assertRaisesRegex(CommandError, "不能安全接管"):
            commands.prepare(other, {"action": "init", "mode": "adopt"})

    def test_owner_check_rechecked_after_preview_and_claimed_only_on_execution(self):
        checker = mock.Mock(return_value="")
        self.session.fleet_owner_check = checker
        prepared = commands.prepare(self.session, {"action": "init", "mode": "new"})
        self.assertFalse(any(call.kwargs.get("claim") for call in checker.call_args_list))
        checker.return_value = "工作区已有其他会话"
        with self.assertRaisesRegex(Exception, "其他会话"):
            prepared.guard("chenshu_init")
        self.assertFalse(self.session.agent.chenshu.active)

    def test_strict_fields_symlinks_and_nonpersistent_session_fail_closed(self):
        with self.assertRaises(CommandError):
            commands.prepare(self.session, {"action": "init", "mode": "new", "path": "/other"})
        self.session.cfg.persist = False
        with self.assertRaises(CommandError):
            commands.prepare(self.session, {"action": "init", "mode": "new"})
        self.session.cfg.persist = True
        runtime = self.session.agent.chenshu
        outside = self.root / "other-state"
        outside.mkdir()
        runtime.root.parent.mkdir()
        runtime.root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(CommandError, "符号链接"):
            commands.prepare(self.session, {"action": "init", "mode": "new"})

    def test_preview_contract_keyless_snapshot(self):
        runtime = self.init()
        prepared = commands.prepare(self.session, {"action": "plan", "board_id": runtime._board_id,
            "missions": [{"title": "调研", "kind": "survey"},
                         {"title": "实现", "scope": ["src/"], "deps": ["M1"]}]})
        actual = copy.deepcopy(prepared.preview)
        actual.update(board_id="BOARD", preview_token="TOKEN", repo_head="HEAD")
        if actual["existing"]:
            actual["existing"]["state_id"] = "STATE"
        rendered = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path = Path(__file__).parent / "fixtures" / "fleet_commands.expected.json"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            path.write_text(rendered, encoding="utf-8")
        self.assertEqual(rendered, path.read_text(encoding="utf-8", errors="replace"))

    def test_actual_parameters_cannot_change_after_preview(self):
        runtime = self.init()
        prepared = commands.prepare(self.session, {"action": "plan", "board_id": runtime._board_id,
                                                  "missions": [{"title": "已审阅", "kind": "survey"}]})
        runtime.host_guard = prepared.guard
        try:
            result = self.session.agent.toolbox.get(prepared.tool_name).handler(
                missions=[{"title": "被替换", "kind": "survey"}])
        finally:
            runtime.host_guard = None
        self.assertIn("执行参数", result)
        self.assertEqual(runtime.missions, [])

    def test_corrupt_state_ids_dependencies_and_member_assignment_reject_adoption(self):
        runtime = self.init()
        self.plan(runtime)
        commands.release_owner(self.session)
        original = json.loads(runtime._state_path().read_text())
        for mutation in ("id", "deps", "member"):
            with self.subTest(mutation=mutation):
                state = copy.deepcopy(original)
                if mutation == "id":
                    state["missions"][0]["id"] = "M2"
                elif mutation == "deps":
                    state["missions"][0]["deps"] = ["M2"]
                else:
                    state["members"] = [{"name": "reader", "kind": "worker", "mission_id": "M99"}]
                runtime._state_path().write_text(json.dumps(state))
                with self.assertRaisesRegex(CommandError, "格式不完整"):
                    commands.prepare(self.session_for(), {"action": "init", "mode": "adopt"})

    def test_archive_save_failure_clears_old_generation_digest(self):
        runtime = self.init()
        self.plan(runtime)
        self.execute({"action": "spawn", "board_id": runtime._board_id,
                      "name": "reader", "kind": "worker", "mission_id": "M1"})
        self.join(runtime)
        self.assertTrue(runtime.member("reader").archive_sha256)
        prepared = commands.prepare(self.session, {"action": "resume", "board_id": runtime._board_id,
            "name": "reader", "run_id": runtime.member("reader").run_id})
        from xiaoyu import fsguard
        writer = fsguard.write_atomic

        def fail_archive(path, *args, **kwargs):
            if path == runtime._archive_path("reader"):
                raise OSError("fixture cannot save archive")
            return writer(path, *args, **kwargs)

        runtime.host_guard = prepared.guard
        try:
            with mock.patch("xiaoyu.chenshu.fsguard.write_atomic", side_effect=fail_archive):
                result = self.session.agent.toolbox.get(prepared.tool_name).handler(**prepared.args)
                self.assertFalse(result.startswith("ERROR:"), result)
                self.join(runtime)
        finally:
            runtime.host_guard = None
        self.assertEqual(runtime.member("reader").archive_sha256, "")
        self.assertFalse(commands.capabilities(self.session)["members"][0]["can_resume"])

    def build_ready(self, *, review=True, filename="src/new.py", content="new value\n"):
        from xiaoyu import worktree
        runtime = self.init()
        self.execute({"action": "plan", "board_id": runtime._board_id,
                      "missions": [{"title": "实现", "scope": ["src/"]}]})
        mission = runtime.missions[0]
        path = worktree.create_branch(self.root, mission.branch, "test", "main")
        mission.worktree = str(path)
        (path / filename).parent.mkdir(parents=True, exist_ok=True)
        (path / filename).write_text(content)
        git(path, "add", "-A")
        git(path, "commit", "-qm", "deliver")
        runtime._save()
        if review:
            runtime.submit_review("chenshu", mission.id, "clean", "已核对实际提交。")
        return runtime, mission, path

    def test_blocked_merge_still_shows_diff_and_readonly_changes_survives_busy(self):
        runtime, mission, _ = self.build_ready(review=False)
        body = {"action": "merge", "board_id": runtime._board_id, "mission_id": mission.id}
        preview = commands.prepare(self.session, body).preview
        self.assertFalse(preview["can_execute"])
        self.assertEqual(preview["preview_token"], "")
        self.assertIn("review_missing", [b["code"] for b in preview["blockers"]])
        self.assertIn("+new value", preview["merge"]["diff"]["text"])
        self.session.busy = True
        read = commands.changes(self.session, runtime._board_id, mission.id)
        self.assertTrue(read["merge"]["diff"]["available"])
        self.assertFalse(read["can_merge"])
        self.assertIn("session_unavailable", [b["code"] for b in read["blockers"]])
        self.assertEqual(mission.status, "pending")

    def test_real_merge_cleanup_keeps_branch_and_protocol_records(self):
        runtime, mission, path = self.build_ready()
        self.execute({"action": "merge", "board_id": runtime._board_id, "mission_id": mission.id})
        self.assertEqual(mission.status, "merged")
        self.assertEqual((self.root / "src/new.py").read_text(), "new value\n")
        preview = commands.prepare(self.session, {"action": "teardown", "board_id": runtime._board_id}).preview
        self.assertTrue(preview["can_execute"], preview)
        self.assertTrue(preview["teardown"]["archives_retained"])
        self.execute({"action": "teardown", "board_id": runtime._board_id})
        self.assertFalse(path.exists())
        self.assertFalse(runtime.active)
        self.assertEqual(mission.worktree, "")
        self.assertTrue(runtime._state_path().exists())
        self.assertTrue(git(self.root, "rev-parse", mission.branch))

    @unittest.skipIf(os.name == "nt", "Windows 的文件名不能含换行，这条路径形态在那里造不出来")
    def test_scope_uses_exact_nul_delimited_paths_and_diff_disables_external_drivers(self):
        runtime, mission, _ = self.build_ready(filename="src/ spaced\nname.py")
        marker = self.root / "external-diff-ran"
        git(self.root, "config", "diff.external", f"touch {marker}")
        data = commands.changes(self.session, runtime._board_id, mission.id)
        self.assertEqual(data["merge"]["changed_files"], ["src/ spaced\nname.py"])
        self.assertTrue(data["can_merge"], data)
        self.assertFalse(marker.exists())
        self.execute({"action": "merge", "board_id": runtime._board_id, "mission_id": mission.id})
        self.assertTrue((self.root / "src/ spaced\nname.py").exists())

    def test_review_and_dirty_changes_after_preview_rejected_at_mutation(self):
        runtime, mission, _ = self.build_ready()
        body = {"action": "merge", "board_id": runtime._board_id, "mission_id": mission.id}
        prepared = commands.prepare(self.session, body)
        runtime.submit_review("chenshu", mission.id, "p1-1", "仍需修复。")
        with self.assertRaisesRegex(Exception, "阻止"):
            prepared.guard("chenshu_merge")
        runtime.submit_review("chenshu", mission.id, "clean", "已重新核对。")
        prepared = commands.prepare(self.session, body)
        (self.root / "local.txt").write_text("local draft")
        with self.assertRaisesRegex(Exception, "阻止"):
            prepared.guard("chenshu_merge")
        with self.assertRaisesRegex(Exception, "主工作区"):
            runtime.merge(mission.id)
        self.assertNotEqual(mission.status, "merged")

    def test_teardown_rejects_ignored_or_unknown_and_never_implicitly_stops(self):
        runtime, mission, path = self.build_ready()
        git(path, "config", "core.excludesFile", "/dev/null")
        (path / "ignored.bin").write_bytes(b"valuable cache")
        with (self.root / ".git/info/exclude").open("a") as handle:
            handle.write("ignored.bin\n")
        body = {"action": "teardown", "board_id": runtime._board_id}
        preview = commands.prepare(self.session, body).preview
        self.assertFalse(preview["can_execute"])
        self.assertTrue(preview["teardown"]["worktrees"][0]["dirty"])
        with self.assertRaises(CommandError):
            commands.prepare(self.session, {**body, "preview_token": ""}, require_preview=True)
        (path / "ignored.bin").unlink()
        runtime.members.append(Member("reader", "worker", mission_id=mission.id,
                                      status="retired", execution_outcome="unknown"))
        runtime._save()
        preview = commands.prepare(self.session, body).preview
        self.assertFalse(preview["can_execute"])
        self.assertIn("member_unknown", [b["code"] for b in preview["blockers"]])
        self.assertFalse(runtime._closing)
        self.assertTrue(path.is_dir())

    def test_unmerged_committed_worktree_can_be_cleaned_but_failed_remove_keeps_path(self):
        runtime, mission, path = self.build_ready(review=False)
        body = {"action": "teardown", "board_id": runtime._board_id}
        prepared = commands.prepare(self.session, body)
        self.assertTrue(prepared.preview["can_execute"], prepared.preview)
        self.assertFalse(prepared.preview["teardown"]["worktrees"][0]["merged"])
        runtime.host_guard = prepared.guard
        try:
            with mock.patch("xiaoyu.chenshu.worktree.remove", return_value=False):
                result = self.session.agent.toolbox.get("chenshu_teardown").handler()
        finally:
            runtime.host_guard = None
        self.assertIn("ERROR:", result)
        self.assertTrue(path.exists())
        self.assertEqual(mission.worktree, str(path))
        self.assertTrue(runtime.active)
        self.execute(body)
        self.assertFalse(path.exists())
        self.assertTrue(git(self.root, "rev-parse", mission.branch))

    def test_core_survey_requires_completion_and_merge_abort_is_not_assumed(self):
        runtime = self.init()
        self.plan(runtime)
        with self.assertRaisesRegex(Exception, "completed"):
            runtime.merge("M1")
        runtime.mission_view("chenshu", "M1", {"status": "completed"})
        self.assertIn("完结", runtime.merge("M1"))

    def test_bounded_diff_and_merge_preview_keyless_snapshot(self):
        runtime, mission, _ = self.build_ready(review=False, content="x" * 80000 + "\n")
        preview = commands.prepare(self.session, {"action": "merge", "board_id": runtime._board_id,
                                                 "mission_id": mission.id}).preview
        diff = preview["merge"]["diff"]
        self.assertTrue(diff["available"])
        self.assertTrue(diff["truncated"])
        self.assertLessEqual(len(diff["text"].encode()), 64 * 1024)
        actual = {"action": preview["action"], "can_execute": preview["can_execute"],
                  "blockers": preview["blockers"], "preview_token": preview["preview_token"],
                  "review": preview["merge"]["review"], "changed_files": preview["merge"]["changed_files"],
                  "diff": {key: value for key, value in diff.items() if key != "text"},
                  "conflict_check": preview["merge"]["conflict_check"]}
        rendered = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path = Path(__file__).parent / "fixtures" / "fleet_merge.expected.json"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            path.write_text(rendered, encoding="utf-8")
        self.assertEqual(rendered, path.read_text(encoding="utf-8", errors="replace"))

    def test_real_merge_conflict_reports_rollback_only_after_verified_restore(self):
        runtime, mission, _ = self.build_ready(filename="src/a.py", content="branch change\n")
        (self.root / "src/a.py").write_text("base change\n")
        git(self.root, "add", "src/a.py")
        git(self.root, "commit", "-qm", "base advances")
        before = git(self.root, "rev-parse", "HEAD")
        prepared = commands.prepare(self.session, {"action": "merge", "board_id": runtime._board_id,
                                                  "mission_id": mission.id})
        self.assertTrue(prepared.preview["can_execute"], prepared.preview)
        self.assertEqual(prepared.preview["merge"]["conflict_check"], "not_checked")
        runtime.host_guard = prepared.guard
        try:
            result = self.session.agent.toolbox.get("chenshu_merge").handler(target=mission.id)
        finally:
            runtime.host_guard = None
        self.assertIn("ERROR: 合并冲突，已回滚", result)
        self.assertEqual(git(self.root, "rev-parse", "HEAD"), before)
        self.assertEqual((self.root / "src/a.py").read_text(), "base change\n")
        self.assertFalse((self.root / ".git/MERGE_HEAD").exists())
        self.assertNotEqual(mission.status, "merged")

    def test_failed_abort_does_not_claim_successful_rollback(self):
        import subprocess
        from xiaoyu import chenshu
        runtime, mission, _ = self.build_ready()
        original = chenshu._git

        def fail_merge(args, cwd):
            if args[:2] == ["merge", "--no-ff"]:
                return subprocess.CompletedProcess(args, 1, "CONFLICT fixture", "")
            if args == ["merge", "--abort"]:
                return subprocess.CompletedProcess(args, 1, "", "abort failed")
            if args == ["rev-parse", "-q", "--verify", "MERGE_HEAD"]:
                return subprocess.CompletedProcess(args, 0, "still-merging", "")
            return original(args, cwd)

        with mock.patch("xiaoyu.chenshu._git", side_effect=fail_merge):
            with self.assertRaisesRegex(Exception, "未能确认回滚") as caught:
                runtime.merge(mission.id)
        self.assertNotIn("已回滚", str(caught.exception))
        self.assertNotEqual(mission.status, "merged")

    def test_worktree_remove_checks_process_result_and_path_disappearance(self):
        import subprocess
        from xiaoyu import worktree
        runtime, mission, path = self.build_ready()
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                with mock.patch("xiaoyu.worktree._run_git", return_value=subprocess.CompletedProcess([], returncode, "", "")):
                    self.assertFalse(worktree.remove(self.root, path))
                self.assertTrue(path.is_dir())
        self.assertTrue(worktree.remove(self.root, path))
        self.assertFalse(path.exists())
