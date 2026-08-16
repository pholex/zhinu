"""启动横幅的测试。"""

from __future__ import annotations

import unittest

import xiaoyu
from xiaoyu.banner import (
    _ART,
    _BOX_MIN,
    _FEATHERS,
    _FULL_WIDTH,
    _MIN_WIDTH,
    _vis,
    build_banner,
)


class BoxBannerTest(unittest.TestCase):
    """宽终端：圆角框分栏。"""

    def test_box_has_title_meta_and_panels(self):
        out = build_banner("deepseek-v4-pro", "/tmp/ws", width=100)
        self.assertIn("╭─", out)
        self.assertIn("╰", out)
        self.assertIn(f"小羽 · Xiaoyu v{xiaoyu.__version__}", out)
        self.assertIn("deepseek-v4-pro", out)
        self.assertIn("/tmp/ws", out)
        self.assertIn("上手提示", out)
        self.assertIn("/help", out)
        self.assertIn("新特性", out)

    def test_box_lines_align(self):
        #  非 tty 下 ui 不加 ANSI，可见宽度必须整齐一致（CJK 按 2 列算）
        out = build_banner("m", "/tmp/ws", width=100)
        widths = {_vis(line) for line in out.strip("\n").splitlines()}
        self.assertEqual(widths, {99})  # width - 1，留一列防终端提前换行

    def test_box_normal_width_uses_feather_mascot(self):
        out = build_banner("m", "/tmp/ws", width=90)
        self.assertNotIn("██", out)
        self.assertIn(_FEATHERS[1], out)

    def test_box_very_wide_uses_block_art(self):
        out = build_banner("m", "/tmp/ws", width=140)
        self.assertIn(_ART[0], out)

    def test_box_starts_at_min_width(self):
        out = build_banner("m", "/tmp/ws", width=_BOX_MIN)
        self.assertIn("╭─", out)
        widths = {_vis(line) for line in out.strip("\n").splitlines()}
        self.assertEqual(widths, {_BOX_MIN - 1})


class LegacyTiersTest(unittest.TestCase):
    """中窄终端：逐级降级到块字、单行。"""

    def test_medium_terminal_block_art_with_feathers(self):
        out = build_banner("deepseek-v4-pro", "/tmp/ws", width=_BOX_MIN - 2)
        self.assertNotIn("╭", out)
        self.assertIn(_ART[0], out)
        self.assertIn(_FEATHERS[0], out)
        self.assertIn(xiaoyu.__version__, out)
        self.assertIn("deepseek-v4-pro", out)
        self.assertIn("/tmp/ws", out)
        self.assertIn("/help", out)

    def test_medium_terminal_drops_feathers_keeps_art(self):
        out = build_banner("m", "/ws", width=_FULL_WIDTH)  # 差 2 列余量，舍羽毛
        self.assertIn(_ART[0], out)
        for feather in _FEATHERS:
            self.assertNotIn(feather, out)

    def test_narrow_terminal_degrades_to_one_line(self):
        out = build_banner("m", "/ws", width=_MIN_WIDTH - 1)
        self.assertNotIn("██", out)
        self.assertIn("小羽", out)
        self.assertIn(xiaoyu.__version__, out)
        self.assertEqual(len(out.splitlines()), 1)

    def test_full_banner_lines_fit_declared_min_width(self):
        #  非 tty 下 ui 不加 ANSI，行宽即可见宽；块字必须放得进最小宽度
        out = build_banner("m", "/ws", width=_MIN_WIDTH)
        for line in _ART:
            self.assertLessEqual(len(line), _MIN_WIDTH)
        self.assertIn(_ART[0], out)

    def test_letters_are_spaced(self):
        #  防回归到挤在一起的版本：X 和 I 之间必须有间隔
        self.assertIn("██╗  ██╗  ██╗", _ART[0])


if __name__ == "__main__":
    unittest.main()
