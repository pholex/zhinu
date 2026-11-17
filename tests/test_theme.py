"""语义色 token 的测试。

重点是**能 fail 的断言**：rich 遇到未知样式名是静默忽略的（不抛异常），
所以"渲染一遍没报错"根本证明不了 token 名没拼错。这里改用两条硬检查：
① AST 扫源码里的 `style=` 字面量，逐个核对确有定义；
② 深浅两张表键集必须完全相同——少一个键，浅色终端上那一处就悄悄没了颜色。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from xiaoyu import theme, ui


class TestThemeTables(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(theme.set_mode, theme.mode())

    def test_light_and_dark_cover_the_same_tokens(self) -> None:
        """浅色表漏一个 token，那一处在白底终端上就会退回无色——
        而且只有真去用浅色终端的人才会发现。"""
        theme.set_mode("dark")
        dark = set(theme.tokens())
        for token in dark:
            theme.set_mode("light")
            self.assertIsNotNone(theme.style(token), f"浅色表缺 {token}")

    def test_every_token_renders_in_all_three_formats(self) -> None:
        """一个 token 要同时供 ANSI（明文 REPL）、rich（TUI）、
        prompt_toolkit（确认菜单）三个消费端，任一渲染不出来都是漏配。"""
        for mode in ("dark", "light"):
            theme.set_mode(mode)
            for token in theme.tokens():
                item = theme.style(token)
                with self.subTest(mode=mode, token=token):
                    item.ansi()  # 允许空串（text.primary 本来就不着色）
                    self.assertTrue(item.rich())
                    #  深色表的 text.primary 三项皆空是有意的，其余必须有内容
                    if token != "text.primary":
                        self.assertTrue(item.ptk(), f"{token} 没有 ptk 样式")

    def test_unknown_token_raises(self) -> None:
        """拼错的 token 当场炸，而不是静默变成无色。"""
        with self.assertRaises(KeyError):
            theme.style("status.nope")

    def test_to_hex_matches_xterm_palette(self) -> None:
        self.assertEqual(theme.to_hex(196), "#ff0000")  # 色立方纯红
        self.assertEqual(theme.to_hex(21), "#0000ff")  # 色立方纯蓝
        self.assertEqual(theme.to_hex(232), "#080808")  # 灰阶最暗
        self.assertEqual(theme.to_hex(255), "#eeeeee")  # 灰阶最亮
        #  低 16 色随终端调色板而变，没有固定 RGB，不该被当成绝对色用
        with self.assertRaises(ValueError):
            theme.to_hex(3)

    def test_ptk_class_names_have_no_dots(self) -> None:
        """prompt_toolkit 的 class 名不能带点，token 里的点要换成横杠。"""
        for name in theme.ptk_style():
            self.assertNotIn(".", name)


class TestStyleNamesAreDefined(unittest.TestCase):
    """源码里出现的样式名必须都在 token 表里。

    rich 对未知样式静默忽略，这条检查是唯一能挡住拼写错误的地方。
    """

    def _style_literals(self, module) -> list[tuple[int, str]]:
        """捞出源码里当样式名用的字符串字面量。

        两种形态：rich 的 `style="..."` 关键字参数（tui.py），以及
        `ui.styled("...", text)` / `theme.ptk("...")` 的首参（render.py、
        确认菜单）。
        """
        source = pathlib.Path(module.__file__)
        tree = ast.parse(source.read_text(encoding="utf-8"))
        found: list[tuple[int, str]] = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.keyword)
                and node.arg == "style"
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                found.append((node.lineno, node.value.value))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("styled", "ptk")
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                found.append((node.lineno, node.args[0].value))
        return found

    def test_render_and_tui_only_use_defined_tokens(self) -> None:
        modules = []
        from xiaoyu import render

        modules.append(render)
        try:
            from xiaoyu import tui

            modules.append(tui)
        except ImportError:  # 未装 tui 可选依赖
            pass

        known = set(theme.tokens())
        total = 0
        for module in modules:
            for lineno, name in self._style_literals(module):
                total += 1
                with self.subTest(module=module.__name__, line=lineno):
                    self.assertIn(name, known, f"{module.__name__}:{lineno} 用了未定义的 {name}")
        #  断言本身要能失效：扫不到任何字面量就说明扫描逻辑坏了，测试在空转。
        #  （render.py 的样式名全部来自查表，由 test_style_lookup_tables_are_defined 覆盖）
        self.assertGreater(total, 0, "一个样式字面量都没扫到，检查逻辑已失效")

    def test_style_lookup_tables_are_defined(self) -> None:
        """按状态查样式的小表（计划状态、提示级别）也要在 token 词汇表里。"""
        from xiaoyu.render import PlainSink

        known = set(theme.tokens())
        self.assertLessEqual(set(PlainSink._NOTICE_TOKENS.values()), known)  # noqa: SLF001
        try:
            from xiaoyu.tui import _NOTICE_STYLES, _PLAN_STYLES
        except ImportError:
            self.skipTest("未安装 tui 可选依赖")
        self.assertLessEqual(set(_NOTICE_STYLES.values()), known)
        self.assertLessEqual({style for _mark, style in _PLAN_STYLES.values()}, known)

    def test_diff_fragment_styles_are_defined(self) -> None:
        """diff 预览的样式名藏在返回的元组里，AST 扫不到，单独跑一遍核对。"""
        try:
            from xiaoyu.tui import _diff_rows
        except ImportError:
            self.skipTest("未安装 tui 可选依赖")
        known = set(theme.tokens())
        rows = _diff_rows("a\nfoo = 1\nlong line here", "a\nfoo = 2\nquite different", 80)
        names = {style for row in rows for style, _text in row}
        self.assertTrue(names)
        self.assertLessEqual(names, known)


class TestWidthBudget(unittest.TestCase):
    def test_preview_counts_the_ellipsis(self) -> None:
        """省略号要算进预算：limit 现在就是终端宽度，多吐一个字符就折行。"""
        self.assertEqual(len(ui.preview("x" * 100, 20)), 20)
        self.assertEqual(ui.preview("abc", 20), "abc")  # 不超长就原样

    def test_budget_subtracts_prefix_and_has_a_floor(self) -> None:
        self.assertEqual(ui.budget(10, width=100), 90)
        #  终端窄到离谱时不再让步：再减就没有可读信息量了
        self.assertGreaterEqual(ui.budget(60, width=50), 40)

    def test_fit_uses_the_budget(self) -> None:
        self.assertEqual(len(ui.fit("y" * 200, 10, width=100)), 90)


if __name__ == "__main__":
    unittest.main()
