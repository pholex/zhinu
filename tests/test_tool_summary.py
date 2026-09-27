"""工具行摘要（ui.tool_summary）：终端的工具行与 ACP 的标题共用的那一份规则。"""

from __future__ import annotations

import unittest

from xiaoyu import acp, render, ui


class ToolSummaryTest(unittest.TestCase):
    def test_search_shows_what_is_searched_not_just_where(self) -> None:
        args = {"pattern": "def main", "path": "src", "glob": "*.py"}
        self.assertEqual(ui.tool_summary("grep", args), "def main in src *.py")
        self.assertEqual(ui.tool_summary("grep", {"pattern": "TODO"}), "TODO")
        self.assertEqual(
            ui.tool_summary("list_files", {"pattern": "**/*.md", "path": "docs"}), "**/*.md in docs"
        )

    def test_file_tools_show_the_path_and_never_the_content(self) -> None:
        summary = ui.tool_summary("write_file", {"path": "a/b.py", "content": "SECRET" * 100})
        self.assertEqual(summary, "a/b.py")

    def test_credential_like_keys_never_reach_the_title(self) -> None:
        args = {
            "owner": "o",
            "token": "ghp_SECRET1",
            "api_key": "SECRET2",
            "apiKey": "SECRET3",
            "Authorization": "Bearer SECRET4",
            "client_secret": "SECRET5",
            "password": "SECRET6",
            "cookie": "SECRET7",
        }
        for summary in (
            ui.tool_summary("mcp__github__get", args),
            ui.tool_summary("use_tool", {"tool_name": "mcp__github__get", "tool_input": args}),
            render.args_preview("mcp__github__get", args, width=200),
            acp._tool_title("mcp__github__get", args),
        ):
            self.assertNotIn("SECRET", summary)
            self.assertIn("owner=o", summary)

    def test_generic_values_are_capped_and_nested_shapes_only(self) -> None:
        summary = ui.tool_summary(
            "mcp__x__y", {"body": "y" * 500, "labels": ["a", "b"], "opts": {"k": 1}, "empty": ""}
        )
        self.assertIn("labels=[2 项]", summary)
        self.assertIn("opts={…}", summary)
        self.assertNotIn("empty", summary)
        self.assertLess(len(summary), 120)

    def test_forwarded_call_is_titled_by_the_real_tool(self) -> None:
        args = {"tool_name": "mcp__github__create_issue", "tool_input": {"repo": "o/r"}}
        self.assertEqual(ui.tool_summary("use_tool", args), "mcp__github__create_issue repo=o/r")
        self.assertEqual(acp._tool_title("use_tool", args), "mcp__github__create_issue: repo=o/r")
        self.assertEqual(acp._tool_title("use_tool", {}), "use_tool")

    def test_terminal_and_acp_agree(self) -> None:
        cases = [
            ("bash", {"command": "git status --short"}),
            ("grep", {"pattern": "x", "path": "src"}),
            ("read_file", {"path": "a.py", "offset": 3}),
            ("search_tool", {"query": "create issue"}),
        ]
        for name, args in cases:
            with self.subTest(name=name):
                shown = render.args_preview(name, args, width=200)
                self.assertEqual(acp._tool_title(name, args), f"{name}: {shown}")

    def test_multiline_command_keeps_its_line_marks_on_the_terminal(self) -> None:
        shown = render.args_preview("bash", {"command": "set -e\nmake"}, width=200)
        self.assertEqual(shown, "set -e⏎make")
        self.assertEqual(acp._tool_title("bash", {"command": "set -e\nmake"}), "bash: set -e make")

    def test_non_dict_arguments_do_not_crash(self) -> None:
        self.assertEqual(ui.tool_summary("x", None), "None")
        self.assertEqual(ui.tool_summary("x", "raw"), "raw")


if __name__ == "__main__":
    unittest.main()
