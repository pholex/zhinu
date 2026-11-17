"""完整字符串里的转义序列连参数一起摘，不留 "[31m" 残渣；流式分片照旧逐字符删。"""

from __future__ import annotations

import contextlib
import io
import unittest

from xiaoyu import ui
from xiaoyu.events import Notice, TextDelta, ToolCompleted, ToolPending
from xiaoyu.render import PlainSink, sanitize_event


class StripSequencesTest(unittest.TestCase):
    def test_coloured_output_leaves_no_residue(self) -> None:
        coloured = "\x1b[31mFAILED\x1b[0m tests/test_x.py \x1b[1;32m3 passed\x1b[m"
        self.assertEqual(ui.strip_sequences(coloured), "FAILED tests/test_x.py 3 passed")

    def test_osc_and_eight_bit_forms_go_whole(self) -> None:
        self.assertEqual(ui.strip_sequences("a\x1b]0;窗口标题\x07b"), "ab")
        self.assertEqual(ui.strip_sequences("a\x1b]52;c;ZXZpbA==\x1b\\b"), "ab")
        self.assertEqual(ui.strip_sequences("a\x9b31mb\x1b[2Jc"), "abc")
        self.assertEqual(ui.strip_sequences("a\x1bMb"), "ab")

    def test_unterminated_sequences_still_lose_their_control_bytes(self) -> None:
        cleaned = ui.strip_sequences("a\x1b]0;没有结束符")
        self.assertNotIn("\x1b", cleaned)
        self.assertTrue(cleaned.startswith("a"))

    def test_plain_text_tabs_and_newlines_are_untouched(self) -> None:
        text = "第一行\n\t[31m 这只是普通的方括号文本\n"
        self.assertEqual(ui.strip_sequences(text), text)

    def test_preview_has_no_residue(self) -> None:
        self.assertEqual(ui.preview("\x1b[31m红色\x1b[0m"), "红色")


class SanitizeEventTest(unittest.TestCase):
    def test_whole_fields_are_stripped_by_sequence(self) -> None:
        done = sanitize_event(
            ToolCompleted("bash", output="\x1b[31mFAILED\x1b[0m", ok=False, seconds=0.1)
        )
        self.assertEqual(done.output, "FAILED")
        pending = sanitize_event(ToolPending("bash", {"command": "echo \x1b[1mhi\x1b[0m"}))
        self.assertEqual(pending.args["command"], "echo hi")
        self.assertEqual(sanitize_event(Notice("注意\x1b[31m")).text, "注意")

    def test_streamed_text_is_still_cleaned_byte_by_byte(self) -> None:
        #  一个序列被切在两片之间：按序列匹配会漏掉后半片，所以这条路只删控制字符
        first = sanitize_event(TextDelta("看\x1b["))
        second = sanitize_event(TextDelta("31m这里"))
        self.assertEqual(first.text, "看[")
        self.assertEqual(second.text, "31m这里")

    def test_clean_events_are_returned_as_is(self) -> None:
        event = ToolCompleted("bash", output="ok", ok=True, seconds=0.1)
        self.assertIs(sanitize_event(event), event)

    def test_plain_sink_prints_coloured_tool_output_clean(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            PlainSink().emit(
                ToolCompleted("bash", output="\x1b[31mFAILED\x1b[0m", ok=False, seconds=0.1)
            )
        self.assertNotIn("[31m", buffer.getvalue())
        self.assertNotIn("\x1b", buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
