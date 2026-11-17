"""流式预览在执行前可见，且中断、嵌套字段和大正文不改变执行语义。"""

import contextlib
import io
import json
from unittest import TestCase, mock

from xiaoyu.events import RequestEnded, RequestStarted, ToolPreparing
from xiaoyu.tool_preparation import Preparation
from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk
from .test_render import RecordingSink


class PreviewTest(TestCase):
    def test_split_unicode_and_nested_fields(self):
        args = json.dumps({"nested": {"path": "wrong"}, "path": "目录/😀.py",
                           "__tool_use_purpose": '写入 "示例"', "content": "x" * 10000})
        preview = Preparation(2)
        for char in args:
            preview.feed(char)
        event = preview.event("write_file", "c")
        self.assertEqual(event.path, "目录/😀.py")
        self.assertEqual(event.purpose, '写入 "示例"')
        self.assertEqual(event.argument_chars, len(args))
        self.assertEqual(event.index, 2)
        self.assertIsNone(preview._buffer)
        self.assertNotIn("content", preview.fields)

    def test_large_field_is_skipped_but_later_path_is_found(self):
        preview = Preparation(0)
        preview.feed(json.dumps({"__tool_use_purpose": "x" * 10000, "path": "a.py"}))
        event = preview.event("write_file", "c")
        self.assertEqual(event.purpose, "")
        self.assertEqual(event.path, "a.py")

    def test_throttling_keeps_final_count(self):
        preview = Preparation(0)
        with mock.patch("xiaoyu.tool_preparation.time.monotonic", return_value=1):
            preview.feed('{"content":"')
            self.assertIsNotNone(preview.event("write_file", "c"))
            for _ in range(100):
                preview.feed("x")
                self.assertIsNone(preview.event("write_file", "c"))
            final = preview.event("write_file", "c", force=True)
            self.assertEqual(final.argument_chars, 112)
            self.assertIsNone(preview.event("write_file", "c", force=True))


class StreamPreviewTest(AgentTestCase):
    def test_preview_precedes_execution_and_interleaved_calls_stay_separate(self):
        recorder = RecordingSink()
        def stream():
            yield chunk(tool_calls=[call_fragment(0, "a", "read_file", '{"path":"calc.py"')])
            self.assertEqual(recorder.events[-1].path, "calc.py")
            self.assertFalse(any(e.kind == "tool.pending" for e in recorder.events))
            yield chunk(tool_calls=[call_fragment(1, "b", "list_files", '{"pattern":"*.py"}')])
            yield chunk(tool_calls=[call_fragment(0, None, None, '}')])
            yield usage_chunk(10, 10)
        agent = self.build([stream(), [chunk(content="完成"), usage_chunk(20, 2)]], sink=recorder)
        agent.send("查看文件")
        previews = [e for e in recorder.events if isinstance(e, ToolPreparing)]
        self.assertEqual({e.tool_call_id for e in previews}, {"a", "b"})
        self.assertTrue(all(e.path == "calc.py" for e in previews if e.index == 0))
        self.assertTrue(all(not e.path for e in previews if e.index == 1))
        ended = recorder.kinds().index("request.ended")
        self.assertFalse(any(isinstance(e, ToolPreparing) for e in recorder.events[ended:]))
        self.assertEqual(len(agent.trace), 2)

    def test_interruption_closes_preview_without_executing(self):
        recorder = RecordingSink()
        def stream():
            yield chunk(tool_calls=[call_fragment(0, "c", "write_file", '{"path":"new.txt","content":"')])
            raise KeyboardInterrupt
        agent = self.build([stream()], sink=recorder)
        with self.assertRaises(KeyboardInterrupt):
            agent.send("新建文件")
        self.assertIsInstance(recorder.events[-1], RequestEnded)
        self.assertTrue(any(isinstance(e, ToolPreparing) for e in recorder.events))
        self.assertFalse((self.root / "new.txt").exists())
        self.assertFalse(any(e.kind == "tool.pending" for e in recorder.events))


class PreparingRenderingTest(TestCase):
    def test_rich_updates_and_ignores_late_preview(self):
        from rich.console import Console
        from xiaoyu.tui import RichSink
        sink = RichSink(Console(file=io.StringIO(), force_terminal=True, width=100))
        self.addCleanup(sink.interrupt)
        sink.emit(RequestStarted("model"))
        sink.emit(ToolPreparing("write_file", 0, 100, "a.py", "写入\x1b[31m文件"))
        self.assertIn("a.py", sink._running_line.progress)
        self.assertNotIn("\x1b", sink._running_line.progress)
        status = sink._status
        sink.emit(ToolPreparing("write_file", 0, 4000, "a.py"))
        self.assertIs(sink._status, status)
        self.assertIn("4,000", sink._running_line.progress)
        sink.emit(RequestEnded(error="interrupted"))
        sink.emit(ToolPreparing("write_file", 0, 5000, "a.py"))
        self.assertIsNone(sink._status)

    def test_plain_pipe_is_quiet(self):
        from xiaoyu.render import PlainSink
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sink = PlainSink()
            sink.emit(RequestStarted("model"))
            sink.emit(ToolPreparing("write_file", 0, 100, "a.py"))
            sink.emit(RequestEnded())
        self.assertEqual(out.getvalue(), "")
