"""提示词文件的注释约定：块级 HTML 注释不发给模型，边界宁可少剥不错剥。"""

from __future__ import annotations

import unittest

from xiaoyu.promptfile import parse, strip_comments


class StripCommentsTest(unittest.TestCase):
    def strip(self, text: str) -> str:
        stripped, unclosed = strip_comments(text)
        self.assertEqual(unclosed, [])
        return stripped

    def test_no_comment_is_untouched(self) -> None:
        text = "# 标题\n\n\n正文 -- 破折号\n--CORE SETTINGS--\n"
        self.assertEqual(self.strip(text), text.rstrip("\n"))

    def test_single_line_block_removed(self) -> None:
        self.assertEqual(self.strip("<!-- 给维护者 -->\n你是炉匠"), "你是炉匠")

    def test_multi_line_block_removed(self) -> None:
        text = "开头\n<!-- AUTHOR NOTES\n改这里\n  -->\n结尾"
        self.assertEqual(self.strip(text), "开头\n结尾")

    def test_indented_opening_counts_as_block(self) -> None:
        self.assertEqual(self.strip("a\n   <!-- x -->   \nb"), "a\nb")

    def test_inline_comment_is_kept(self) -> None:
        text = "正文 <!-- 行内 --> 继续"
        self.assertEqual(self.strip(text), text)

    def test_trailing_text_after_close_keeps_whole_line(self) -> None:
        text = "<!-- x --> 后面还有正文"
        stripped, unclosed = strip_comments(text)
        self.assertEqual(stripped, text)
        self.assertEqual(unclosed, [1])

    def test_inside_code_fence_is_kept(self) -> None:
        text = "示例：\n```html\n<!-- 导航 -->\n<nav></nav>\n```\n<!-- 这条剥 -->\n完"
        self.assertEqual(self.strip(text), "示例：\n```html\n<!-- 导航 -->\n<nav></nav>\n```\n完")

    def test_tilde_fence_and_longer_fence(self) -> None:
        text = "~~~\n<!-- a -->\n~~~\n````\n```\n<!-- b -->\n````"
        self.assertEqual(self.strip(text), text)

    def test_unclosed_comment_is_kept_and_reported(self) -> None:
        text = "身份\n<!-- 忘了收尾\n后半份提示词"
        stripped, unclosed = strip_comments(text)
        self.assertEqual(stripped, text)
        self.assertEqual(unclosed, [2])

    def test_blank_lines_around_removed_block_merge(self) -> None:
        self.assertEqual(self.strip("一\n\n<!-- x -->\n\n\n二"), "一\n\n二")

    def test_blank_lines_elsewhere_untouched(self) -> None:
        self.assertEqual(self.strip("一\n\n\n二\n<!-- x -->\n三"), "一\n\n\n二\n三")

    def test_adjacent_blocks(self) -> None:
        self.assertEqual(self.strip("<!-- a -->\n<!-- b\n-->\n正文"), "正文")


class ParseTest(unittest.TestCase):
    def test_leading_comment_then_text(self) -> None:
        loaded = parse("﻿".lstrip("﻿") + "<!-- 说明 -->\n\n你是炉匠\n")
        self.assertEqual(loaded.text, "你是炉匠")
        self.assertEqual(loaded.warnings("--x"), [])

    def test_only_comments_yields_empty(self) -> None:
        self.assertEqual(parse("<!-- 全是说明 -->\n").text, "")

    def test_placeholders_counted_after_stripping(self) -> None:
        loaded = parse("<!-- 把 {{NAME}} 换掉 -->\nName: {{NAME}}\nVoice: {{VOICE}}\nint f() { return 0; }")
        self.assertEqual(loaded.placeholders, 2)
        (note,) = loaded.warnings("--system-prompt-file")
        self.assertIn("2 处 {{…}}", note)

    def test_unclosed_warning_names_the_line(self) -> None:
        (note,) = parse("a\n<!-- 没收尾").warnings("--system-prompt-file")
        self.assertIn("第 2 行", note)


if __name__ == "__main__":
    unittest.main()
