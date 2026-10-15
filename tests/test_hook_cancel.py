"""取消落在 hook/宿主回调中时，下一项工具和模型请求必须留在执行边界之外。"""

import sys
import threading
import time
from unittest import mock

from xiaoyu.embedding import measured_send
from xiaoyu.errors import Interrupted
from xiaoyu.events import RequestStarted, ToolRunning
from xiaoyu.hooks import Decision, Hook, HookEngine

from .test_agent_paths import AgentTestCase
from .test_hooks import text_turn, tool_turn
from .test_render import RecordingSink


class CancellingHook:
    def __init__(self, event, cancel, blocked=False):
        self.event, self.cancel, self.blocked = event, cancel, blocked
        self.fired = []

    def has(self, event):
        return event == self.event

    def fire(self, event, payload, **kwargs):
        self.fired.append(event)
        self.cancel()
        return Decision(self.blocked, reason="有限测试反馈")


class HookCancellationTest(AgentTestCase):
    def test_real_hook_cancel_from_host_thread_stops_at_return(self):
        for event in ("UserPromptSubmit", "PreToolUse"):
            with self.subTest(event=event):
                ready = self.root / (event + ".ready")
                release = self.root / (event + ".release")
                script = self.root / (event + ".py")
                script.write_text(
                    "from pathlib import Path\nimport time\n"
                    f"Path({ready.name!r}).touch()\n"
                    "deadline = time.monotonic() + 5\n"
                    f"while not Path({release.name!r}).exists() and time.monotonic() < deadline:\n"
                    "    time.sleep(0.01)\n",
                    encoding="utf-8",
                )
                turns = [text_turn("不应请求")]
                if event == "PreToolUse":
                    turns.insert(0, tool_turn("write_file", {"path": "real.txt", "content": "no"}))
                agent = self.build(turns, sink=RecordingSink(), hook_engine=HookEngine(
                    [Hook(event, f'"{sys.executable}" "{script}"', timeout=8)], self.root
                ))
                results, failures = [], []

                def send():
                    try:
                        results.append(measured_send(agent, "取消测试"))
                    except BaseException as exc:
                        failures.append(exc)

                worker = threading.Thread(target=send)
                worker.start()
                try:
                    deadline = time.monotonic() + 4
                    while not ready.exists() and worker.is_alive() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertTrue(ready.exists(), failures)
                    agent.interrupt()
                finally:
                    release.touch()
                    worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                self.assertTrue(results[0].interrupted)
                self.assertEqual(len(self.client.completions.calls), int(event == "PreToolUse"))
                self.assertFalse((self.root / "real.txt").exists())

    def test_pretool_cancel_does_not_write_or_request_again(self):
        for blocked in (False, True):
            for upstream in (False, True):
                with self.subTest(blocked=blocked, upstream=upstream):
                    recorder = RecordingSink()
                    stop = threading.Event()
                    agent = self.build([
                        tool_turn("write_file", {"path": "cancelled.txt", "content": "must not write"}),
                        text_turn("下一轮正常"),
                    ], sink=recorder, upstream_stop=stop.is_set if upstream else None)
                    agent.hook_engine = CancellingHook(
                        "PreToolUse", stop.set if upstream else agent.interrupt, blocked
                    )
                    result = measured_send(agent, "写文件")
                    self.assertTrue(result.interrupted)
                    self.assertFalse((self.root / "cancelled.txt").exists())
                    self.assertEqual(len(self.client.completions.calls), 1)
                    kinds = recorder.kinds()
                    self.assertEqual(kinds.count("tool.pending"), 1)
                    self.assertEqual(kinds.count("tool.denied"), 1)
                    self.assertNotIn("tool.running", kinds)
                    self.assertNotIn("tool.completed", kinds)
                    self.assertEqual(len([m for m in agent.messages if m["role"] == "tool"]), 1)
                    stop.clear()
                    agent.hook_engine = None
                    resumed = measured_send(agent, "继续")
                    self.assertFalse(resumed.interrupted)
                    self.assertEqual(resumed.text, "下一轮正常")

    def test_prompt_hook_cancel_never_starts_model(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                recorder = RecordingSink()
                agent = self.build([text_turn("不应请求")], sink=recorder)
                agent.hook_engine = CancellingHook("UserPromptSubmit", agent.interrupt, blocked)
                self.assertTrue(measured_send(agent, "测试取消").interrupted)
                self.assertEqual(self.client.completions.calls, [])
                self.assertNotIn("request.started", recorder.kinds())

    def test_posttool_cancel_preserves_executed_result(self):
        recorder = RecordingSink()
        agent = self.build([
            tool_turn("write_file", {"path": "done.txt", "content": "already written"}),
            text_turn("下一轮正常"),
        ], sink=recorder)
        agent.hook_engine = CancellingHook("PostToolUse", agent.interrupt)
        self.assertTrue(measured_send(agent, "写文件").interrupted)
        self.assertEqual(len(self.client.completions.calls), 1)
        self.assertEqual((self.root / "done.txt").read_text(encoding="utf-8"), "already written")
        completed = [e for e in recorder.events if e.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0].ok)
        results = [m for m in agent.messages if m["role"] == "tool"]
        self.assertEqual(len(results), 1)
        self.assertIn("已创建", results[0]["content"])
        agent.hook_engine = None
        self.assertEqual(measured_send(agent, "继续").text, "下一轮正常")

    def test_failed_tool_hook_cancel_preserves_error_without_retry(self):
        recorder = RecordingSink()
        agent = self.build([tool_turn("read_file", {"path": "missing.txt"}), text_turn("不应请求")],
                           sink=recorder)
        agent.hook_engine = CancellingHook("ToolFailed", agent.interrupt)
        self.assertTrue(measured_send(agent, "读文件").interrupted)
        self.assertEqual(len(self.client.completions.calls), 1)
        self.assertEqual(recorder.kinds().count("tool.completed"), 1)
        results = [m for m in agent.messages if m["role"] == "tool"]
        self.assertEqual(len(results), 1)
        self.assertIn("文件不存在", results[0]["content"])

    def test_stop_hook_cancel_wins_over_continue_or_finish(self):
        for blocked in (False, True):
            with self.subTest(blocked=blocked):
                agent = self.build([text_turn("当前回答"), text_turn("不应请求")], sink=RecordingSink())
                agent.hook_engine = CancellingHook("Stop", agent.interrupt, blocked)
                self.assertTrue(measured_send(agent, "回答").interrupted)
                self.assertEqual(len(self.client.completions.calls), 1)
                self.assertEqual(agent.last_assistant_text(), "当前回答")

    def test_beforecompact_cancel_does_not_call_summarizer(self):
        agent = self.build([], sink=RecordingSink())
        agent.hook_engine = CancellingHook("BeforeCompact", agent.interrupt)
        with mock.patch.object(agent.compactor, "compact", return_value=(agent.messages, "unchanged")) as compact:
            with self.assertRaises(Interrupted):
                agent.maybe_compact(force=True)
            compact.assert_not_called()

    def test_aftercompact_cancel_preserves_completed_compaction(self):
        agent = self.build([], sink=RecordingSink())
        replacement = [agent.messages[0], {"role": "user", "content": "压缩后历史"}]
        agent.hook_engine = CancellingHook("AfterCompact", agent.interrupt)
        with mock.patch.object(agent.compactor, "compact", return_value=(replacement, "完成压缩")):
            with self.assertRaises(Interrupted):
                agent.maybe_compact(force=True)
        self.assertEqual(agent.messages, replacement)

    def test_session_end_hook_still_runs_with_pending_interrupt(self):
        agent = self.build([], sink=RecordingSink())
        engine = CancellingHook("SessionEnd", lambda: None)
        agent.hook_engine = engine
        agent.begin_session()
        agent.interrupt()
        agent.end_session()
        self.assertEqual(engine.fired, ["SessionEnd"])

    def test_approval_cancel_does_not_start_pretool_hook(self):
        self.config.auto_approve = False
        agent = self.build([
            tool_turn("write_file", {"path": "approval.txt", "content": "must not write"}),
            text_turn("不应请求"),
        ], sink=RecordingSink())

        def approve(name, args):
            agent.interrupt()
            return True

        agent.approver = approve
        agent.hook_engine = CancellingHook("PreToolUse", lambda: None)
        self.assertTrue(measured_send(agent, "写文件").interrupted)
        self.assertEqual(agent.hook_engine.fired, [])
        self.assertFalse((self.root / "approval.txt").exists())
        self.assertEqual(len(self.client.completions.calls), 1)

    def test_tool_running_observer_can_cancel_before_side_effect(self):
        recorder = RecordingSink()
        agent = self.build([
            tool_turn("write_file", {"path": "observed.txt", "content": "must not write"}),
            text_turn("不应请求"),
        ], sink=recorder)
        original = recorder.emit

        def emit(event):
            original(event)
            if isinstance(event, ToolRunning):
                agent.interrupt()

        recorder.emit = emit
        self.assertTrue(measured_send(agent, "写文件").interrupted)
        self.assertFalse((self.root / "observed.txt").exists())
        self.assertEqual(len(self.client.completions.calls), 1)
        completed = [e for e in recorder.events if e.kind == "tool.completed"]
        self.assertEqual(len(completed), 1)
        self.assertFalse(completed[0].ok)

    def test_request_started_observer_can_cancel_before_transport(self):
        recorder = RecordingSink()
        agent = self.build([text_turn("不应请求")], sink=recorder)
        original = recorder.emit

        def emit(event):
            original(event)
            if isinstance(event, RequestStarted):
                agent.interrupt()

        recorder.emit = emit
        self.assertTrue(measured_send(agent, "回答").interrupted)
        self.assertEqual(self.client.completions.calls, [])
        self.assertEqual(recorder.kinds(), ["request.started", "request.ended"])
