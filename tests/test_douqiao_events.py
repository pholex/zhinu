"""斗巧事实事件、部分交付及停止边界；使用脚本模型，不访问网络。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from pathlib import Path
from unittest import mock

from xiaoyu.agent import Usage
from xiaoyu.agents import DelegationResult, RunStore
from xiaoyu.artifacts import FileArtifact
from xiaoyu.douqiao import make_douqiao_tool
from xiaoyu.errors import Interrupted, PARTIAL_OUTPUT
from xiaoyu.events import DouqiaoUpdated
from xiaoyu.providers import Registry

from .test_agent_paths import AgentTestCase, FakeClient
from .test_douqiao import LONG, READER, WRITER, VERDICT, text_turn, tool_turn


SAFE_READ = hasattr(os, "O_NOFOLLOW") and os.open in os.supports_dir_fd

class Sink:
    def __init__(self):
        self.events = []
        self.threads = []

    def emit(self, event):
        if isinstance(event, DouqiaoUpdated):
            self.events.append(event)
            self.threads.append(threading.get_ident())


class DouqiaoEventsTest(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.config.qixiang_concurrency = 1
        self.sink = Sink()
        self.runs = RunStore()

    def tool(self, script=(), *, stop=None, approve=lambda name, args: True, spec=READER):
        self.client = FakeClient(list(script))
        return make_douqiao_tool(
            [spec], self.config, Registry.for_client(self.client), Usage(), self.sink,
            approve, None, self.runs, stop_requested=stop,
        )

    def run_contest(self, tool, **kwargs):
        return tool.handler(spec="thinker", task="核查交付", contestants=2, **kwargs)

    def isolate_writer(self):
        if not shutil.which("git"):
            self.skipTest("Git is required for write isolation")
        for args in (["init", "-q"], ["add", "calc.py"],
                     ["-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", "commit", "-qm", "fixture"]):
            subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)
        patcher = mock.patch("xiaoyu.worktree.user_config_dir", return_value=self.root / "cfg")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ordered_stable_snapshots_and_real_judge_recommendation(self):
        report = self.run_contest(self.tool([text_turn(LONG), text_turn(LONG), text_turn(VERDICT)]),
                                  criteria="正确性优先", models=["model-a", "model-b"])
        events = self.sink.events
        first, final = events[0], events[-1]
        self.assertEqual([item.status for item in first.candidates], ["queued", "queued"])
        self.assertEqual(first.judge.status, "pending")
        self.assertTrue(any(item.status == "running" for event in events for item in event.candidates))
        self.assertTrue(any(event.phase == "judging" and event.judge.status == "running" for event in events))
        self.assertTrue(final.final)
        self.assertFalse(final.cancelled)
        self.assertEqual(final.phase, "settled")
        self.assertEqual(final.criteria, "正确性优先")
        self.assertEqual(final.judge.status, "completed")
        self.assertEqual(final.judge.recommendation_id, final.candidates[1].id)
        self.assertEqual(final.judge.answer, VERDICT)
        self.assertEqual([item.model for item in final.candidates], ["model-a", "model-b"])
        self.assertTrue(all(item.run_id in report and item.answer == LONG for item in final.candidates))
        self.assertEqual([e.revision for e in events], list(range(1, len(events) + 1)))
        self.assertEqual({e.contest_id for e in events}, {final.contest_id})
        self.assertTrue(all([c.id for c in e.candidates] == [c.id for c in final.candidates] for e in events))
        self.assertEqual(set(self.sink.threads), {threading.get_ident()})
        self.assertEqual(json.loads(json.dumps(final.to_dict()))["kind"], "douqiao.updated")
        self.assertLessEqual(len(events), 2 * len(final.candidates) + 3)

    def test_invalid_or_failed_judge_never_recommends(self):
        for result, expected in [
            (DelegationResult(answer="没有固定裁决"), "invalid"),
            (DelegationResult(answer="胜者：#99"), "invalid"),
            (DelegationResult(answer="胜者：#1", failure="评审断线"), "failed"),
            (DelegationResult(answer="胜者：#1", error="无法启动"), "failed"),
            (DelegationResult(answer="胜者：#1", stopped="turn_cap"), "failed"),
        ]:
            with self.subTest(expected=expected, result=result):
                self.sink.events.clear()
                with mock.patch("xiaoyu.douqiao._run_judge", return_value=result):
                    self.run_contest(self.tool([text_turn(LONG), text_turn(LONG)]))
                final = self.sink.events[-1]
                self.assertEqual(final.judge.status, expected)
                self.assertEqual(final.judge.recommendation_id, "")
                self.assertTrue(final.judge.reason)
                self.assertTrue(all(c.status == "completed" for c in final.candidates))

    def test_partial_delivery_remains_partial_after_readonly_handoff(self):
        outcomes = [DelegationResult(answer="进度", stopped="turn_cap", run_id="one"),
                    DelegationResult(answer=LONG, run_id="handoff"),
                    DelegationResult(answer=LONG, run_id="two")]
        with mock.patch("xiaoyu.douqiao.execute_delegation", side_effect=outcomes), \
                mock.patch("xiaoyu.douqiao._run_judge") as judge:
            report = self.run_contest(self.tool())
        final = self.sink.events[-1]
        self.assertEqual([c.status for c in final.candidates], ["partial", "completed"])
        self.assertEqual(final.candidates[0].answer, LONG)
        self.assertIn("撞了轮数上限", report)
        self.assertEqual(report.count(LONG), 2)
        self.assertEqual(final.judge.status, "skipped")
        judge.assert_not_called()

    def test_failure_and_frontloaded_error_are_not_completed(self):
        with mock.patch("xiaoyu.douqiao.execute_delegation", side_effect=[
                DelegationResult(failure="模型失败", run_id="partial"), DelegationResult(error="隔离未建立")]):
            self.run_contest(self.tool())
        final = self.sink.events[-1]
        self.assertEqual([c.status for c in final.candidates], ["failed", "error"])
        self.assertEqual(final.judge.status, "skipped")
        self.assertEqual(final.candidates[0].run_id, "partial")

    def test_stop_at_candidate_approval_prevents_additional_model_requests(self):
        self.isolate_writer()
        stopped = threading.Event()

        def approve(name, args):
            stopped.set()
            return False

        self.config.auto_approve = False
        tool = self.tool([tool_turn("write", "write_file", {"path": "plan.txt", "content": "blocked"}), text_turn(LONG)],
                         stop=stopped.is_set, approve=approve, spec=WRITER)
        with self.assertRaises(Interrupted):
            tool.handler(spec="builder", task="写方案", contestants=2)
        final = self.sink.events[-1]
        self.assertTrue(final.cancelled)
        self.assertTrue(final.final)
        self.assertEqual([c.status for c in final.candidates], ["aborted", "aborted"])
        self.assertEqual(final.judge.status, "aborted")
        self.assertEqual(len(self.client.completions.calls), 1)

    def test_real_isolated_writes_keep_each_candidates_delivery(self):
        self.isolate_writer()
        tool = self.tool([
            tool_turn("one", "write_file", {"path": "plan.txt", "content": "first"}), text_turn(LONG),
            tool_turn("two", "write_file", {"path": "plan.txt", "content": "second"}), text_turn(LONG),
            text_turn(VERDICT),
        ], spec=WRITER)
        tool.handler(spec="builder", task="写方案", contestants=2)
        final = self.sink.events[-1]
        self.assertFalse((self.root / "plan.txt").exists())
        self.assertEqual(len({c.workspace for c in final.candidates}), 2)
        for candidate, content in zip(final.candidates, ("first", "second")):
            self.assertEqual(candidate.status, "completed")
            self.assertEqual(candidate.workspace, candidate.worktree)
            self.assertEqual((Path(candidate.workspace) / "plan.txt").read_text(encoding="utf-8"), content)
            self.assertEqual(candidate.artifacts[0].path, "plan.txt")
            #  成果预览要不跟随链接地按目录句柄读文件，这套系统调用 Windows 上没有
            self.assertEqual(candidate.artifact_previews[0]["delivered"]["available"], SAFE_READ)

    def test_judge_interrupt_keeps_all_completed_candidates(self):
        stopped = threading.Event()

        def judging(*args, **kwargs):
            self.assertIs(kwargs["stop_requested"], stop)
            stopped.set()
            return DelegationResult(answer="胜者：#1", failure="已中止", model="actual-judge", run_id="judge")

        stop = stopped.is_set
        with mock.patch("xiaoyu.douqiao._run_judge", side_effect=judging):
            with self.assertRaises(Interrupted):
                self.run_contest(self.tool([text_turn(LONG), text_turn(LONG)], stop=stop))
        final = self.sink.events[-1]
        self.assertTrue(final.final)
        self.assertTrue(final.cancelled)
        self.assertTrue(all(c.status == "completed" and c.run_id for c in final.candidates))
        self.assertEqual(final.judge.status, "aborted")
        self.assertEqual(final.judge.model, "actual-judge")
        self.assertEqual(final.judge.recommendation_id, "")

    def test_unexpected_scheduler_failure_emits_final_without_fabricated_success(self):
        with mock.patch("xiaoyu.douqiao.run_attempts", side_effect=RuntimeError("调度故障")):
            with self.assertRaisesRegex(RuntimeError, "调度故障"):
                self.run_contest(self.tool())
        final = self.sink.events[-1]
        self.assertTrue(final.final)
        self.assertEqual(final.judge.status, "failed")
        self.assertTrue(all(c.status == "aborted" and not c.started for c in final.candidates))

    def test_real_judge_observes_parent_stop_before_accepting_verdict(self):
        if os.environ.get("XIAOYU_SNAPSHOT") == "record":
            self.skipTest("中止时序使用确定性脚本，不调用真实模型")
        stopped = threading.Event()
        tool = self.tool([text_turn(LONG), text_turn(LONG), text_turn(VERDICT)], stop=stopped.is_set)
        create = self.client.completions.create

        def stop_on_judge(**kwargs):
            response = create(**kwargs)
            if len(self.client.completions.calls) == 3:
                stopped.set()
            return response

        with mock.patch.object(self.client.completions, "create", side_effect=stop_on_judge):
            with self.assertRaises(Interrupted) as caught:
                self.run_contest(tool)
        final = self.sink.events[-1]
        self.assertEqual(len(self.client.completions.calls), 3)
        self.assertEqual(final.judge.status, "aborted")
        self.assertTrue(final.judge.run_id)
        self.assertEqual(final.judge.recommendation_id, "")
        self.assertTrue(all(c.status == "completed" for c in final.candidates))
        # Lock the externally visible interrupted report and settled event together.
        replacements = [(final.contest_id, "CONTEST"), (str(self.root), "WORKSPACE"),
                        (final.judge.run_id, "JUDGE_RUN")]
        replacements.extend((candidate.run_id, f"CANDIDATE_RUN_{candidate.index}") for candidate in final.candidates)
        settled = final.to_dict()
        # Tick batching can combine two start transitions; monotonicity is tested separately.
        settled["revision"] = "REVISION"
        actual = json.dumps({"final": settled, "report": getattr(caught.exception, PARTIAL_OUTPUT),
                             "model_requests": len(self.client.completions.calls)}, ensure_ascii=False,
                            indent=2, sort_keys=True)
        for source, target in replacements:
            #  JSON 文本里反斜杠是转义过的：Windows 路径要按转义后的样子替换
            actual = actual.replace(json.dumps(source)[1:-1], target).replace(source, target)
        path = Path(__file__).parent / "fixtures" / "douqiao_judge_stop.expected.json"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            path.write_text(actual + "\n", encoding="utf-8")
        self.assertEqual(json.loads(actual), json.loads(path.read_text(encoding="utf-8", errors="replace")))

    def test_bounded_snapshots_keep_artifact_identity_and_real_model(self):
        artifact = FileArtifact("file", "result.txt", 1, "digest", "created", "file_tool")
        previews = [{"id": "file", "delivered": {"base64": "a" * 100}}]
        result = DelegationResult(answer="字" * 5000, model="actual-model", run_id="run",
                                  workspace=str(self.root), artifacts=(artifact,), artifact_previews=previews)
        with mock.patch("xiaoyu.douqiao.execute_delegation", return_value=result), \
                mock.patch("xiaoyu.douqiao._run_judge", return_value=DelegationResult(answer="胜者：#2")), \
                mock.patch("xiaoyu.douqiao._PREVIEW_BUDGET", len(json.dumps(previews).encode())):
            self.run_contest(self.tool())
        final = self.sink.events[-1]
        self.assertEqual(final.candidates[0].artifact_previews, previews)
        self.assertEqual(final.candidates[1].artifact_previews, [])
        self.assertTrue(all(c.artifacts == (artifact,) and c.model == "actual-model" and c.answer_truncated
                            and len(c.answer) == 4000 for c in final.candidates))
        self.assertTrue(all(not c.artifact_previews and not c.artifacts
                            for e in self.sink.events[:-1] for c in e.candidates))
        previews[0]["id"] = "mutated"
        self.assertEqual(final.candidates[0].artifact_previews[0]["id"], "file")

    def test_invalid_request_does_not_emit_started_contest(self):
        result = self.tool().handler(spec="thinker", task="任务", contestants=1)
        self.assertTrue(result.startswith("ERROR"))
        self.assertEqual(self.sink.events, [])
