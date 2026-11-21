"""运行期输入行的按键解析（xiaoyu/midturn.py）。纯逻辑，不碰终端。"""

from __future__ import annotations

import unittest

from xiaoyu.midturn import PASTE_END, PASTE_START, Draft


class TypingTest(unittest.TestCase):
    def test_enter_submits_only_after_input_settles(self):
        draft = Draft()
        self.assertEqual(draft.feed("改用 unittest\r".encode()), [])
        self.assertTrue(draft.unsettled)
        self.assertEqual(draft.settle(), [("submit", "改用 unittest")])
        self.assertEqual(draft.text, "")
        self.assertFalse(draft.unsettled)

    def test_blank_line_submits_nothing(self):
        draft = Draft()
        draft.feed(b"  \r")
        self.assertEqual(draft.settle(), [])
        self.assertEqual(draft.text, "")

    def test_multibyte_character_split_across_reads(self):
        draft = Draft()
        raw = "织".encode()
        draft.feed(raw[:1])
        draft.feed(raw[1:])
        self.assertEqual(draft.text, "织")

    def test_backspace_kill_line_and_kill_word(self):
        draft = Draft()
        draft.feed("先跑测试 再改文档x\x7f".encode())
        self.assertEqual(draft.text, "先跑测试 再改文档")
        draft.feed(b"\x17")
        self.assertEqual(draft.text, "先跑测试 ")
        draft.feed(b"\x15")
        self.assertEqual(draft.text, "")
        draft.feed(b"\x7f")  # 空草稿上退格不出错
        self.assertEqual(draft.text, "")

    def test_other_control_characters_are_swallowed(self):
        draft = Draft()
        draft.feed(b"a\x01\x0f\x04b")
        self.assertEqual(draft.text, "ab")


class EscapeTest(unittest.TestCase):
    def test_lone_escape_clears_the_draft(self):
        draft = Draft()
        draft.feed(b"abc\x1b")
        self.assertEqual(draft.text, "abc", "ESC 可能是序列开头，静下来之前不能动")
        self.assertEqual(draft.settle(), [])
        self.assertEqual(draft.text, "")

    def test_escape_on_empty_draft_asks_to_retract(self):
        draft = Draft()
        draft.feed(b"\x1b")
        self.assertEqual(draft.settle(), [("retract", "")])

    def test_arrow_keys_leave_no_residue(self):
        draft = Draft()
        self.assertEqual(draft.feed(b"a\x1b[Ab\x1b[1;5Cc\x1bOPd"), [])
        self.assertEqual(draft.text, "abcd")

    def test_sequence_split_across_reads(self):
        draft = Draft()
        draft.feed(b"a\x1b[")
        draft.feed(b"1;5")
        draft.feed(b"Cb")
        self.assertEqual(draft.text, "ab")

    def test_unfinished_sequence_is_dropped_when_input_settles(self):
        draft = Draft()
        draft.feed(b"a\x1b[1;")
        self.assertEqual(draft.settle(), [])
        draft.feed(b"b")
        self.assertEqual(draft.text, "ab")

    def test_alt_enter_inserts_a_newline(self):
        draft = Draft()
        draft.feed("第一行\x1b\r第二行\r".encode())
        self.assertEqual(draft.settle(), [("submit", "第一行\n第二行")])


class PasteTest(unittest.TestCase):
    def test_bracketed_paste_keeps_newlines_and_does_not_submit(self):
        draft = Draft()
        data = f"{PASTE_START}第一行\r\n第二行\r{PASTE_END}".encode()
        self.assertEqual(draft.feed(data), [])
        self.assertFalse(draft.unsettled)
        self.assertEqual(draft.text, "第一行\n第二行\n")
        draft.feed(b"\r")
        self.assertEqual(draft.settle(), [("submit", "第一行\n第二行")])

    def test_paste_end_marker_split_across_reads(self):
        draft = Draft()
        data = f"{PASTE_START}abc{PASTE_END}d".encode()
        for cut in range(1, len(data)):
            with self.subTest(cut=cut):
                draft = Draft()
                draft.feed(data[:cut])
                if cut >= len(PASTE_START):
                    draft.settle()  # 粘贴进行中静下来，什么都不该判定
                draft.feed(data[cut:])
                self.assertEqual(draft.text, "abcd")

    def test_escape_sequences_inside_a_paste_are_stripped_of_control_bytes(self):
        draft = Draft()
        draft.feed(f"{PASTE_START}a\x1b[31mb{PASTE_END}".encode())
        self.assertEqual(draft.text, "a[31mb", "粘贴内容不解释成按键，只滤掉控制字符")

    def test_unbracketed_multiline_burst_becomes_one_submission(self):
        """终端没透传 bracketed paste：多行连着到达，只有最后那个回车算提交。"""
        draft = Draft()
        actions = draft.feed("第一行\r".encode())
        actions += draft.feed("\r".encode())
        actions += draft.feed("第三行\r".encode())
        self.assertEqual(actions, [])
        self.assertEqual(draft.settle(), [("submit", "第一行\n\n第三行")])

    def test_take_hands_over_the_unsent_draft(self):
        draft = Draft()
        draft.feed("还没发".encode())
        self.assertEqual(draft.take(), "还没发")
        self.assertEqual(draft.text, "")


if __name__ == "__main__":
    unittest.main()
