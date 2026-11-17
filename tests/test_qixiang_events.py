"""七襄快照来自调度事实；不解析报告，不调用真实模型。"""
from __future__ import annotations

import json
import base64
import os
import threading
import unittest
from unittest import mock

from xiaoyu.agents import DelegationResult, RunStore
from xiaoyu.artifacts import FileArtifact
from xiaoyu.errors import Interrupted
from xiaoyu.events import QixiangUpdated
from xiaoyu.qixiang import make_qixiang_tool
from xiaoyu.agent import Usage
from xiaoyu.providers import Registry

from .test_agent_paths import AgentTestCase, FakeClient
from .test_qixiang import LONG, READER, WRITER, text_turn, tool_turn


class RecordingSink:
    def __init__(self):
        self.events = []
        self.threads = []

    def emit(self, event):
        if isinstance(event, QixiangUpdated):
            self.events.append(event)
            self.threads.append(threading.get_ident())


class QixiangEventsTest(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.config.qixiang_concurrency = 1
        self.sink = RecordingSink()
        self.runs = RunStore()

    def tool(self, script=(), stop=None, specs=(READER,)):
        return make_qixiang_tool(
            list(specs), self.config, Registry.for_client(FakeClient(list(script))),
            Usage(), self.sink, lambda name, args: True, None, self.runs,
            stop_requested=stop,
        )

    def call(self, tool, items=("甲", "乙")):
        return tool.handler(spec="reader", prompt_template="{{item}}", items=list(items))

    def test_real_delegations_emit_ordered_stable_snapshots_and_final_answers(self):
        tool = self.tool([text_turn("甲" + LONG), text_turn("乙" + LONG)])
        report = self.call(tool)
        snapshots = self.sink.events
        self.assertEqual([i.status for i in snapshots[0].items], ["queued", "queued"])
        self.assertTrue(any(i.status == "running" for e in snapshots for i in e.items))
        final = snapshots[-1]
        self.assertTrue(final.final)
        self.assertFalse(final.cancelled)
        self.assertEqual([i.status for i in final.items], ["completed", "completed"])
        self.assertEqual([i.answer for i in final.items], ["甲" + LONG, "乙" + LONG])
        self.assertTrue(all(i.run_id in report and i.model for i in final.items))
        self.assertTrue(all(i.started for i in final.items))
        self.assertEqual({e.batch_id for e in snapshots}, {final.batch_id})
        self.assertEqual([e.revision for e in snapshots], list(range(1, len(snapshots) + 1)))
        self.assertTrue(all([i.id for i in e.items] == [i.id for i in final.items] for e in snapshots))
        self.assertTrue(all(not i.answer for e in snapshots[:-1] for i in e.items))
        self.assertEqual(set(self.sink.threads), {threading.get_ident()})
        self.assertEqual(final.to_dict()["kind"], "qixiang.updated")
        self.assertEqual(json.loads(json.dumps(final.to_dict()))["items"][0]["run_id"], final.items[0].run_id)
        # Old snapshots are immutable values, not references to mutable scheduler state.
        self.assertEqual([i.status for i in snapshots[0].items], ["queued", "queued"])

    def test_terminal_states_are_not_inferred_from_answer_text(self):
        outcomes = {
            "成功": DelegationResult(answer=LONG, run_id="success"),
            "部分": DelegationResult(answer="完成了全部工作", stopped="turn_cap", run_id="partial"),
            "校验": DelegationResult(error="隔离未建立"),
            "失败": DelegationResult(failure="执行失败", run_id="failed"),
        }

        def delegation(*args, **kwargs):
            if kwargs["task"] == "崩溃":
                raise RuntimeError("线程异常")
            return outcomes[kwargs["task"]]

        with mock.patch("xiaoyu.qixiang.execute_delegation", side_effect=delegation), \
                mock.patch("xiaoyu.qixiang.MIN_ANSWER_CHARS", 0):
            self.call(self.tool(), (*outcomes, "崩溃"))
        items = self.sink.events[-1].items
        self.assertEqual([i.status for i in items], ["completed", "partial", "error", "failed", "failed"])
        self.assertEqual(items[2].error, "隔离未建立")
        self.assertIn("线程异常", items[-1].error)

    def test_cancelled_queue_has_final_snapshot_and_no_fake_completed_items(self):
        started = threading.Event()
        stopped = threading.Event()

        class RunningAgent:
            def interrupt(self):
                stopped.set()

        def delegation(*args, **kwargs):
            kwargs["on_agent"](RunningAgent())
            started.set()
            if not stopped.wait(5):
                raise RuntimeError("scheduler did not interrupt the child")
            return DelegationResult(failure="被中止", answer="部分进度", run_id="saved")

        with mock.patch("xiaoyu.qixiang.execute_delegation", side_effect=delegation):
            with self.assertRaises(Interrupted):
                self.call(self.tool(stop=started.is_set), ("执行中", "排队一", "排队二"))
        final = self.sink.events[-1]
        self.assertTrue(final.final)
        self.assertTrue(final.cancelled)
        self.assertEqual([i.status for i in final.items], ["aborted"] * 3)
        self.assertTrue(final.items[0].started)
        self.assertEqual(final.items[0].run_id, "saved")
        self.assertFalse(final.items[1].started)
        self.assertFalse(final.items[2].started)
        self.assertIn("尚未开始", final.items[1].error)

    def test_resume_keeps_source_handle_but_gets_a_new_batch_and_result(self):
        tool = self.tool([text_turn(LONG), text_turn(LONG), text_turn("续跑" + LONG)])
        self.call(tool)
        previous = self.sink.events[-1]
        handle = previous.items[0].run_id
        tool.handler(spec="reader", resume={handle: "继续核查"})
        final = self.sink.events[-1]
        self.assertNotEqual(final.batch_id, previous.batch_id)
        self.assertEqual(len(final.items), 1)
        self.assertEqual(final.items[0].resume_from, handle)
        self.assertNotEqual(final.items[0].run_id, handle)
        self.assertEqual(final.items[0].answer, "续跑" + LONG)
        self.assertEqual(final.items[0].status, "completed")

    def test_stop_during_approval_prevents_next_model_turn_and_queued_items(self):
        stopped = threading.Event()
        client = FakeClient([
            tool_turn("write", "write_file", {"path": "stopped.txt", "content": "must not exist"}),
            text_turn("must not consume this turn" + LONG), text_turn(LONG),
        ])

        def approve(name, args):
            stopped.set()
            return False

        self.config.auto_approve = False
        tool = make_qixiang_tool(
            [WRITER], self.config, Registry.for_client(client), Usage(), self.sink,
            approve, None, self.runs, stop_requested=stopped.is_set,
        )
        with self.assertRaises(Interrupted):
            tool.handler(spec="writer", prompt_template="{{item}}", items=["当前项", "排队项"], isolation="none")
        self.assertEqual(len(client.completions.calls), 1)
        self.assertFalse((self.root / "stopped.txt").exists())
        final = self.sink.events[-1]
        self.assertTrue(final.cancelled)
        self.assertEqual([item.status for item in final.items], ["aborted", "aborted"])

    def test_large_answers_are_bounded_only_in_snapshots(self):
        answer = "正文" * 8000
        tool = self.tool([text_turn(answer), text_turn(answer)])
        self.call(tool)
        final = self.sink.events[-1]
        self.assertTrue(all(i.answer_truncated and len(i.answer) <= 4000 for i in final.items))
        self.assertTrue(all(self.runs[i.run_id].messages for i in final.items))
        self.assertLessEqual(len(self.sink.events), 2 * len(final.items) + 2)

    def test_invalid_batch_does_not_emit_a_started_batch(self):
        result = self.call(self.tool(), ("唯一任务",))
        self.assertTrue(result.startswith("ERROR"))
        self.assertEqual(self.sink.events, [])

    def test_scheduler_exception_still_publishes_final_without_success(self):
        with mock.patch("xiaoyu.qixiang.run_attempts", side_effect=RuntimeError("调度失败")):
            with self.assertRaisesRegex(RuntimeError, "调度失败"):
                self.call(self.tool())
        final = self.sink.events[-1]
        self.assertTrue(final.final)
        self.assertTrue(final.cancelled)
        self.assertEqual([item.status for item in final.items], ["aborted", "aborted"])
        self.assertTrue(all(not item.started for item in final.items))

    def test_readonly_handoff_followup_keeps_primary_file_and_before_preview(self):
        (self.root / "result.txt").write_text("before", encoding="utf-8")
        tool = self.tool([
            tool_turn("read", "read_file", {"path": "result.txt"}),
            tool_turn("write", "write_file", {"path": "result.txt", "content": "after"}),
            text_turn("已写"), text_turn("完整交接" + LONG), text_turn(LONG),
        ], specs=(WRITER,))
        tool.handler(spec="writer", prompt_template="{{item}}", items=["更新", "核查"], isolation="none")
        item = self.sink.events[-1].items[0]
        self.assertEqual(item.status, "completed")
        self.assertEqual(item.answer, "完整交接" + LONG)
        self.assertEqual(item.workspace, str(self.root))
        self.assertEqual([artifact.path for artifact in item.artifacts], ["result.txt"])
        preview, = item.artifact_previews
        self.assertEqual(preview["workspace"], str(self.root))
        if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
            self.assertEqual(base64.b64decode(preview["delivered"]["base64"]), b"after")
            self.assertEqual(base64.b64decode(preview["before"]["base64"]), b"before")
        else:
            self.assertFalse(preview["delivered"]["available"])
            self.assertIn("此平台", preview["note"])
        self.assertTrue(all(not row.artifacts and not row.artifact_previews
                            for event in self.sink.events[:-1] for row in event.items))

    def test_failed_child_preserves_incomplete_artifact(self):
        tool = self.tool([
            tool_turn("write", "write_file", {"path": "partial.txt", "content": "partial"}),
            RuntimeError("fixture failure"), text_turn(LONG),
        ], specs=(WRITER,))
        tool.handler(spec="writer", prompt_template="{{item}}", items=["部分失败", "完成"], isolation="none")
        item = self.sink.events[-1].items[0]
        self.assertEqual(item.status, "failed")
        artifact, = item.artifacts
        self.assertEqual((artifact.path, artifact.status), ("partial.txt", "incomplete"))
        self.assertEqual(item.artifact_previews[0]["delivered"]["available"],
                         os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"))

    def test_batch_preview_budget_drops_bytes_but_preserves_artifact_identity(self):
        preview = {"id": "artifact", "delivered": {"base64": "x" * 100}}
        artifact = FileArtifact("artifact", "report.txt", 10, "hash", "created", "file_tool")
        result = DelegationResult(answer=LONG, artifacts=(artifact,), workspace=str(self.root),
                                  artifact_previews=[preview])
        budget = len(json.dumps([preview], ensure_ascii=False).encode("utf-8"))
        with mock.patch("xiaoyu.qixiang.execute_delegation", return_value=result), \
                mock.patch("xiaoyu.qixiang._PREVIEW_BUDGET", budget):
            self.call(self.tool())
        first, second = self.sink.events[-1].items
        self.assertEqual(first.artifact_previews, [preview])
        self.assertEqual(second.artifact_previews, [])
        self.assertEqual(first.artifacts, second.artifacts)


if __name__ == "__main__":
    unittest.main()
