"""render/events 层的测试。

锁住三件事：
1. Agent 的所有面向用户输出都走 sink 事件——注入 RecordingSink 后 stdout 必须为空；
2. 工具生命周期状态机：pending → running → completed|denied，
   每个 pending 恰好一个终态；
3. PlainSink 的渲染与最初 print 行为一致（关键片段抽查），事件可 JSON 序列化。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from xiaoyu.events import (
    Notice,
    PlanUpdated,
    RequestEnded,
    RequestStarted,
    TextDelta,
    TextEnd,
    ToolCompleted,
    ToolDenied,
    ToolPending,
    ToolPurpose,
    ToolRunning,
    UIEvent,
)
from xiaoyu.render import NullSink, PlainSink

from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk


class RecordingSink:
    """把事件按序记下来，供断言。"""

    def __init__(self) -> None:
        self.events: list[UIEvent] = []

    def emit(self, event: UIEvent) -> None:
        self.events.append(event)

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events]


class TestAgentEmitsSinkEvents(AgentTestCase):
    def test_tool_loop_event_sequence_and_silent_stdout(self) -> None:
        """一轮「调工具 → 出正文」的事件序列；注入 sink 后 agent 自己不碰 stdout。"""
        first = [
            chunk(tool_calls=[call_fragment(0, "c1", "read_file", '{"path": "calc.py"}')]),
            usage_chunk(500, 30),
        ]
        second = [chunk(content="文件里"), chunk(content="有 add"), usage_chunk(700, 40)]
        recorder = RecordingSink()
        agent = self.build([first, second], sink=recorder)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send("看一下 calc.py")

        self.assertEqual(buffer.getvalue(), "", "注入 sink 后 agent 不应再直接打印")
        #  每次模型调用都由 request.started/ended 包住——这一对覆盖的正是
        #  "请求已发出、还没有任何输出"那段空白，前端靠它才有东西可画
        self.assertEqual(
            recorder.kinds(),
            [
                "request.started", "request.ended",
                "tool.pending", "tool.running", "tool.completed",
                "request.started", "text.delta", "text.delta", "text.end", "request.ended",
            ],
        )
        started = [e for e in recorder.events if e.kind == "request.started"]
        self.assertEqual([e.model for e in started], ["main-model", "main-model"])
        pending, running, completed = recorder.events[2:5]
        self.assertEqual(pending.name, "read_file")
        self.assertEqual(pending.args, {"path": "calc.py"})
        self.assertEqual(running.args, {"path": "calc.py"})
        self.assertTrue(completed.ok)
        self.assertIn("def add", completed.output)
        self.assertGreaterEqual(completed.seconds, 0.0)

    def test_denied_by_rule_is_terminal_state(self) -> None:
        """deny 规则命中：pending 之后终态是 denied(by=rule)，不出现 running。"""
        from xiaoyu.permissions import Permissions, parse_rule

        permissions = Permissions(self.root)
        rule = parse_rule("deny read_file")
        assert rule is not None
        permissions.rules.append(rule)
        recorder = RecordingSink()
        first = [chunk(tool_calls=[call_fragment(0, "c1", "read_file", '{"path": "calc.py"}')])]
        agent = self.build([first, [chunk(content="好")]], sink=recorder, permissions=permissions)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("读一下")

        tool_kinds = [k for k in recorder.kinds() if k.startswith("tool.")]
        self.assertEqual(tool_kinds, ["tool.pending", "tool.denied"])
        denied = next(e for e in recorder.events if isinstance(e, ToolDenied))
        self.assertEqual(denied.by, "rule")

    def test_denied_by_user_is_terminal_state(self) -> None:
        """用户在确认框拒绝：终态 denied(by=user)。"""
        self.config.auto_approve = False
        recorder = RecordingSink()
        first = [chunk(tool_calls=[call_fragment(0, "c1", "write_file", '{"path": "a.txt", "content": "x"}')])]
        agent = self.build(
            [first, [chunk(content="好")]],
            sink=recorder,
            approver=lambda name, args: False,
        )

        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("写文件")

        tool_kinds = [k for k in recorder.kinds() if k.startswith("tool.")]
        self.assertEqual(tool_kinds, ["tool.pending", "tool.denied"])
        denied = next(e for e in recorder.events if isinstance(e, ToolDenied))
        self.assertEqual(denied.by, "user")

    def test_max_iterations_notice_goes_through_sink(self) -> None:
        self.config.max_iterations = 1
        forever = [chunk(tool_calls=[call_fragment(0, "c1", "read_file", '{"path": "calc.py"}')])]
        wrapup = [chunk(content="收尾")]
        recorder = RecordingSink()
        agent = self.build([forever, wrapup], sink=recorder)

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send("无限循环")

        self.assertEqual(buffer.getvalue(), "")
        notices = [e for e in recorder.events if isinstance(e, Notice)]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0].level, "warn")
        self.assertIn("已达到单轮工具调用上限", notices[0].text)

    def test_update_plan_emits_plan_event(self) -> None:
        recorder = RecordingSink()
        agent = self.build([], sink=recorder)
        result = agent._update_plan(
            [
                {"step": "读文件", "status": "completed"},
                {"step": "改代码", "status": "in_progress"},
            ],
            explanation="开始动手",
        )
        self.assertEqual(result, "已更新计划")
        self.assertEqual(recorder.kinds(), ["plan.updated"])
        event = recorder.events[0]
        self.assertEqual([i["status"] for i in event.plan], ["completed", "in_progress"])
        self.assertEqual(event.explanation, "开始动手")

    def test_request_events_are_balanced_even_when_the_call_blows_up(self) -> None:
        """不变量：每个 request.started 恰好收到一个 request.ended。

        异常路径尤其要成立——ended 是前端收活区的兜底信号，漏一次 spinner
        就永远转下去了。
        """

        class Exploding:
            class chat:  # noqa: N801 - 模仿 OpenAI client 的属性形状
                class completions:
                    @staticmethod
                    def create(**_kwargs):
                        raise RuntimeError("端点炸了")

        recorder = RecordingSink()
        agent = self.build([], sink=recorder)
        agent.client = Exploding()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.suppress(Exception):
            agent.send("会失败的请求")

        kinds = recorder.kinds()
        self.assertEqual(kinds.count("request.started"), kinds.count("request.ended"))
        self.assertGreater(kinds.count("request.started"), 0, "没发出请求，这测试就空转了")

    def test_invalid_plan_emits_nothing(self) -> None:
        recorder = RecordingSink()
        agent = self.build([], sink=recorder)
        result = agent._update_plan([{"step": "x", "status": "done"}])
        self.assertIn("ERROR", result)
        self.assertEqual(recorder.events, [])


class TestEventSerialization(unittest.TestCase):
    def test_all_events_json_roundtrip(self) -> None:
        """事件即协议：每种事件都能 to_dict + json.dumps（headless/SSE 的前提）。"""
        samples = [
            TextDelta("正文"),
            TextEnd(),
            ToolPending("bash", {"command": "ls"}),
            ToolPurpose("bash", "看目录"),
            ToolRunning("bash", {"command": "ls"}),
            ToolCompleted("bash", output="a.txt", ok=True, seconds=0.12),
            ToolDenied("bash", by="rule"),
            PlanUpdated([{"step": "x", "status": "pending"}], "原因"),
            Notice("[提示]", "warn"),
            RequestStarted("deepseek-v4-pro"),
            RequestEnded(),
        ]
        kinds = set()
        for event in samples:
            data = event.to_dict()
            self.assertIn("kind", data)
            kinds.add(data["kind"])
            json.dumps(data, ensure_ascii=False)  # 不可序列化会抛
        self.assertEqual(len(kinds), len(samples), "kind 必须互不相同")


class TestPlainSink(unittest.TestCase):
    def render(self, sink: PlainSink, event: UIEvent) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            sink.emit(event)
        return buffer.getvalue()

    def test_tool_pending_formats_like_before(self) -> None:
        out = self.render(PlainSink(), ToolPending("bash", {"command": "ls -la"}))
        self.assertIn("⚙ bash", out)
        self.assertIn("ls -la", out)

    def test_tool_completed_previews_first_line(self) -> None:
        out = self.render(
            PlainSink(), ToolCompleted("grep", output="第一行\n第二行不该出现", ok=True, seconds=0.1)
        )
        self.assertIn("↳ 第一行", out)
        self.assertNotIn("第二行", out)

    def test_request_started_only_speaks_to_a_real_terminal(self) -> None:
        """明文没有活区，只能实打实占一行。管道/CI 里那是污染（`xy "..." > out`
        不该混进进度行），有人盯着看时才值得打。"""
        class TtyBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        sink = PlainSink()
        #  单测里 stdout 被重定向到普通缓冲，isatty 为假 → 保持静默
        self.assertEqual(self.render(sink, RequestStarted("m")), "")

        buffer = TtyBuffer()
        with contextlib.redirect_stdout(buffer):
            sink.emit(RequestStarted("deepseek-v4-pro"))
        out = buffer.getvalue()
        self.assertIn("deepseek-v4-pro", out)
        self.assertIn("思考中", out)

    def test_osc133_anchors_only_on_terminal(self) -> None:
        """assistant 正文块的 OSC 133 锚点：终端里首尾各一次、成对收束；
        管道里一个字节都不该有（零宽标记进文件也是污染）。"""
        from xiaoyu.render import OSC133_TEXT_END, OSC133_TEXT_START

        class TtyBuffer(io.StringIO):
            def isatty(self) -> bool:
                return True

        #  管道：无锚点
        sink = PlainSink()
        out = self.render(sink, TextDelta("正文")) + self.render(sink, TextEnd())
        self.assertNotIn("\x1b]133", out)

        #  终端：块首 A 一次（多个 delta 不重复），块尾 B/C 收束
        sink = PlainSink()
        buffer = TtyBuffer()
        with contextlib.redirect_stdout(buffer):
            sink.emit(TextDelta("第一段"))
            sink.emit(TextDelta("第二段"))
            sink.emit(TextEnd())
        out = buffer.getvalue()
        self.assertEqual(out.count(OSC133_TEXT_START), 1)
        self.assertEqual(out.count(OSC133_TEXT_END), 1)
        self.assertTrue(out.startswith(OSC133_TEXT_START))
        #  下一个正文块重新开锚（每段回复都是独立的导航块）
        buffer2 = TtyBuffer()
        with contextlib.redirect_stdout(buffer2):
            sink.emit(TextDelta("下一块"))
            sink.emit(TextEnd())
        self.assertEqual(buffer2.getvalue().count(OSC133_TEXT_START), 1)

    def test_new_lifecycle_events_render_nothing_in_plain(self) -> None:
        """running/denied 是给活区和 headless 准备的，明文渲染保持原有输出不变。"""
        sink = PlainSink()
        self.assertEqual(self.render(sink, ToolRunning("bash", {"command": "ls"})), "")
        self.assertEqual(self.render(sink, ToolDenied("bash", by="user")), "")

    def test_quiet_subagent_indents_tools_and_mutes_content(self) -> None:
        sink = PlainSink(indent="    ", verbose=False)
        self.assertEqual(self.render(sink, TextDelta("正文")), "")
        self.assertEqual(self.render(sink, TextEnd()), "")
        self.assertEqual(self.render(sink, PlanUpdated([], "")), "")
        out = self.render(sink, ToolPending("grep", {"pattern": "x"}))
        self.assertTrue(out.startswith("    "))

    def test_plan_marks(self) -> None:
        out = self.render(
            PlainSink(),
            PlanUpdated(
                [
                    {"step": "读文件", "status": "completed"},
                    {"step": "改代码", "status": "in_progress"},
                    {"step": "跑测试", "status": "pending"},
                ],
                "",
            ),
        )
        self.assertIn("✔ 读文件", out)
        self.assertIn("▶ 改代码", out)
        self.assertIn("○ 跑测试", out)

    def test_notice_levels_do_not_crash_and_pass_text(self) -> None:
        for level in ("info", "warn", "error"):
            out = self.render(PlainSink(), Notice(f"[{level} 提示]", level))
            self.assertIn(f"[{level} 提示]", out)

    def test_null_sink_is_silent(self) -> None:
        sink = NullSink()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            for event in (
                TextDelta("x"),
                TextEnd(),
                ToolPending("bash", {}),
                ToolRunning("bash", {}),
                ToolCompleted("bash", output="输出", ok=True, seconds=0.1),
                ToolDenied("bash", by="rule"),
                PlanUpdated([{"step": "x", "status": "pending"}], ""),
                Notice("提示", "warn"),
            ):
                sink.emit(event)
        self.assertEqual(buffer.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
