"""子命令表：分发与 --help 的清单是同一张，谁也不能单独漏掉一项。"""

from __future__ import annotations

import ast
import importlib
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import cli


class SubcommandTableTest(unittest.TestCase):
    def test_every_entry_has_a_real_handler(self) -> None:
        for names, handler, usage, summary in cli.SUBCOMMANDS:
            with self.subTest(names=names):
                self.assertTrue(callable(cli.subcommand_handler(handler)), handler)
                self.assertTrue(usage.startswith(names[0]))
                self.assertTrue(summary.strip())

    def test_names_are_unique(self) -> None:
        names = [name for entry in cli.SUBCOMMANDS for name in entry[0]]
        self.assertEqual(len(names), len(set(names)))

    def test_help_lists_every_subcommand(self) -> None:
        text = cli.build_parser().format_help()
        for names, _, usage, summary in cli.SUBCOMMANDS:
            with self.subTest(names=names):
                self.assertIn(f"xiaoyu {usage}", text)
                self.assertIn(summary, text)
        #  当初漏掉的那一项
        self.assertIn("xiaoyu serve", text)

    def test_dispatch_goes_through_the_table(self) -> None:
        for names, handler, _, _ in cli.SUBCOMMANDS:
            #  住在自己模块里的子命令族：patch 要打在函数真正所在的模块上
            module_name, _, attr = handler.rpartition(".")
            owner = importlib.import_module(f"xiaoyu.{module_name}") if module_name else cli
            for name in names:
                with self.subTest(name=name), mock.patch.object(
                    owner, attr, return_value=42
                ) as fake:
                    self.assertEqual(cli.main([name, "--flag", "x"]), 42)
                    fake.assert_called_once_with(["--flag", "x"])

    def test_no_subcommand_is_dispatched_outside_the_table(self) -> None:
        """main 里不许再出现 `argv[0] == "某个名字"` 的手写分流——那正是会漏的写法。"""
        source = Path(cli.__file__).read_text(encoding="utf-8")
        main = next(
            node for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name == "main"
        )
        handwritten = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Subscript)
            and isinstance(node.left.value, ast.Name)
            and node.left.value.id == "argv"
            and any(isinstance(c, ast.Constant) and isinstance(c.value, str) for c in node.comparators)
        ]
        self.assertEqual(handwritten, [])


if __name__ == "__main__":
    unittest.main()
