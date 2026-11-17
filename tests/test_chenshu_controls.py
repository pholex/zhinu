"""Host member control addresses one execution and reports actual input consumption."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import unittest
from unittest import mock

from xiaoyu.agent import Agent, Usage
from xiaoyu.chenshu import ChenshuError, ChenshuRuntime
from xiaoyu.events import ChenshuUpdated
from xiaoyu.providers import Registry

from .test_agent_paths import FakeClient
from .test_chenshu import ChenshuCase, HAS_GIT, LONG, text_turn, tool_turn


@unittest.skipUnless(HAS_GIT, "Git is required")
class ChenshuControlsTest(ChenshuCase):
    def setUp(self):
        super().setUp()
        self.init_repo()
        self.snapshots = []

    def runtime(self, script, *, approver=None, build=False):
        client = FakeClient(script)
        sink = mock.Mock()
        sink.emit.side_effect = lambda event: self.snapshots.append(event) if isinstance(event, ChenshuUpdated) else None
        runtime = ChenshuRuntime(self.config, Registry.for_client(client), Usage(), sink,
                                 None, member_approver=approver)
        runtime.init()
        if not runtime.missions:
            runtime.plan([{"title": "实现", "scope": ["src/"]} if build else {"title": "调研", "kind": "survey"}])
        return runtime, client

    def command(self, runtime, kind="message", *, name="reader", run_id=None, operation_id="op-1", text="补充核对。"):
        return runtime.member_control(kind, board_id=runtime._board_id, name=name,
                                      run_id=run_id or runtime.member(name).run_id,
                                      operation_id=operation_id, text=text if kind == "message" else "")

    def join(self, runtime, name="reader"):
        runtime._threads[name].join(5)
        self.assertFalse(runtime._threads[name].is_alive())
        runtime._publish_snapshot()

    def test_queued_messages_are_accepted_once_and_old_generation_cannot_control_resume(self):
        entered, release = threading.Event(), threading.Event()
        runtime, client = self.runtime([text_turn(LONG), text_turn("已核对。" + LONG), text_turn(LONG)])
        create = client.completions.create

        def gated(**kwargs):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(5))
            return create(**kwargs)

        with mock.patch.object(client.completions, "create", side_effect=gated):
            try:
                runtime.spawn("reader", "worker", mission_id="M1")
                self.assertTrue(entered.wait(2))
                old_run = runtime.member("reader").run_id
                queued = self.command(runtime)
                self.assertEqual(queued["status"], "queued")
                self.assertEqual(self.command(runtime), queued)
                with self.assertRaises(ValueError):
                    self.command(runtime, text="不同内容")
            finally:
                release.set()
                self.join(runtime)
        final = self.snapshots[-1]
        self.assertEqual(final.controls[0]["status"], "accepted")
        archive = json.loads(runtime._archive_path("reader").read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(sum("补充核对。" in str(m.get("content")) for m in archive), 1)
        self.assertEqual(self.command(runtime)["status"], "accepted")
        runtime.spawn("reader", "worker", mission_id="M1", resume=True)
        with self.assertRaises(ValueError):
            self.command(runtime, run_id=old_run, operation_id="old-generation")
        self.join(runtime)
        self.assertNotEqual(runtime.member("reader").run_id, old_run)

    def test_message_and_stop_before_send_survive_startup_reset(self):
        entered, release = threading.Event(), threading.Event()
        runtime, client = self.runtime([text_turn(LONG)])
        begin = Agent._begin_turn

        def blocked(agent, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return begin(agent, *args, **kwargs)

        with mock.patch.object(Agent, "_begin_turn", blocked):
            try:
                runtime.spawn("reader", "worker", mission_id="M1")
                self.assertTrue(entered.wait(2))
                self.command(runtime)
                stopped = self.command(runtime, "stop", operation_id="stop-1")
                self.assertEqual(stopped["status"], "requested")
                self.assertTrue(runtime.member("reader").stop_requested)
            finally:
                release.set()
                self.join(runtime)
        self.assertEqual(len(client.completions.calls), 0)
        self.assertEqual([c["status"] for c in self.snapshots[-1].controls], ["not_consumed", "settled"])
        self.assertEqual(runtime.member("reader").execution_outcome, "partial")
        self.assertTrue(runtime._archive_path("reader").exists())
        actual = {"controls": [dict(record, board_id="BOARD", member_run_id="RUN",
                                     created_at="TIME", updated_at="TIME")
                               for record in self.snapshots[-1].controls],
                  "member": {key: self.snapshots[-1].members[0][key]
                             for key in ("status", "execution_state", "execution_outcome", "stop_reason", "stop_requested")}}
        rendered = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path = Path(__file__).parent / "fixtures" / "chenshu_controls.expected.json"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            path.write_text(rendered, encoding="utf-8")
        self.assertEqual(rendered, path.read_text(encoding="utf-8", errors="replace"))

    def test_early_message_without_stop_is_delivered_after_startup_drain(self):
        entered, release = threading.Event(), threading.Event()
        runtime, client = self.runtime([text_turn(LONG), text_turn("按补充完成。" + LONG)])
        begin = Agent._begin_turn

        def blocked(agent, *args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return begin(agent, *args, **kwargs)

        with mock.patch.object(Agent, "_begin_turn", blocked):
            try:
                runtime.spawn("reader", "worker", mission_id="M1")
                self.assertTrue(entered.wait(2))
                self.command(runtime)
            finally:
                release.set()
                self.join(runtime)
        self.assertEqual(self.snapshots[-1].controls[0]["status"], "accepted")
        self.assertEqual(len(client.completions.calls), 2)

    def test_message_after_last_consumption_is_not_consumed_and_never_wakes_member(self):
        entered, release = threading.Event(), threading.Event()
        runtime, client = self.runtime([text_turn(LONG)])
        consume = Agent._consume_steers

        def after_last(agent):
            consumed = consume(agent)
            entered.set()
            self.assertTrue(release.wait(5))
            return consumed

        with mock.patch.object(Agent, "_consume_steers", after_last):
            try:
                runtime.spawn("reader", "worker", mission_id="M1")
                self.assertTrue(entered.wait(2))
                self.command(runtime)
            finally:
                release.set()
                self.join(runtime)
        self.assertEqual(self.snapshots[-1].controls[0]["status"], "not_consumed")
        self.assertEqual(len(client.completions.calls), 1)
        with self.assertRaises(ValueError):
            self.command(runtime, operation_id="late")

    def test_member_approval_stop_wakes_without_cleanup(self):
        waiting = threading.Event()
        identities = []

        def approve(tool, args, identity, stopped):
            identities.append(identity)
            waiting.set()
            for _ in range(100):
                if stopped():
                    return False
                threading.Event().wait(.01)
            raise AssertionError("member stop did not wake approval")

        runtime, _ = self.runtime([tool_turn("write", "write_file", {"path": "src/new.py", "content": "changed"}),
                                   text_turn(LONG)], approver=approve, build=True)
        runtime.spawn("reader", "worker", mission_id="M1")
        self.assertTrue(waiting.wait(3))
        workdir = runtime.missions[0].worktree
        self.command(runtime, "stop")
        self.join(runtime)
        self.assertEqual(identities[0]["member_run_id"], runtime.member("reader").run_id)
        self.assertEqual(identities[0]["workspace"], workdir)
        from pathlib import Path
        self.assertTrue(Path(workdir).is_dir())
        self.assertFalse((Path(workdir) / "src/new.py").exists())
        self.assertTrue(runtime._archive_path("reader").is_file())
        self.assertTrue(runtime.active)
        self.assertEqual(runtime.member("reader").execution_outcome, "partial")
        self.assertEqual(self.snapshots[-1].controls[0]["status"], "settled")

    def test_host_guard_blocks_outside_write_even_if_permission_bypasses_approver(self):
        from xiaoyu.permissions import Permissions
        target = self.root / "outside.txt"
        approve = mock.Mock(return_value=True)
        runtime, _ = self.runtime([tool_turn("write", "write_file", {"path": str(target), "content": "bad"}),
                                   text_turn(LONG)], approver=approve, build=True)
        permissions = Permissions(self.config)
        with mock.patch.object(permissions, "explain", return_value=("allow", None)):
            runtime.permissions = permissions
            runtime.spawn("reader", "worker", mission_id="M1")
            self.join(runtime)
        self.assertFalse(target.exists())
        approve.assert_not_called()
        archive = runtime._archive_path("reader").read_text(encoding="utf-8", errors="replace")
        self.assertIn("越界", archive)

    def test_stop_one_member_leaves_other_running_and_close_requests_all_without_teardown(self):
        entered = {name: threading.Event() for name in ("reader", "other")}
        release = {name: threading.Event() for name in entered}
        runtime, client = self.runtime([text_turn(LONG), text_turn(LONG)])
        runtime.plan([{"title": "甲", "kind": "survey"}, {"title": "乙", "kind": "survey"}])
        create = client.completions.create

        def gated(**kwargs):
            name = threading.current_thread().name.removeprefix("chenshu-")
            entered[name].set()
            self.assertTrue(release[name].wait(5))
            return create(**kwargs)

        with mock.patch.object(client.completions, "create", side_effect=gated):
            try:
                runtime.spawn("reader", "worker", mission_id="M1")
                runtime.spawn("other", "worker", mission_id="M2")
                self.assertTrue(all(event.wait(2) for event in entered.values()))
                self.command(runtime, "stop")
                self.assertFalse(runtime.member("other").stop_requested)
                release["reader"].set()
                self.join(runtime)
                self.assertTrue(runtime._threads["other"].is_alive())
                self.assertEqual(runtime.member("other").execution_outcome, "running")
                runtime.stop_members()
                self.assertTrue(runtime.member("other").stop_requested)
            finally:
                for event in release.values():
                    event.set()
                self.join(runtime)
                self.join(runtime, "other")
        self.assertTrue(runtime.active)
        self.assertTrue(all(runtime._archive_path(name).exists() for name in entered))
        self.assertTrue(all(member.execution_outcome == "partial" for member in runtime.members))

    def test_guard_precedes_approval_and_rechecks_approved_argument_changes(self):
        from xiaoyu.agent import Allow
        from pathlib import Path
        for rewritten in (False, True):
            with self.subTest(rewritten=rewritten):
                outside = self.root / f"outside-{rewritten}.txt"
                approve = mock.Mock(return_value=Allow(updated_args={"path": str(outside), "content": "bad",
                                                               "_name": "read_file", "_handler": "override"}))
                runtime, _ = self.runtime([
                    tool_turn("write", "write_file", {"path": "src/new.py" if rewritten else str(outside), "content": "bad"}),
                    text_turn(LONG)], approver=approve, build=True)
                runtime.spawn("reader", "worker", mission_id="M1", resume=rewritten)
                self.join(runtime)
                self.assertFalse(outside.exists())
                self.assertFalse((Path(runtime.missions[0].worktree) / "src/new.py").exists())
                self.assertEqual(approve.call_count, 1 if rewritten else 0)

    def test_denied_host_approval_is_not_file_completion(self):
        from pathlib import Path
        runtime, _ = self.runtime([
            tool_turn("write", "write_file", {"path": "src/new.py", "content": "bad"}),
            text_turn("已放弃这次写入。" + LONG)], approver=mock.Mock(return_value=False), build=True)
        runtime.spawn("reader", "worker", mission_id="M1")
        self.join(runtime)
        self.assertFalse((Path(runtime.missions[0].worktree) / "src/new.py").exists())
        self.assertIn("用户拒绝", runtime._archive_path("reader").read_text(encoding="utf-8", errors="replace"))
        self.assertEqual(runtime.missions[0].status, "active")

    def test_queue_bound_retains_queued_receipts_and_rejects_invalid_targets(self):
        entered, release = threading.Event(), threading.Event()
        runtime, client = self.runtime([text_turn(LONG), text_turn(LONG)])
        create = client.completions.create

        def gated(**kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return create(**kwargs)

        with mock.patch.object(client.completions, "create", side_effect=gated):
            try:
                runtime.spawn("reader", "worker", mission_id="M1")
                self.assertTrue(entered.wait(2))
                for index in range(64):
                    self.command(runtime, operation_id=f"message-{index}", text=f"核对 {index}")
                with self.assertRaises(ValueError):
                    self.command(runtime, operation_id="overflow")
                with self.assertRaises(ValueError):
                    runtime.member_control("stop", board_id="another", name="reader",
                                           run_id=runtime.member("reader").run_id, operation_id="wrong")
                self.assertEqual(len(self.snapshots[-1].controls), 64)
                self.assertTrue(all(c["status"] == "queued" for c in self.snapshots[-1].controls))
                self.command(runtime, "stop", operation_id="stop")
            finally:
                release.set()
                self.join(runtime)
        self.assertEqual(sum(c["status"] == "not_consumed" for c in self.snapshots[-1].controls), 64)

    def test_shutdown_permanently_rejects_spawn_even_after_init(self):
        runtime, client = self.runtime([])
        runtime.stop_members()
        runtime.init()
        with self.assertRaisesRegex(ChenshuError, "正在关闭"):
            runtime.spawn("reader", "worker", mission_id="M1")
        self.assertEqual(runtime.members, [])
        self.assertEqual(runtime._member_controls, {})
        self.assertEqual(client.completions.calls, [])

    def test_shutdown_wins_over_spawn_preflight_in_progress(self):
        entered, release = threading.Event(), threading.Event()
        runtime, client = self.runtime([])
        resolve = runtime.registry.resolve
        failures = []

        def gated(model):
            entered.set()
            self.assertTrue(release.wait(5))
            return resolve(model)

        def spawn():
            try:
                runtime.spawn("reader", "worker", mission_id="M1", model="member-model")
            except ChenshuError as exc:
                failures.append(str(exc))

        with mock.patch.object(runtime.registry, "resolve", side_effect=gated):
            thread = threading.Thread(target=spawn)
            try:
                thread.start()
                self.assertTrue(entered.wait(2))
                runtime.stop_members()
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIn("正在关闭", failures[0])
        self.assertEqual(runtime.members, [])
        self.assertEqual(runtime._member_controls, {})
        self.assertEqual(client.completions.calls, [])

    def test_shutdown_stops_registered_member_before_worker_thread_starts(self):
        entered, release = threading.Event(), threading.Event()
        runtime, client = self.runtime([])
        briefing = runtime._briefing
        failures = []

        def gated(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return briefing(*args)

        def spawn():
            try:
                runtime.spawn("reader", "worker", mission_id="M1")
            except Exception as exc:
                failures.append(exc)

        with mock.patch.object(runtime, "_briefing", side_effect=gated):
            thread = threading.Thread(target=spawn)
            try:
                thread.start()
                self.assertTrue(entered.wait(2))
                self.assertEqual(runtime.member("reader").status, "running")
                runtime.stop_members()
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.join(runtime)
        self.assertEqual(client.completions.calls, [])
        self.assertEqual(runtime.member("reader").execution_outcome, "partial")
        self.assertEqual(runtime.member("reader").stop_reason, "interrupted")
        self.assertTrue(runtime._archive_path("reader").exists())
