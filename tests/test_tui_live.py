"""常驻输入区前端（xiaoyu/tui_live.py）里不依赖真实终端的部分。

终端里的实际表现（补打、缩放、让位）靠 tmux 里的自动验收看，不在这里：
`experiments/persistent_prompt_proto.py --selfcheck`。
"""

from __future__ import annotations

import importlib.util
import io
import tempfile
import threading
import unittest
from types import SimpleNamespace

HAVE_TUI = all(importlib.util.find_spec(name) for name in ("prompt_toolkit", "rich"))


@unittest.skipUnless(HAVE_TUI, "没装 TUI 可选依赖")
class SplitRowsTest(unittest.TestCase):
    def setUp(self) -> None:
        from xiaoyu import tui_live

        self.live = tui_live

    def test_short_text_stays_whole(self) -> None:
        self.assertEqual(self.live.split_rows("abc", 10), ([], "abc", ""))

    def test_exact_fit_does_not_split(self) -> None:
        #  正好铺满一行还不算溢出：要靠「下一个字符」才触发终端折行
        self.assertEqual(self.live.split_rows("abcde", 5), ([], "abcde", ""))

    def test_ascii_wraps_at_width(self) -> None:
        self.assertEqual(self.live.split_rows("abcdefgh", 3), (["abc", "def"], "gh", ""))

    def test_wide_char_moves_to_next_row_when_one_cell_left(self) -> None:
        #  5 格宽：「好好」占 4 格，下一个「好」放不进最后 1 格，整个挪到下一行
        rows, rest, _ = self.live.split_rows("好好好好", 5)
        self.assertEqual((rows, rest), (["好好"], "好好"))

    def test_escape_sequences_take_no_width(self) -> None:
        rows, rest, _ = self.live.split_rows("\x1b[31mabc\x1b[0mdef", 3)
        self.assertEqual((rows, rest), (["\x1b[31mabc\x1b[0m"], "def"))

    def test_colour_in_force_is_reported_for_the_remainder(self) -> None:
        #  颜色开在第一行、关在余下部分里：余下部分单独画时要先补上这段颜色
        rows, rest, active = self.live.split_rows("\x1b[31mabcdef\x1b[0m", 3)
        self.assertEqual((rows, rest, active), (["\x1b[31mabc"], "def\x1b[0m", "\x1b[31m"))

    def test_reset_clears_the_colour_in_force(self) -> None:
        self.assertEqual(self.live.split_rows("\x1b[31mab\x1b[0mcdef", 3)[2], "")

    def test_starting_colour_is_carried_in(self) -> None:
        self.assertEqual(self.live.split_rows("abcdef", 3, "\x1b[1m")[2], "\x1b[1m")

    def test_tab_advances_to_next_stop(self) -> None:
        self.assertEqual(self.live.visible_width("a\tb"), 9)
        self.assertEqual(self.live.visible_width("\x1b]0;标题\x07"), 0)

    def test_first_cell_keeps_leading_escapes(self) -> None:
        self.assertEqual(self.live.first_cell("\x1b[31m好x"), ("\x1b[31m好", 2))
        self.assertEqual(self.live.first_cell("\x1b[0m"), ("\x1b[0m", 0))

    def test_displayable_keeps_colour_only(self) -> None:
        text = "\x1b]133;A\x07a\tb\x1b[1mc\x07"
        self.assertEqual(self.live.displayable(text), "a       b\x1b[1mc")


@unittest.skipUnless(HAVE_TUI, "没装 TUI 可选依赖")
class LiveWriterTest(unittest.TestCase):
    def make(self):
        from xiaoyu.tui_live import LiveWriter

        real = io.StringIO()
        writer = LiveWriter(real)
        writer.passthrough = False
        return writer, real

    def test_passthrough_writes_straight_through(self) -> None:
        writer, real = self.make()
        writer.passthrough = True
        writer.write("直写\n")
        self.assertEqual(real.getvalue(), "直写\n")
        self.assertEqual(writer.pending, [])

    def test_lines_and_tail_are_separated(self) -> None:
        writer, real = self.make()
        writer.write("第一行\n第二")
        writer.write("行\n半")
        self.assertEqual(writer.pending, ["第一行", "第二行"])
        self.assertEqual(writer.tail, "半")
        self.assertEqual(real.getvalue(), "")

    def test_carriage_returns_are_normalised(self) -> None:
        writer, _ = self.make()
        writer.write("a\r\nb\rc\n")
        self.assertEqual(writer.pending, ["a", "bc"])

    def test_whole_line_from_another_thread_goes_before_an_open_tail(self) -> None:
        #  工作线程的半行还开着，主线程打一句提示：提示排到前面，半行不被粘上
        writer, _ = self.make()
        writer.write("流式正文的半")
        other = threading.Thread(target=writer.write, args=("  一句提示\n",))
        other.start()
        other.join()
        writer.write("行\n")
        self.assertEqual(writer.pending, ["  一句提示", "流式正文的半行"])
        self.assertEqual(writer.tail, "")

    def test_same_thread_keeps_writing_its_own_line(self) -> None:
        writer, _ = self.make()
        writer.write("半")
        writer.write("行\n")
        self.assertEqual(writer.pending, ["半行"])

    def test_drain_flushes_everything_to_the_real_stream(self) -> None:
        writer, real = self.make()
        writer.write("一\n二")
        writer.drain()
        self.assertEqual(real.getvalue(), "一\n二\n")
        self.assertEqual((writer.pending, writer.tail), ([], ""))

    def test_tracks_whether_output_ends_with_a_blank_line(self) -> None:
        writer, _ = self.make()
        self.assertTrue(writer.ends_blank)
        writer.write("正文\n")
        self.assertFalse(writer.ends_blank)
        writer.write("\n")
        self.assertTrue(writer.ends_blank)
        writer.write("半行")
        self.assertFalse(writer.ends_blank)

    def test_looks_like_a_terminal_to_its_users(self) -> None:
        writer, _ = self.make()
        self.assertTrue(writer.isatty())
        self.assertFalse(hasattr(writer, "buffer"))  # 有它的话别人会绕过 write 直写字节


@unittest.skipUnless(HAVE_TUI, "没装 TUI 可选依赖")
class TakeTest(unittest.TestCase):
    """LiveScreen._take：把攒着的输出变成要写的字节与占用行数。"""

    def make(self, cols: int = 10):
        from xiaoyu.tui_live import LiveScreen, LiveWriter

        writer = LiveWriter(io.StringIO())
        writer.passthrough = False
        app = SimpleNamespace(
            output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=cols, rows=24)),
            renderer=SimpleNamespace(render=lambda *args, **kwargs: None),
        )
        return LiveScreen(app, writer), writer

    def test_complete_line_ends_with_clear_and_newline(self) -> None:
        screen, writer = self.make()
        writer.write("你好\n")
        self.assertEqual(screen._take(False), ("你好\x1b[0m\x1b[K\r\n", 1))

    def test_long_line_counts_every_physical_row(self) -> None:
        screen, writer = self.make(cols=8)
        writer.write("abcdefghijklmnopqr\n")
        data, used = screen._take(False)
        self.assertEqual(used, 3)
        self.assertTrue(data.startswith("abcdefghijklmnopqr"))

    def test_tail_stays_until_it_fills_a_row(self) -> None:
        screen, writer = self.make()
        writer.write("abc")
        self.assertEqual(screen._take(False), ("", 0))
        self.assertEqual((writer.tail, screen.tail, screen.cont), ("abc", "abc", 0))

    def test_full_row_is_committed_as_a_soft_wrap(self) -> None:
        screen, writer = self.make(cols=8)
        writer.write("abcdefghijk")
        data, used = screen._take(False)
        #  铺满的一行 + 下一个字符触发折行 + 回到行首；那个字符仍留在尾巴里
        self.assertEqual((data, used), ("abcdefghi\x1b[0m\r", 1))
        self.assertEqual((writer.tail, screen.open, screen.cont), ("ijk", "abcdefgh", 1))

    def test_line_end_after_soft_wrap_closes_the_logical_line(self) -> None:
        screen, writer = self.make(cols=8)
        writer.write("abcdefghijk")
        screen._take(False)
        writer.write("\n")
        self.assertEqual(screen._take(False), ("ijk\x1b[0m\x1b[K\r\n", 1))
        self.assertEqual((screen.open, screen.cont), ("", 0))

    def test_control_only_tail_is_released_at_once(self) -> None:
        #  改窗口标题、响铃这类序列不占位置，不该等到下一个换行才生效
        screen, writer = self.make()
        writer.write("\x1b]0;标题\x07")
        self.assertEqual(screen._take(False), ("\x1b]0;标题\x07", 0))
        self.assertEqual(writer.tail, "")

    def test_final_take_flushes_the_tail_as_a_line(self) -> None:
        screen, writer = self.make()
        writer.write("半行")
        self.assertEqual(screen._take(True), ("半行\x1b[0m\x1b[K\r\n", 1))

    def test_first_row_shows_tail_without_control_noise(self) -> None:
        screen, writer = self.make()
        writer.write("\x1b[1ma\tb")
        screen._take(False)
        self.assertEqual(screen.first_row(), "\x1b[1ma       b")


@unittest.skipUnless(HAVE_TUI, "没装 TUI 可选依赖")
class SwitchTest(unittest.TestCase):
    def test_off_by_default(self) -> None:
        from xiaoyu.tui_live import enabled

        self.assertFalse(enabled({}))
        self.assertFalse(enabled({"XIAOYU_LIVE_INPUT": "0"}))
        self.assertTrue(enabled({"XIAOYU_LIVE_INPUT": "1"}))
        self.assertTrue(enabled({"XIAOYU_LIVE_INPUT": " On "}))

    def test_git_facts_outside_a_repository_is_empty(self) -> None:
        from xiaoyu.tui_live import _git_facts

        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(_git_facts(folder), "")

    def test_frontend_keeps_the_table_driven_keys(self) -> None:
        #  新前端只多出打断 / 退出两个键，其余按键仍来自按键表
        from pathlib import Path

        from rich.console import Console

        from xiaoyu.permissions import Permissions
        from xiaoyu.tui_live import LiveTui

        with tempfile.TemporaryDirectory() as folder:
            front = LiveTui(Permissions(Path(folder)), console=Console(file=io.StringIO()))
        extra = {binding.keys for binding in front._live_bindings().bindings}
        self.assertEqual(len(extra), 2)
        self.assertIsNotNone(front.sink.activity)
        self.assertIsNone(front.sink.live_footer)


if __name__ == "__main__":
    unittest.main()
