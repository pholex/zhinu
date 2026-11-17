"""frontmatter 里写成多行的普通值要整段读进来：description 的续行里常有触发词。"""

from __future__ import annotations

import unittest

from xiaoyu.skills import parse_frontmatter


def front(body: str) -> dict[str, str]:
    return parse_frontmatter(f"---\n{body}\n---\n正文")


class MultilineScalarTest(unittest.TestCase):
    def test_indented_continuation_lines_join_the_value(self) -> None:
        meta = front(
            "name: deploy\n"
            "description: 部署到预发环境。\n"
            "  当用户说「发一下预发」「上 staging」时使用。\n"
            "  不负责生产发布。\n"
            "license: MIT"
        )
        self.assertEqual(
            meta["description"],
            "部署到预发环境。 当用户说「发一下预发」「上 staging」时使用。 不负责生产发布。",
        )
        self.assertEqual((meta["name"], meta["license"]), ("deploy", "MIT"))

    def test_quoted_value_spanning_lines_loses_its_quotes(self) -> None:
        meta = front('description: "第一行\n  第二行"\nname: x')
        self.assertEqual(meta["description"], "第一行 第二行")
        self.assertEqual(meta["name"], "x")

    def test_children_of_an_empty_key_are_still_skipped(self) -> None:
        meta = front("name: x\nmetadata:\n  type: tool\n  owner: me\ndescription: 一句话")
        self.assertEqual(meta["metadata"], "")
        self.assertEqual(meta["description"], "一句话")
        self.assertNotIn("type", meta)

    def test_block_scalars_and_single_lines_are_unchanged(self) -> None:
        meta = front("description: >-\n  折叠的\n  两行\nname: 'quoted'\nplain: v")
        self.assertEqual(meta, {"description": "折叠的 两行", "name": "quoted", "plain": "v"})

    def test_blank_line_does_not_glue_the_next_key(self) -> None:
        meta = front("description: 一句\n\nname: x")
        self.assertEqual(meta, {"description": "一句", "name": "x"})


if __name__ == "__main__":
    unittest.main()
