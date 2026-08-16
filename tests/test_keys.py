"""按键绑定表的测试。

要点仍是**断言能 fail**：这张表的全部价值就是"表、实现、帮助文案三者不漂移"，
所以测试必须真的能在漂移时炸，而不是把三者各测一遍各自自洽。
"""

from __future__ import annotations

import unittest

from xiaoyu import keys

try:
    import prompt_toolkit  # noqa: F401
    import rich  # noqa: F401

    HAS_TUI = True
except ImportError:
    HAS_TUI = False


class TestBindingTable(unittest.TestCase):
    def test_commands_are_unique(self) -> None:
        commands = [item.command for item in keys.BINDINGS]
        self.assertEqual(len(commands), len(set(commands)))

    def test_every_binding_has_label_and_description(self) -> None:
        for item in keys.BINDINGS:
            with self.subTest(command=item.command):
                self.assertTrue(item.label)
                self.assertTrue(item.desc)
                self.assertIn(item.category, keys.CATEGORIES)

    def test_no_duplicate_key_within_a_category(self) -> None:
        """同一上下文里一个键只能有一个含义，重复绑定后写的会静默盖掉先写的。"""
        for category in keys.CATEGORIES:
            seen: dict[tuple[str, ...], str] = {}
            for item in keys.BINDINGS:
                if item.category != category:
                    continue
                for combo in item.keys:
                    self.assertNotIn(
                        combo, seen, f"{category} 里 {combo} 同时绑给了 {seen.get(combo)} 和 {item.command}"
                    )
                    seen[combo] = item.command

    def test_paste_image_reachable_on_windows(self) -> None:
        """Windows 终端宿主默认把 Ctrl-V 截给自家文本粘贴，c-v 到不了程序；
        Alt-V（escape+v）是那边唯一按得到的组合，删掉它 = Windows 贴图整体失灵。"""
        combos = keys.BY_COMMAND["input.pasteImage"].keys
        self.assertIn(("c-v",), combos)
        self.assertIn(("escape", "v"), combos)

    def test_hint_line_covers_the_prefixes(self) -> None:
        """首屏速览必须带上四个前缀里用户最需要知道的三个——
        它们没有任何其它发现路径（不像按键还能瞎按到）。"""
        line = keys.hint_line()
        for symbol in ("!", "@", "#"):
            self.assertIn(symbol, line)
        self.assertIn("Ctrl-O", line)
        self.assertIn("Alt-Enter", line)  # 多行输入是最高频的"不说不会知道"

    def test_tips_cover_the_undiscoverable_keys(self) -> None:
        """轮播是这些键唯一的发现路径：不在速览行（放不下），菜单提示行也
        不教（Ctrl-X Ctrl-E / Ctrl-R 不是菜单键，1…9 被 menu_hint 省略）。
        从轮播里掉出去 = 该功能对用户不存在。"""
        rotation = "\n".join(keys.tips())
        for label in ("Alt-Enter", "Ctrl-X Ctrl-E", "Ctrl-R", "1…9"):
            self.assertIn(label, rotation)

    def test_tips_skip_what_the_menu_already_teaches(self) -> None:
        """menu_hint 在用户正对着菜单时就教了这些键，轮播再播就是噪音；
        菜单键里只有 menu.digit（menu_hint 放不下）有资格进轮播。"""
        for item in keys.BINDINGS:
            if item.category == keys.MENU and item.command != "menu.digit":
                with self.subTest(command=item.command):
                    self.assertFalse(item.tip)

    def test_help_text_lists_every_shown_binding(self) -> None:
        """/keys 不能漏：漏掉的那个键等于不存在。"""
        text = keys.help_text()
        for item in keys.BINDINGS:
            if item.show:
                with self.subTest(command=item.command):
                    self.assertIn(item.desc, text)

    def test_help_text_documents_keys_it_does_not_register(self) -> None:
        """由 prompt_toolkit / REPL 循环实现的键也要出现在帮助里，
        否则帮助只覆盖一半用户真正能按的键。"""
        text = keys.help_text()
        self.assertIn("Ctrl-R", text)  # prompt_toolkit 自带的反向搜索
        self.assertIn("Ctrl-C", text)  # REPL 循环里的两连退出
        self.assertEqual(keys.BY_COMMAND["history.search"].keys, ())


@unittest.skipUnless(HAS_TUI, "未安装 tui 可选依赖")
class TestRegistrationMatchesTable(unittest.TestCase):
    def test_input_bindings_come_from_the_table(self) -> None:
        import io
        import tempfile
        from pathlib import Path

        from rich.console import Console

        from xiaoyu.permissions import Permissions
        from xiaoyu.tui import Tui

        from prompt_toolkit.key_binding import KeyBindings

        tui = Tui(Permissions(Path(tempfile.mkdtemp())), console=Console(file=io.StringIO()))
        actual = {tuple(str(key) for key in b.keys) for b in tui._key_bindings().bindings}

        #  按表另建一份参照（prompt_toolkit 会把 "enter" 归一成 Keys.ControlM，
        #  所以只能过一遍同样的解析再比，不能拿字面量比）
        reference = KeyBindings()
        for item in keys.BINDINGS:
            if item.category != keys.INPUT:
                continue
            for combo in item.keys:
                reference.add(*combo)(lambda event: None)
        expected = {tuple(str(key) for key in b.keys) for b in reference.bindings}

        self.assertEqual(actual, expected)

    def test_missing_handler_raises(self) -> None:
        """表里加了键却忘了实现——帮助会承诺一个按不动的键，必须当场炸。"""
        from xiaoyu.tui import _register

        with self.assertRaises(RuntimeError) as caught:
            _register({"input.submit": lambda event: None}, keys.INPUT)
        self.assertIn("input.recall", str(caught.exception))

    def test_handler_not_in_table_raises(self) -> None:
        """实现了却没进表——用户永远发现不了它，同样当场炸。"""
        from xiaoyu.tui import _register

        handlers = {command: (lambda event: None) for command in keys.registered_in(keys.INPUT)}
        handlers["app.secretFeature"] = lambda event: None
        with self.assertRaises(RuntimeError) as caught:
            _register(handlers, keys.INPUT)
        self.assertIn("app.secretFeature", str(caught.exception))

    def test_menu_hint_matches_what_the_menu_actually_binds(self) -> None:
        """菜单底部提示的每个按法，都得真有对应绑定。"""
        hint = keys.menu_hint()
        for item in keys.BINDINGS:
            if item.category == keys.MENU and item.hint and item.keys:
                with self.subTest(command=item.command):
                    self.assertIn(item.label, hint)


if __name__ == "__main__":
    unittest.main()
