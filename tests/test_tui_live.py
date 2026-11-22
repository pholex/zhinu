"""常驻输入区前端（xiaoyu/tui_live.py）里不依赖真实终端的部分。

终端里的实际表现（补打、缩放、让位）靠 tmux 里的自动验收看，不在这里：
`experiments/persistent_prompt_proto.py --selfcheck`。
"""

from __future__ import annotations

import importlib.util
import io
import sys
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
        from xiaoyu.tui_live import LiveWriter

        class Terminal(io.StringIO):
            def isatty(self) -> bool:
                return True

        self.assertTrue(LiveWriter(Terminal()).isatty())
        writer, _ = self.make()
        self.assertFalse(writer.isatty())  # 真正的去处不是终端，就不冒充
        self.assertFalse(hasattr(writer, "buffer"))  # 有它的话别人会绕过 write 直写字节

    def test_everything_written_is_remembered_as_logical_lines(self) -> None:
        #  缩放后整屏重打靠这份记录：直写与攒着两种状态都要记，半行接得上
        writer, _ = self.make()
        writer.write("开场\n半")
        writer.passthrough = False
        writer.write("行\n正文")
        self.assertEqual(list(writer.history), ["开场", "半行"])
        self.assertEqual(writer.line, "正文")
        with writer.lock:
            writer.break_line()
        self.assertEqual(list(writer.history), ["开场", "半行", "正文"])
        self.assertEqual(writer.line, "")

    def test_history_is_capped(self) -> None:
        from xiaoyu import tui_live

        writer, _ = self.make()
        writer.write("x\n" * (tui_live.HISTORY_LINES + 10))
        self.assertEqual(len(writer.history), tui_live.HISTORY_LINES)

    def test_block_is_recomputed_on_replay(self) -> None:
        writer, real = self.make()
        widths = iter(["宽版横幅", "窄版横幅"])
        writer.block(lambda: next(widths))
        self.assertIn("宽版横幅", real.getvalue())
        self.assertEqual(writer.history[-1](), "窄版横幅")

    def test_replay_does_not_ring_the_bell_again(self) -> None:
        from xiaoyu.tui_live import replayable

        self.assertEqual(replayable("完成\x07"), "完成")
        #  OSC 序列里作结束符的那个不是响铃，留着
        self.assertEqual(replayable("\x1b]0;标题\x07正文"), "\x1b]0;标题\x07正文")


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
    def test_on_by_default(self) -> None:
        from xiaoyu.tui_live import enabled

        self.assertTrue(enabled({}))
        self.assertTrue(enabled({"XIAOYU_LIVE_INPUT": "1"}))
        self.assertTrue(enabled({"XIAOYU_LIVE_INPUT": ""}))
        self.assertFalse(enabled({"XIAOYU_LIVE_INPUT": "0"}))
        self.assertFalse(enabled({"XIAOYU_LIVE_INPUT": " Off "}))

    def frontend(self, environ: dict[str, str], **options: bool):
        from pathlib import Path
        from unittest import mock

        from xiaoyu import terminal
        from xiaoyu.cli import make_frontend
        from xiaoyu.permissions import Permissions
        from xiaoyu.tui_live import LiveTui

        with tempfile.TemporaryDirectory() as folder, mock.patch(
            "sys.stdin"
        ) as fake_in, mock.patch("sys.stdout") as fake_out, mock.patch.object(
            terminal, "autodetect"
        ), mock.patch.object(LiveTui, "begin_recording"), mock.patch.dict(
            "os.environ", environ
        ):
            fake_in.isatty.return_value = True
            fake_out.isatty.return_value = True
            _approver, _sink, run, *_rest = make_frontend(Permissions(Path(folder)), **options)
        return run.__self__

    @unittest.skipIf(sys.platform == "win32", "Windows 一律用原来的前端")
    def test_cli_picks_the_live_frontend_unless_told_otherwise(self) -> None:
        from xiaoyu.tui_live import LiveTui

        self.assertIsInstance(self.frontend({"XIAOYU_LIVE_INPUT": ""}), LiveTui)
        self.assertNotIsInstance(self.frontend({"XIAOYU_LIVE_INPUT": ""}, classic=True), LiveTui)
        self.assertNotIsInstance(self.frontend({"XIAOYU_LIVE_INPUT": "0"}), LiveTui)

    def test_classic_flag_is_on_both_entry_parsers(self) -> None:
        from xiaoyu.cli import build_parser

        self.assertFalse(build_parser().parse_args([]).classic)
        self.assertTrue(build_parser().parse_args(["--classic"]).classic)

    def test_git_facts_outside_a_repository_is_empty(self) -> None:
        from xiaoyu.tui_live import _git_facts

        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(_git_facts(folder), "")

    def test_frontend_keeps_the_table_driven_keys(self) -> None:
        #  新前端只多出打断 / 退出（Ctrl-C 与信号）和作答中的 Esc，其余按键仍来自按键表
        from pathlib import Path

        from rich.console import Console

        from xiaoyu.permissions import Permissions
        from xiaoyu.tui_live import LiveTui

        with tempfile.TemporaryDirectory() as folder:
            front = LiveTui(Permissions(Path(folder)), console=Console(file=io.StringIO()))
        extra = {binding.keys for binding in front._live_bindings().bindings}
        self.assertEqual(len(extra), 3)
        self.assertIsNotNone(front.sink.activity)
        self.assertIsNone(front.sink.live_footer)


@unittest.skipUnless(HAVE_TUI, "没装 TUI 可选依赖")
class PanelTest(unittest.TestCase):
    """确认菜单、提问面板、单行追问嵌进输入区：只验状态怎么流转，画面靠 tmux 验收。"""

    def front(self):
        from pathlib import Path

        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from rich.console import Console

        from xiaoyu.permissions import Permissions
        from xiaoyu.tui_live import LiveTui

        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        pipe = create_pipe_input()
        self.addCleanup(pipe.__exit__, None, None, None)
        front = LiveTui(Permissions(Path(folder.name)), console=Console(file=io.StringIO()))
        front._session = PromptSession(
            input=pipe.__enter__(), output=DummyOutput(), completer=object(), multiline=True
        )
        return front

    def test_menu_runs_in_the_host_it_is_given(self) -> None:
        #  tui.py 的接缝：菜单把按键与画面交给宿主，结果经宿主的 finish 交回
        from xiaoyu.tui import inline_select

        seen: dict = {}

        class Host:
            def run(self, bindings, fragments, height):
                seen.update(bindings=bindings, fragments=fragments, height=height)
                digit = next(b for b in bindings.bindings if b.keys == ("2",))
                digit.handler(None)
                return seen["result"]

            def finish(self, result):
                seen["result"] = result

            def above(self, action):
                action()

        options = [("once", "允许一次", "y"), ("deny", "拒绝", "n")]
        self.assertEqual(inline_select("要执行吗？", options, host=Host()), "deny")
        self.assertEqual(seen["height"], 4)
        self.assertIn("允许一次", "".join(text for _style, text in seen["fragments"]()))

    def test_without_an_input_area_asking_counts_as_cancelled(self) -> None:
        front = self.front()
        self.assertIsNone(front._menu_host().run(None, list, 1))
        with self.assertRaises(EOFError):
            front._confirm_prompt([("", "  规则：")], default="allow bash(ls *)")

    def test_line_question_borrows_the_input_line_and_gives_it_back(self) -> None:
        from prompt_toolkit.document import Document

        from xiaoyu.tui_live import _Panel

        front = self.front()
        session = front._session
        buffer = session.default_buffer
        completer = session.completer
        buffer.reset(Document("半截草稿"))
        panel = _Panel("text")
        panel.label, panel.default, panel.password = [("", "  密码：")], "", True
        front._open(panel)
        self.assertEqual(buffer.text, "")
        self.assertTrue(session.is_password)
        self.assertIsNone(session.completer)
        self.assertEqual(front._live_message()[-1], ("", "  密码："))
        buffer.reset(Document("hunter2"))
        front._hand_off(buffer)
        self.assertTrue(panel.done.is_set())
        self.assertEqual(panel.result, "hunter2")
        self.assertEqual(buffer.text, "半截草稿")
        self.assertFalse(session.is_password)
        self.assertIs(session.completer, completer)
        self.assertIsNone(front._panel)

    def test_empty_answer_to_a_line_question_is_an_answer(self) -> None:
        from xiaoyu.tui_live import _CANCELLED, _Panel

        front = self.front()
        panel = _Panel("text")
        panel.result = _CANCELLED
        front._open(panel)
        front._hand_off(front._session.default_buffer)
        self.assertEqual(panel.result, "")

    def test_menu_is_drawn_above_the_input_line_and_keeps_the_draft(self) -> None:
        from prompt_toolkit.document import Document

        from xiaoyu.tui_live import _Panel

        front = self.front()
        buffer = front._session.default_buffer
        buffer.reset(Document("半截草稿"))
        panel = _Panel("menu")
        panel.fragments = lambda: [("class:menu-title", "  要执行这条命令吗？")]
        front._open(panel)
        drawn = "".join(text for _style, text in front._live_message())
        self.assertIn("要执行这条命令吗？", drawn)
        self.assertTrue(drawn.endswith("❯ "))
        front._hand_off(buffer)  # 回车归菜单管，漏到这里也不能把草稿当消息发出去
        self.assertEqual(buffer.text, "半截草稿")
        self.assertTrue(front._jobs.empty())
        front._menu_host().finish("once")
        self.assertEqual(panel.result, "once")
        self.assertIsNone(front._panel)


class InterruptibleIoTest(unittest.TestCase):
    """agent 在工作线程里跑时，打断要能打进卡在网络读上的那一刻（Agent._blocking）。"""

    def agent(self, enabled: bool = True):
        from xiaoyu.agent import Agent

        agent = Agent.__new__(Agent)
        agent._interrupt_flag = threading.Event()
        agent._upstream_stop = None
        agent.interruptible_io = enabled
        return agent

    def test_result_and_errors_come_back_to_the_caller(self) -> None:
        agent = self.agent()
        self.assertEqual(agent._blocking(lambda: 42), 42)
        with self.assertRaises(ZeroDivisionError):
            agent._blocking(lambda: 1 // 0)

    def test_interrupt_reaches_a_call_stuck_on_the_network(self) -> None:
        import time

        from xiaoyu.errors import Interrupted

        agent = self.agent()
        release = threading.Event()
        closed = threading.Event()
        stream = SimpleNamespace(close=closed.set)

        def stuck():
            release.wait(10)
            return stream

        threading.Timer(0.15, agent._interrupt_flag.set).start()
        started = time.monotonic()
        with self.assertRaises(Interrupted):
            agent._blocking(stuck)
        self.assertLess(time.monotonic() - started, 2)
        #  被丢下的那次调用晚一步拿到了流：没人要，自己关掉
        release.set()
        self.assertTrue(closed.wait(2))

    def test_stream_events_keep_order_and_end(self) -> None:
        agent = self.agent()
        self.assertEqual(list(agent._events_of(iter("abc"))), ["a", "b", "c"])

    def test_result_that_beats_the_interrupt_is_closed_not_leaked(self) -> None:
        #  调用刚交回流、打断就到了：这份流调用方拿不到，得有人关
        import time

        from xiaoyu.errors import Interrupted

        agent = self.agent()
        release = threading.Event()
        closed = threading.Event()
        stream = SimpleNamespace(close=closed.set)

        def stuck():
            release.wait(10)
            return stream

        def requested() -> bool:
            release.set()
            time.sleep(0.05)
            agent._interrupt_flag.set()
            return True

        agent.interrupt_requested = requested
        with self.assertRaises(Interrupted):
            agent._blocking(stuck)
        self.assertTrue(closed.wait(2))

    def test_one_helper_thread_serves_the_whole_stream(self) -> None:
        agent = self.agent()

        def stream():
            for _ in range(5):
                yield threading.current_thread()

        readers = set(agent._events_of(stream()))
        self.assertEqual(len(readers), 1)
        reader = readers.pop()
        self.assertIsNot(reader, threading.current_thread())
        reader.join(2)
        self.assertFalse(reader.is_alive())

    def test_helper_thread_exits_when_the_caller_stops_reading(self) -> None:
        agent = self.agent()

        def stream():
            while True:
                yield threading.current_thread()

        events = agent._events_of(stream())
        reader = next(events)
        events.close()
        reader.join(2)
        self.assertFalse(reader.is_alive())

    def test_interrupt_reaches_a_stream_stuck_between_events(self) -> None:
        import time

        from xiaoyu.errors import Interrupted

        agent = self.agent()
        release = threading.Event()

        def stream():
            yield "a"
            release.wait(10)

        events = agent._events_of(stream())
        self.assertEqual(next(events), "a")
        threading.Timer(0.15, agent._interrupt_flag.set).start()
        started = time.monotonic()
        with self.assertRaises(Interrupted):
            next(events)
        self.assertLess(time.monotonic() - started, 2)
        release.set()

    def test_stream_errors_come_back_to_the_caller(self) -> None:
        agent = self.agent()

        def stream():
            yield "a"
            raise ZeroDivisionError

        events = agent._events_of(stream())
        self.assertEqual(next(events), "a")
        with self.assertRaises(ZeroDivisionError):
            next(events)

    def test_off_by_default_is_a_plain_call(self) -> None:
        agent = self.agent(enabled=False)
        stream = iter("ab")
        self.assertIs(agent._events_of(stream), stream)
        self.assertEqual(agent._blocking(threading.current_thread), threading.current_thread())

    def test_helper_thread_sees_the_callers_context(self) -> None:
        #  流总时限记在上下文变量里，挪到辅助线程后还得查得到
        from xiaoyu.streaming import StreamLifetimeExceeded, check_deadline, stream_scope

        import time

        agent = self.agent()

        def late_check() -> None:
            #  等过时钟的粒度再查：Windows 上旧版解释器的单调时钟约 15 毫秒才走一格，
            #  立刻查的话时限还没到
            time.sleep(0.05)
            check_deadline()

        with stream_scope(0.001):
            with self.assertRaises(StreamLifetimeExceeded):
                agent._blocking(late_check)


if __name__ == "__main__":
    unittest.main()
