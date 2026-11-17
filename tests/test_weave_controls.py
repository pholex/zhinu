"""Exact child input and cancellation boundaries, exercised with scripted agents."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest import mock

from xiaoyu.agent import Agent, Usage
from xiaoyu.agents import RunStore
from xiaoyu.events import QixiangExecutionUpdated, QixiangUpdated
from xiaoyu.providers import Registry
from xiaoyu.qixiang import make_qixiang_tool
from xiaoyu.weave_controls import WeaveControls, WeaveControlError

from .test_agent_paths import AgentTestCase, FakeClient
from .test_qixiang import READER, WRITER, LONG, text_turn, tool_turn


class WeaveControlsTest(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.config.qixiang_concurrency = 1
        self.events = []
        self.sink = mock.Mock()
        self.sink.emit.side_effect = self.events.append
        self.controls = WeaveControls(self.sink)
        self.runs = RunStore()
        self.errors = []

    def tool(self, script, spec=READER, stop=None):
        self.client = FakeClient(script)
        return make_qixiang_tool([spec], self.config, Registry.for_client(self.client), Usage(),
                                 self.sink, lambda *args: True, None, self.runs, controls=self.controls,
                                 stop_requested=stop)

    def start(self, tool, spec="reader"):
        def work():
            try:
                self.report = tool.handler(spec=spec, prompt_template="{{item}}", items=["甲", "乙"], isolation="none")
            except BaseException as exc:
                self.errors.append(exc)
        thread = threading.Thread(target=work)
        thread.start()
        return thread

    def join(self, thread):
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(self.errors, [])

    def current(self):
        return next(e for e in reversed(self.events) if isinstance(e, QixiangExecutionUpdated) and e.live)

    def command(self, event, kind="message", key="message-1", text="补充核对"):
        return self.controls.control(kind, batch_id=event.batch_id, item_id=event.item_id,
                                     execution_id=event.execution_id, operation_id=key,
                                     text=text if kind == "message" else "")

    def test_real_input_acceptance_is_once_and_receipt_survives_settlement(self):
        entered, release = threading.Event(), threading.Event()
        tool = self.tool([text_turn(LONG), text_turn("补充完成" + LONG), text_turn(LONG)])
        original = self.client.completions.create
        def gated(**kwargs):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(5))
            return original(**kwargs)
        with mock.patch.object(self.client.completions, "create", side_effect=gated):
            thread = self.start(tool)
            try:
                self.assertTrue(entered.wait(2))
                event = self.current()
                record = self.command(event)
                self.assertEqual(record["status"], "queued")
                self.assertEqual(self.command(event), record)
                with self.assertRaises(WeaveControlError):
                    self.command(event, text="另一消息")
            finally:
                release.set()
                self.join(thread)
        self.assertEqual(self.command(event)["status"], "accepted")
        final = [e for e in self.events if isinstance(e, QixiangUpdated)][-1]
        source = self.runs[final.items[0].run_id]
        self.assertEqual(sum("补充核对" in str(m.get("content")) for m in source.messages), 1)
        with self.assertRaises(WeaveControlError):
            self.command(event, key="late")

    def test_before_send_stop_survives_reset_and_does_not_stop_sibling(self):
        entered, release = threading.Event(), threading.Event()
        tool = self.tool([text_turn(LONG)])
        original = Agent._begin_turn
        def gated(agent, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(5))
            return original(agent, *args, **kwargs)
        with mock.patch.object(Agent, "_begin_turn", gated):
            thread = self.start(tool)
            try:
                self.assertTrue(entered.wait(2))
                event = self.current()
                self.assertEqual(self.command(event)["status"], "queued")
                self.assertEqual(self.command(event, "stop", "stop-1")["status"], "requested")
            finally:
                release.set()
                self.join(thread)
        self.assertEqual(len(self.client.completions.calls), 1)
        final = [e for e in self.events if isinstance(e, QixiangUpdated)][-1]
        self.assertEqual([i.status for i in final.items], ["aborted", "completed"])
        self.assertTrue(final.items[0].run_id)
        snapshot = self.controls.snapshot(event.batch_id, event.item_id, event.execution_id)
        self.assertFalse(snapshot["live"])
        self.assertEqual([r["status"] for r in snapshot["controls"]], ["not_consumed", "settled"])
        snapshot = dict(snapshot, batch_id="BATCH", item_id="ITEM", execution_id="EXECUTION", workspace="WORKSPACE")
        snapshot["controls"] = [dict(r, batch_id="BATCH", item_id="ITEM", execution_id="EXECUTION",
                                     created_at="TIME", updated_at="TIME") for r in snapshot["controls"]]
        rendered = json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        path = Path(__file__).parent / "fixtures" / "weave_controls.expected.json"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            path.write_text(rendered, encoding="utf-8")
        self.assertEqual(rendered, path.read_text(encoding="utf-8", errors="replace"))

    def test_late_message_does_not_cross_automatic_follow_up_generation(self):
        tool = self.tool([text_turn("短交接"), text_turn(LONG), text_turn(LONG)])
        registered = self.controls.register
        old = []
        def register(execution, agent):
            registered(execution, agent)
            if execution.phase == "primary" and not old:
                old.append(execution)
                def late():
                    self.controls.control("message", batch_id=execution.batch_id, item_id=execution.item_id,
                                          execution_id=execution.execution_id, operation_id="late", text="不能跨代")
                    return False
                agent._on_step_input = late
        with mock.patch.object(self.controls, "register", side_effect=register):
            self.join(self.start(tool))
        execution = old[0]
        previous = self.controls.snapshot(execution.batch_id, execution.item_id, execution.execution_id)
        current = self.controls.snapshot(execution.batch_id, execution.item_id)
        self.assertEqual(previous["controls"][0]["status"], "not_consumed")
        self.assertNotEqual(previous["execution_id"], current["execution_id"])
        self.assertEqual(current["execution_phase"], "follow_up")
        self.assertEqual(current["controls"], [])
        self.assertNotIn("不能跨代", str(self.client.completions.calls[1]["messages"]))
        with self.assertRaises(WeaveControlError):
            self.controls.control("stop", batch_id=execution.batch_id, item_id=execution.item_id,
                                  execution_id=execution.execution_id, operation_id="stale-stop")

    def test_message_before_send_is_delivered_after_startup_queue_reset(self):
        entered, release = threading.Event(), threading.Event()
        tool = self.tool([text_turn(LONG), text_turn("按补充完成" + LONG), text_turn(LONG)])
        original = Agent._begin_turn
        def gated(agent, *args, **kwargs):
            if not entered.is_set():
                entered.set()
                self.assertTrue(release.wait(5))
            return original(agent, *args, **kwargs)
        with mock.patch.object(Agent, "_begin_turn", gated):
            thread = self.start(tool)
            try:
                self.assertTrue(entered.wait(2))
                event = self.current()
                self.assertEqual(event.execution_state, "starting")
                self.command(event)
            finally:
                release.set()
                self.join(thread)
        self.assertEqual(self.command(event)["status"], "accepted")
        self.assertEqual(len(self.client.completions.calls), 3)

    def test_host_approval_stop_uses_exact_identity_and_wakes_without_parent_abort(self):
        entered = threading.Event()
        self.config.auto_approve = False
        tool = self.tool([tool_turn("write", "write_file", {"path":"never.txt", "content":"x"}),
                          text_turn(LONG)], spec=WRITER)
        identities = []
        def approve(name, args, identity, stopped):
            identities.append(identity)
            entered.set()
            for _ in range(100):
                if stopped():
                    return False
                threading.Event().wait(.02)
            raise AssertionError("stop did not wake approval")
        self.controls.item_approver = approve
        thread = self.start(tool, "writer")
        try:
            self.assertTrue(entered.wait(3))
            event = self.current()
            self.command(event, "stop", "stop-approval")
        finally:
            self.join(thread)
        self.assertEqual(identities[0]["execution_id"], event.execution_id)
        self.assertEqual(identities[0]["kind"], "qixiang")
        self.assertFalse((self.root / "never.txt").exists())
        self.assertEqual([i.status for i in [e for e in self.events if isinstance(e, QixiangUpdated)][-1].items], ["aborted", "completed"])

    def test_parallel_sibling_completes_while_other_item_is_stopped(self):
        self.config.qixiang_concurrency = 2
        entered, release, sibling_done = threading.Event(), threading.Event(), threading.Event()
        tool = self.tool([])
        calls = []
        def scripted(**kwargs):
            task = kwargs["messages"][-1]["content"]
            calls.append(task)
            if task == "甲":
                entered.set()
                self.assertTrue(release.wait(5))
                return iter(text_turn("短结论"))
            sibling_done.set()
            return iter(text_turn(LONG))
        with mock.patch.object(self.client.completions, "create", side_effect=scripted):
            thread = self.start(tool)
            try:
                self.assertTrue(entered.wait(2))
                first = self.current()
                self.assertTrue(sibling_done.wait(3))
                self.command(first, "stop", "only-first")
            finally:
                release.set()
                self.join(thread)
        self.assertEqual(calls, ["甲", "乙"])
        final = [e for e in self.events if isinstance(e, QixiangUpdated)][-1]
        self.assertEqual([i.status for i in final.items], ["aborted", "completed"])
        self.assertFalse(final.items[1].stop_requested)

    def test_stop_during_approval_lock_wait_does_not_wait_for_other_item(self):
        self.config.qixiang_concurrency = 2
        self.config.auto_approve = False
        first_approval, release, second_registered = threading.Event(), threading.Event(), threading.Event()
        tool = self.tool([], spec=WRITER)
        def scripted(**kwargs):
            task = kwargs["messages"][-1]["content"]
            return iter(tool_turn("write", "write_file", {"path": f"{task}.txt", "content": "x"}))
        def approve(name, args, identity, stopped):
            first_approval.set()
            self.assertTrue(release.wait(5))
            return False
        self.controls.item_approver = approve
        register = self.controls.register
        second = []
        def registered(execution, agent):
            register(execution, agent)
            if execution.item_id.endswith(":1"):
                second.append(execution)
                second_registered.set()
        with mock.patch.object(self.client.completions, "create", side_effect=scripted), \
                mock.patch.object(self.controls, "register", side_effect=registered):
            thread = self.start(tool, "writer")
            try:
                self.assertTrue(first_approval.wait(2))
                self.assertTrue(second_registered.wait(3))
                execution = second[0]
                self.controls.control("stop", batch_id=execution.batch_id, item_id=execution.item_id,
                                      execution_id=execution.execution_id, operation_id="stop-waiter")
                for _ in range(100):
                    if self.controls.snapshot(execution.batch_id, execution.item_id)["execution_state"] == "settled":
                        break
                    threading.Event().wait(.01)
                self.assertEqual(self.controls.snapshot(execution.batch_id, execution.item_id)["execution_state"], "settled")
                # Stop the first too, so its denied write cannot trigger another scripted turn.
                first = next(e for e in self.events if isinstance(e, QixiangExecutionUpdated) and e.item_id.endswith(":0"))
                self.command(first, "stop", "stop-first")
            finally:
                release.set()
                self.join(thread)

    def test_parent_stop_latches_for_identity_approval_even_after_parent_flag_resets(self):
        from xiaoyu.errors import Interrupted

        self.config.auto_approve = False
        entered, observed, release, parent_stop = [threading.Event() for _ in range(4)]
        tool = self.tool([tool_turn("write", "write_file", {"path":"never.txt", "content":"x"})],
                         spec=WRITER, stop=parent_stop.is_set)
        def approve(name, args, identity, stopped):
            entered.set()
            for _ in range(200):
                if stopped():
                    observed.set()
                    self.assertTrue(release.wait(5))
                    return False
                threading.Event().wait(.01)
            raise AssertionError("parent stop did not reach identity approval")
        self.controls.item_approver = approve
        thread = self.start(tool, "writer")
        try:
            self.assertTrue(entered.wait(2))
            parent_stop.set()
            self.assertTrue(observed.wait(2))
            parent_stop.clear()
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], Interrupted)
        self.assertEqual(len(self.client.completions.calls), 1)
        final = [e for e in self.events if isinstance(e, QixiangUpdated)][-1]
        self.assertTrue(final.cancelled)
        self.assertEqual([i.status for i in final.items], ["aborted", "aborted"])

    def test_full_pending_queue_does_not_block_precise_stop(self):
        executions = []
        for index in range(3):
            execution = self.controls.begin("batch", f"item-{index}", "primary", "任务", threading.Event(), lambda: False)
            agent = SimpleNamespace(config=self.config, sink=mock.Mock(), _on_turn_input=None, steer=mock.Mock())
            self.controls.register(execution, agent)
            executions.append(execution)
        for execution in executions[:2]:
            for index in range(64):
                self.controls.control("message", batch_id="batch", item_id=execution.item_id,
                                      execution_id=execution.execution_id, operation_id=f"{execution.item_id}-{index}", text="排队")
        third = executions[2]
        with self.assertRaises(WeaveControlError):
            self.controls.control("message", batch_id="batch", item_id=third.item_id,
                                  execution_id=third.execution_id, operation_id="overflow", text="消息")
        record = self.controls.control("stop", batch_id="batch", item_id=third.item_id,
                                       execution_id=third.execution_id, operation_id="stop-full")
        self.assertEqual(record["status"], "requested")
        self.assertTrue(third.stop.is_set())
        self.assertFalse(executions[0].stop.is_set())
        self.assertEqual(self.controls.control("stop", batch_id="batch", item_id=third.item_id,
                                              execution_id=third.execution_id, operation_id="stop-full"), record)
        with self.assertRaises(WeaveControlError):
            self.controls.control("stop", batch_id="batch", item_id=third.item_id,
                                  execution_id=third.execution_id, operation_id="stop-another-key")
