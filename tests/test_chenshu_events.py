"""宸枢只读快照来自真实协议操作；不从人机状态文本猜执行结果。"""
from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.agent import Usage
from xiaoyu.chenshu import ChenshuRuntime, Member, Mission
from xiaoyu.errors import Interrupted
from xiaoyu.events import ChenshuUpdated
from xiaoyu.providers import Registry

from .test_agent_paths import FakeClient
from .test_chenshu import ChenshuCase, HAS_GIT, LONG, git, text_turn, tool_turn


class Sink:
    def __init__(self):
        self.events = []

    def emit(self, event):
        if isinstance(event, ChenshuUpdated):
            self.events.append(event)


@unittest.skipUnless(HAS_GIT, "Git is required for the real collaboration protocol")
class ChenshuEventsTest(ChenshuCase):
    def setUp(self):
        super().setUp()
        self.init_repo()
        self.sink = Sink()

    def runtime(self, script=(), *, stop=None):
        self.client = FakeClient(list(script))
        return ChenshuRuntime(self.config, Registry.for_client(self.client), Usage(), self.sink,
                              None, stop_requested=stop)

    def join(self, runtime, name="reader"):
        runtime._threads[name].join(5)
        self.assertFalse(runtime._threads[name].is_alive(), "scripted member did not settle")
        runtime.status()

    def test_construction_is_side_effect_free_and_snapshots_are_stable_copies(self):
        runtime = self.runtime()
        self.assertFalse(runtime.root.exists())
        self.assertEqual(self.sink.events, [])
        runtime.init()
        first = self.sink.events[0]
        runtime.plan([{"title": "调研", "kind": "survey"},
                      {"title": "实现", "scope": ["src/"], "deps": ["M1"]}])
        final = self.sink.events[-1]
        self.assertEqual(first.board_id, final.board_id)
        self.assertEqual(first.missions, ())
        self.assertEqual(final.missions[1]["deps"], ["M1"])
        self.assertEqual(final.missions[1]["scope"], ["src/"])
        self.assertEqual(final.missions[1]["status"], "pending")
        self.assertEqual(final.source["runtime_id"], final.board_id)
        self.assertEqual(json.loads(json.dumps(final.to_dict()))["kind"], "chenshu.updated")
        self.assertEqual([e.revision for e in self.sink.events], list(range(1, len(self.sink.events) + 1)))
        count = len(self.sink.events)
        runtime.status()
        runtime.status()
        self.assertEqual(len(self.sink.events), count, "unchanged reads must not emit repeated snapshots")

    def test_real_member_findings_protocol_completion_and_handoff(self):
        runtime = self.runtime([
            tool_turn("finding", "chenshu_finding", {"kind": "idea", "title": "补充材料", "body": "还需核对边界。"}),
            tool_turn("mission", "chenshu_mission", {"status": "completed"}),
            text_turn("完成调研。" + LONG),
        ])
        runtime.init()
        runtime.plan([{"title": "调研", "kind": "survey"}])
        runtime.spawn("reader", "worker", mission_id="M1", model="member-model")
        self.join(runtime)
        final = self.sink.events[-1]
        member = final.members[0]
        self.assertEqual(final.missions[0]["status"], "completed")
        self.assertEqual((member["status"], member["execution_state"], member["execution_outcome"]),
                         ("done", "settled", "completed"))
        self.assertEqual(member["model"], "member-model")
        self.assertFalse(member["thread_alive"])
        self.assertIn("完成调研", member["handoff"])
        self.assertTrue(member["run_id"])
        self.assertEqual(final.findings[0]["mission_id"], "M1")
        self.assertEqual(final.findings[0]["from"], "reader")
        self.assertEqual(final.findings[0]["body"], "还需核对边界。")
        self.assertIn("member.done", [event["action"] for event in final.activity])

    def test_parent_wait_stop_does_not_change_running_member_to_stopped(self):
        stopped, entered, released = threading.Event(), threading.Event(), threading.Event()
        runtime = self.runtime([text_turn(LONG)], stop=stopped.is_set)
        create = self.client.completions.create

        def block(**kwargs):
            entered.set()
            if not released.wait(5):
                raise RuntimeError("test did not release scripted request")
            return create(**kwargs)

        with mock.patch.object(self.client.completions, "create", side_effect=block):
            runtime.init()
            runtime.plan([{"title": "调研", "kind": "survey"}])
            try:
                runtime.spawn("reader", "worker", mission_id="M1")
                self.assertTrue(entered.wait(2))
                stopped.set()
                with self.assertRaises(Interrupted):
                    runtime.wait(timeout=3)
                member = self.sink.events[-1].members[0]
                self.assertTrue(self.sink.events[-1].active)
                self.assertEqual(member["execution_state"], "running")
                self.assertEqual(member["execution_outcome"], "running")
                self.assertTrue(member["thread_alive"])
            finally:
                released.set()
                self.join(runtime)
        self.assertEqual(self.sink.events[-1].members[0]["execution_outcome"], "completed")

    def test_turn_cap_is_partial_even_when_roster_says_done(self):
        self.config.turn_extension = 0
        runtime = self.runtime([tool_turn("read", "read_file", {"path": "src/a.py"}),
                                text_turn("还没有做完。" + LONG)])
        runtime.init()
        runtime.plan([{"title": "调研", "kind": "survey"}])
        with mock.patch("xiaoyu.chenshu._WORKER_ITERATIONS", 1):
            runtime.spawn("reader", "worker", mission_id="M1")
            self.join(runtime)
        member = self.sink.events[-1].members[0]
        self.assertEqual(member["status"], "done")
        self.assertEqual(member["execution_outcome"], "partial")
        self.assertEqual(member["stop_reason"], "turn_cap")
        self.assertEqual(self.sink.events[-1].missions[0]["status"], "active")

    def test_failed_member_preserves_real_error(self):
        runtime = self.runtime([RuntimeError("scripted request failed")])
        runtime.init()
        runtime.plan([{"title": "调研", "kind": "survey"}])
        runtime.spawn("reader", "worker", mission_id="M1")
        self.join(runtime)
        member = self.sink.events[-1].members[0]
        self.assertEqual(member["status"], "failed")
        self.assertEqual(member["execution_outcome"], "failed")
        self.assertIn("scripted request failed", member["error"])

    def test_adoption_links_state_but_changes_runtime_identity_and_does_not_parse_markdown(self):
        first = self.runtime()
        first.init()
        first.plan([{"title": "调研", "kind": "survey"}])
        first.members.append(Member("reader", "worker", mission_id="M1", execution_outcome="running"))
        first._save()
        first.file_finding("chenshu", "idea", "旧发现", "仅保留在旧记录")
        before = self.sink.events[-1]
        second = self.runtime()
        with mock.patch("xiaoyu.chenshu._parse_front", side_effect=AssertionError("do not reconstruct mutable prose")):
            second.init()
        after = self.sink.events[-1]
        self.assertNotEqual(before.board_id, after.board_id)
        self.assertEqual(before.source["state_id"], after.source["state_id"])
        self.assertEqual(after.source["origin"], "adopted")
        self.assertEqual(after.members[0]["status"], "retired")
        self.assertEqual(after.members[0]["execution_outcome"], "unknown")
        self.assertEqual(after.findings, ())
        self.assertTrue(any("未从可变文本重建" in gap for gap in after.gaps))

    def test_review_records_commit_without_promising_current_merge_and_logs_block_reason(self):
        runtime = self.runtime()
        runtime.init()
        runtime.plan([{"title": "实现", "scope": ["src/"]}])
        mission = runtime.missions[0]
        git(self.root, "branch", mission.branch)
        tip = git(self.root, "rev-parse", mission.branch)
        runtime.submit_review("chenshu", "M1", "clean", "已核对这个提交")
        review = self.sink.events[-1].reviews[0]
        self.assertEqual(review["reviewed_commit"], tip)
        self.assertEqual(review["commit_check"], "not_rechecked")
        self.assertTrue(review["recorded_at"])
        (self.root / "src/a.py").write_text("a = 2\n", encoding="utf-8")
        git(self.root, "add", "src/a.py")
        git(self.root, "commit", "-qm", "move tip")
        git(self.root, "branch", "-f", mission.branch, "HEAD")
        with self.assertRaisesRegex(Exception, "tip"):
            runtime.merge("M1")
        final = self.sink.events[-1]
        self.assertEqual(final.reviews[0], review)
        self.assertEqual(final.activity[-1]["action"], "merge.blocked")
        self.assertEqual(final.activity[-1]["details"]["reason"], "tip-moved")

    def test_snapshot_sink_runs_outside_runtime_lock_and_failure_does_not_break_operations(self):
        runtime = self.runtime()
        acquired = []

        class InspectingSink:
            def emit(inner, event):
                if not isinstance(event, ChenshuUpdated):
                    return
                observed = threading.Event()
                def inspect():
                    with runtime.lock:
                        observed.set()
                reader = threading.Thread(target=inspect)
                reader.start()
                acquired.append(observed.wait(1))
                reader.join(1)

        runtime.sink = InspectingSink()
        runtime.init()
        self.assertEqual(acquired, [True])
        with mock.patch.object(runtime.sink, "emit", side_effect=RuntimeError("observer unavailable")):
            runtime.plan([{"title": "调研", "kind": "survey"}])
        runtime.sink = self.sink
        runtime.status()
        self.assertEqual(self.sink.events[-1].missions[0]["id"], "M1")
        self.assertTrue(any("未能发布" in gap for gap in self.sink.events[-1].gaps))

    def test_bounded_arrays_and_text_report_omissions(self):
        runtime = self.runtime()
        runtime.init()
        runtime.missions = [Mission(f"M{i}", "题" * 500, "survey", notes=["注" * 500] * 5) for i in range(140)]
        runtime.members = [Member(f"member{i}", "worker", status="done", handoff="交" * 700,
                                  model="model-" + "m" * 600) for i in range(70)]
        for i in range(70):
            runtime._remember_view("reviews", {"id": str(i)})
            runtime._remember_view("findings", {"id": str(i)})
            runtime.log("chenshu", "fixture", index=i)
        runtime.log("chenshu", "long-detail", detail="x" * 700)
        runtime._publish_snapshot()
        final = self.sink.events[-1]
        self.assertEqual((len(final.missions), len(final.members), len(final.reviews), len(final.findings), len(final.activity)),
                         (128, 64, 64, 64, 64))
        self.assertEqual(final.limits, {"missions": 12, "members": 6, "reviews": 6, "findings": 6, "activity": 8, "controls": 0})
        self.assertTrue(all(m["title_truncated"] and m["notes_truncated"] for m in final.missions))
        self.assertTrue(all(m["handoff_truncated"] and len(m["handoff"]) == 500 for m in final.members))
        self.assertTrue(all(m["model_truncated"] and len(m["model"]) == 512 for m in final.members))
        self.assertTrue(final.activity[-1]["details_truncated"])
        self.assertEqual(len(final.activity[-1]["details"]["detail"]), 500)

    def test_readonly_board_visible_contract_keyless_snapshot(self):
        if os.environ.get("XIAOYU_SNAPSHOT") == "record":
            self.skipTest("看板协议快照使用本地确定性操作")
        runtime = self.runtime()
        with mock.patch("xiaoyu.chenshu._now_iso", return_value="2026-01-01T00:00:00Z"):
            runtime.init()
            runtime.plan([{"title": "调研", "kind": "survey"}, {"title": "实现", "scope": ["src/"], "deps": ["M1"]}])
            runtime.file_finding("chenshu", "idea", "补充验收", "实现前明确验收范围。")
        actual = self.sink.events[-1].to_dict()
        actual["board_id"] = actual["source"]["runtime_id"] = "BOARD"
        actual["source"]["state_id"] = "STATE"
        actual["source"]["workspace"] = "WORKSPACE"
        actual["source"]["protocol_root"] = "WORKSPACE/.xiaoyu/chenshu"
        actual["findings"][0]["id"] = "FINDING"
        for i, record in enumerate(actual["activity"]):
            record["id"] = f"ACTIVITY-{i}"
        text = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path = Path(__file__).parent / "fixtures" / "chenshu_board.expected.json"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            path.write_text(text, encoding="utf-8")
        self.assertEqual(text, path.read_text(encoding="utf-8", errors="replace"))
