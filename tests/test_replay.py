"""resume 回放（render.replay_transcript）的测试。

锁住三件事：
1. 历史消息 → 事件流的翻译规则：user 走 Notice「› …」、assistant 正文走
   TextDelta/TextEnd、工具调用在结果处 pending→completed 成对补发；
2. 边界：synthetic user 文案跳过、超长截断、坏参数 JSON 容错、
   没等到结果的调用不补发、空输入返回 0 且不打 header；
3. 两个真实 sink（PlainSink / RichSink）消费回放事件不崩且关键内容在场。
"""

from __future__ import annotations

import contextlib
import io
import unittest

from xiaoyu.events import Notice, TextDelta, ToolCompleted, ToolPending
from xiaoyu.render import _REPLAY_TEXT_LINES, _REPLAY_USER_LINES, PlainSink, replay_transcript

from .test_render import RecordingSink


def assistant_call(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


class TestReplayTranscript(unittest.TestCase):
    def test_full_turn_event_sequence(self) -> None:
        """一轮「提问 → 调工具 → 出正文」重建出的事件序列与关键字段。"""
        messages = [
            {"role": "user", "content": "看一下 calc.py"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [assistant_call("c1", "read_file", '{"path": "calc.py"}')],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "def add(a, b):\n    return a + b"},
            {"role": "assistant", "content": "文件里有 add 函数"},
        ]
        sink = RecordingSink()
        count = replay_transcript(messages, sink, header="── 回放 ──")

        self.assertEqual(count, 3, "assistant 只有 tool_calls 没正文，不计入产出")
        self.assertEqual(
            sink.kinds(),
            [
                "notice",  # header
                "notice",  # › 看一下 calc.py
                "tool.pending", "tool.completed",
                "text.delta", "text.end",
                "notice",  # 收尾空行
            ],
        )
        self.assertEqual(sink.events[0].text, "── 回放 ──")
        self.assertEqual(sink.events[1].text, "› 看一下 calc.py")
        pending = sink.events[2]
        self.assertIsInstance(pending, ToolPending)
        self.assertEqual(pending.name, "read_file")
        self.assertEqual(pending.args, {"path": "calc.py"})
        completed = sink.events[3]
        self.assertIsInstance(completed, ToolCompleted)
        self.assertTrue(completed.ok)
        self.assertIn("def add", completed.output)
        self.assertEqual(sink.events[4].text, "文件里有 add 函数")

    def test_error_output_marks_not_ok(self) -> None:
        messages = [
            {"role": "assistant", "content": None,
             "tool_calls": [assistant_call("c1", "bash", '{"command": "false"}')]},
            {"role": "tool", "tool_call_id": "c1", "content": "ERROR: 命令退出码 1"},
        ]
        sink = RecordingSink()
        replay_transcript(messages, sink)
        completed = next(e for e in sink.events if isinstance(e, ToolCompleted))
        self.assertFalse(completed.ok)

    def test_synthetic_user_texts_are_skipped(self) -> None:
        """harness 注入的伪 user 消息（收尾指令等）不回放成用户发言。"""
        messages = [
            {"role": "user", "content": "[系统提示] 请收尾"},
            {"role": "assistant", "content": "收尾了"},
        ]
        sink = RecordingSink()
        replay_transcript(messages, sink, skip_user_texts=frozenset({"[系统提示] 请收尾"}))
        notices = [e.text for e in sink.events if isinstance(e, Notice)]
        self.assertFalse(any("系统提示" in text for text in notices))
        self.assertEqual([e.text for e in sink.events if isinstance(e, TextDelta)], ["收尾了"])

    def test_long_texts_are_capped(self) -> None:
        long_answer = "\n".join(f"第 {i} 行" for i in range(_REPLAY_TEXT_LINES + 10))
        long_question = "\n".join(f"问 {i}" for i in range(_REPLAY_USER_LINES + 2))
        messages = [
            {"role": "user", "content": long_question},
            {"role": "assistant", "content": long_answer},
        ]
        sink = RecordingSink()
        replay_transcript(messages, sink)
        delta = next(e for e in sink.events if isinstance(e, TextDelta))
        self.assertEqual(len(delta.text.splitlines()), _REPLAY_TEXT_LINES)
        notices = [e.text for e in sink.events if isinstance(e, Notice)]
        self.assertTrue(any("…还有 10 行" in text for text in notices), notices)
        user_echo = next(text for text in notices if text.startswith("› "))
        self.assertIn("…还有 2 行", user_echo)

    def test_orphan_call_without_result_is_not_replayed(self) -> None:
        """执行中断掉的会话：tool_calls 没等到结果，不补发 pending 悬空。"""
        messages = [
            {"role": "user", "content": "跑一下"},
            {"role": "assistant", "content": None,
             "tool_calls": [assistant_call("c1", "bash", '{"command": "sleep 99"}')]},
        ]
        sink = RecordingSink()
        replay_transcript(messages, sink)
        self.assertFalse(any(k.startswith("tool.") for k in sink.kinds()))

    def test_bad_arguments_json_degrades_to_empty_args(self) -> None:
        messages = [
            {"role": "assistant", "content": None,
             "tool_calls": [assistant_call("c1", "bash", "{截断的半行")]},
            {"role": "tool", "tool_call_id": "c1", "content": "ok"},
        ]
        sink = RecordingSink()
        replay_transcript(messages, sink)
        pending = next(e for e in sink.events if isinstance(e, ToolPending))
        self.assertEqual(pending.args, {})

    def test_unknown_tool_call_id_falls_back_to_generic_name(self) -> None:
        messages = [{"role": "tool", "tool_call_id": "没见过", "content": "输出"}]
        sink = RecordingSink()
        replay_transcript(messages, sink)
        pending = next(e for e in sink.events if isinstance(e, ToolPending))
        self.assertEqual(pending.name, "tool")

    def test_empty_or_all_skipped_returns_zero_without_header(self) -> None:
        sink = RecordingSink()
        self.assertEqual(replay_transcript([], sink, header="── 回放 ──"), 0)
        self.assertEqual(
            replay_transcript(
                [{"role": "user", "content": "注入"}],
                sink,
                skip_user_texts=frozenset({"注入"}),
                header="── 回放 ──",
            ),
            0,
        )
        self.assertEqual(sink.events, [], "没有内容就不该打 header 和收尾空行")

    def test_turns_are_separated_by_blank_notice(self) -> None:
        messages = [
            {"role": "user", "content": "第一轮"},
            {"role": "assistant", "content": "答一"},
            {"role": "user", "content": "第二轮"},
            {"role": "assistant", "content": "答二"},
        ]
        sink = RecordingSink()
        replay_transcript(messages, sink)
        notices = [e.text for e in sink.events if isinstance(e, Notice)]
        self.assertIn("", notices[:-1], "两轮之间应有空行分隔（收尾空行之外）")

    def test_plain_sink_renders_replay(self) -> None:
        messages = [
            {"role": "user", "content": "看一下"},
            {"role": "assistant", "content": None,
             "tool_calls": [assistant_call("c1", "grep", '{"pattern": "add"}')]},
            {"role": "tool", "tool_call_id": "c1", "content": "calc.py:1:def add"},
            {"role": "assistant", "content": "找到了"},
        ]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            replay_transcript(messages, PlainSink(), header="── 回放 ──")
        out = buffer.getvalue()
        self.assertIn("── 回放 ──", out)
        self.assertIn("› 看一下", out)
        self.assertIn("⚙ grep", out)
        self.assertIn("找到了", out)

    def test_rich_sink_folds_readonly_tools(self) -> None:
        """RichSink 消费回放：连续只读调用照常进折叠组，留底可供 Ctrl-O。"""
        try:
            from rich.console import Console

            from xiaoyu.tui import RichSink
        except ImportError:
            self.skipTest("未安装 TUI 可选依赖")
        messages = [
            {"role": "user", "content": "读两个文件"},
            {"role": "assistant", "content": None, "tool_calls": [
                assistant_call("c1", "read_file", '{"path": "a.py"}'),
                assistant_call("c2", "read_file", '{"path": "b.py"}'),
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "内容甲"},
            {"role": "tool", "tool_call_id": "c2", "content": "内容乙"},
            {"role": "assistant", "content": "都读完了"},
        ]
        buffer = io.StringIO()
        sink = RichSink(Console(file=buffer, soft_wrap=True, highlight=False))
        with contextlib.redirect_stdout(io.StringIO()) as raw:
            replay_transcript(messages, sink)
        out = buffer.getvalue()
        self.assertIn("读文件 ×2", out, "连续只读调用应汇成一行折叠组")
        self.assertIn("都读完了", raw.getvalue(), "正文走裸 print 直出")
        self.assertTrue(sink.folded, "折叠留底应保留，供进 REPL 后 Ctrl-O 展开")


if __name__ == "__main__":
    unittest.main()
