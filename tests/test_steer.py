"""steer（运行中追加输入）的测试。不打网络。

时机注入技巧：sink.emit / approver 都在 send() 期间被同步调用，
在里面回调 agent.steer() 就能把插话精确打在想测的边界上。
"""

from __future__ import annotations

import json
import os
import time
import unittest
from unittest import mock

from xiaoyu.agent import INTERJECTION_NOTE, INTERJECTION_TAIL, wrap_interjection
from xiaoyu.events import SteerAccepted, TextEnd, ToolCompleted, UIEvent

from .test_agent_paths import AgentTestCase, call_fragment, chunk


class ListSink:
    """记录全部事件；可挂一个"见到某类事件就做某事"的钩子。"""

    def __init__(self, on_event=None) -> None:
        self.events: list[UIEvent] = []
        self.on_event = on_event

    def emit(self, event: UIEvent) -> None:
        self.events.append(event)
        if self.on_event is not None:
            self.on_event(event)


def tool_turn(name: str, args: dict) -> list:
    return [chunk(tool_calls=[call_fragment(0, f"call_{name}", name, json.dumps(args))])]


def text_turn(text: str) -> list:
    return [chunk(content=text)]


class SteerTest(AgentTestCase):
    def test_steer_after_final_text_forces_another_step(self):
        """模型已给收尾正文，但期间用户插了话 → 不结束，再跑一步。"""
        agent = None

        def on_event(event: UIEvent) -> None:
            #  第一轮正文刚结束的瞬间插话（此时 send 还没检查队列）
            if isinstance(event, TextEnd) and not sink.saw_end:
                sink.saw_end = True
                agent.steer("补充：也检查一下 README")

        sink = ListSink(on_event)
        sink.saw_end = False
        agent = self.build([text_turn("做完了"), text_turn("README 也看过了")], sink=sink)
        agent.send("干活")

        #  插话成了 user 消息（带中途包装），且排在第一轮收尾正文之后
        wrapped = wrap_interjection("补充：也检查一下 README")
        contents = [(m.get("role"), m.get("content")) for m in agent.messages]
        self.assertIn(("user", wrapped), contents)
        idx_reply = contents.index(("assistant", "做完了"))
        idx_steer = contents.index(("user", wrapped))
        self.assertGreater(idx_steer, idx_reply)
        #  第二轮真的跑了
        self.assertEqual(contents[-1], ("assistant", "README 也看过了"))
        #  事件流里有确认
        accepted = [e for e in sink.events if isinstance(e, SteerAccepted)]
        self.assertEqual([e.text for e in accepted], ["补充：也检查一下 README"])

    def test_steer_lands_after_tool_batch(self):
        """工具执行期插话 → 一批工具收尾后、下一次模型调用前入历史。"""

        def on_event(event: UIEvent) -> None:
            if isinstance(event, ToolCompleted):
                agent.steer("顺便：别动测试文件")

        sink = ListSink(on_event)
        agent = self.build(
            [tool_turn("read_file", {"path": "calc.py"}), text_turn("好的")], sink=sink
        )
        agent.send("看代码")
        roles = [m.get("role") for m in agent.messages]
        #  … assistant(tool_calls) → tool → user(插话) → assistant
        self.assertEqual(roles[-3:], ["tool", "user", "assistant"])
        self.assertEqual(agent.messages[-2]["content"], wrap_interjection("顺便：别动测试文件"))

    def test_stale_steer_discarded_at_turn_start(self):
        agent = self.build([text_turn("ok")])
        agent.steer("上一轮的残留")
        agent.send("新任务")
        contents = [m.get("content") for m in agent.messages]
        self.assertNotIn("上一轮的残留", contents)
        self.assertEqual(agent.drain_steers(), [])

    def test_drain_returns_and_empties(self):
        agent = self.build([])
        agent.steer("一")
        agent.steer("二")
        self.assertEqual(agent.drain_steers(), ["一", "二"])
        self.assertEqual(agent.drain_steers(), [])

    def test_blank_steer_ignored_and_reset_clears(self):
        agent = self.build([])
        agent.steer("   ")
        self.assertEqual(agent.drain_steers(), [])
        agent.steer("有内容")
        agent.reset()
        self.assertEqual(agent.drain_steers(), [])


class WrapInterjectionTest(unittest.TestCase):
    """插话的包装格式：说明 + <user_query> + 收尾提醒。"""

    def test_wraps_with_note_query_and_tail(self):
        out = wrap_interjection("先修测试")
        self.assertTrue(out.startswith(f"{INTERJECTION_NOTE}\n<user_query>\n"))
        self.assertIn("先修测试", out)
        self.assertTrue(out.endswith(f"</user_query>\n{INTERJECTION_TAIL}"))

    def test_long_text_truncated(self):
        out = wrap_interjection("哈" * 30_000)
        self.assertIn("已截断", out)
        self.assertLess(len(out), 26_000)

    def test_short_text_untouched(self):
        self.assertNotIn("已截断", wrap_interjection("嗯"))


class _FakeSteerTarget:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def steer(self, text: str) -> None:
        self.lines.append(text)


@unittest.skipUnless(os.name == "posix", "SteerPoller 仅 posix（Windows 无 select-on-stdin）")
class PollerTest(unittest.TestCase):
    def test_reads_complete_lines_from_tty(self):
        """pty 实测：整行（带回车）被 steer；pause 后的输入不被偷走。"""
        import pty

        from xiaoyu import tui

        master, slave = pty.openpty()
        target = _FakeSteerTarget()
        fake_stdin = os.fdopen(slave, "rb", buffering=0, closefd=False)
        try:
            with mock.patch.object(tui.sys, "stdin", fake_stdin):
                poller = tui.SteerPoller(target)  # type: ignore[arg-type]
                self.assertTrue(poller.supported())
                poller.start()
                try:
                    os.write(master, "改用 unittest\n".encode())
                    #  正向断言：一出现就往下走，所以窗口给大点是免费的
                    deadline = time.monotonic() + 5.0
                    while not target.lines and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertEqual(target.lines, ["改用 unittest"])

                    #  pause 期间的整行必须留在缓冲里（确认框的按键不能被偷）。
                    #  ⚠️ 这里原本是 `sleep(0.25)  # 让线程真的退到 pause 分支`
                    #  + `sleep(0.4)`，两处都是把"机器有多快"写进了断言：
                    #  ① 那个 0.25 秒本就不必要——`_loop` 在 select 返回之后会
                    #     **再查一次** `_paused` 才决定读不读（见它的注释），
                    #     所以不存在"线程得先到达 pause 才安全"的时序前提；
                    #  ② 负向断言（"什么都没发生"）只能靠等，但该等成
                    #     **一出现就立刻失败**的轮询，而不是睡固定时长再看一眼——
                    #     后者在机器被抢 CPU 时会假红，且红了也说不清是真回归。
                    poller.pause()
                    os.write(master, "y\n".encode())
                    deadline = time.monotonic() + 10 * tui.SteerPoller._POLL
                    while time.monotonic() < deadline:
                        self.assertEqual(
                            target.lines, ["改用 unittest"], "pause 期间的整行被偷读了"
                        )
                        time.sleep(0.02)
                finally:
                    poller.stop()
        finally:
            fake_stdin.close()
            os.close(slave)
            os.close(master)

    def _start_on_pipe(self) -> tuple[_FakeSteerTarget, int]:
        """用 os.pipe 冒充 stdin 起轮询线程，返回 (插话收集器, 写端)。"""
        import threading

        from xiaoyu import tui

        read_fd, write_fd = os.pipe()
        target = _FakeSteerTarget()
        patcher = mock.patch.object(
            tui.sys, "stdin", mock.Mock(fileno=lambda: read_fd, isatty=lambda: True)
        )
        patcher.start()
        poller = tui.SteerPoller(target)  # type: ignore[arg-type]
        thread = threading.Thread(target=poller._loop, daemon=True)
        thread.start()

        def cleanup() -> None:
            poller._stop.set()
            #  关写端 → 读端 EOF，线程立刻收工
            try:
                os.close(write_fd)
            except OSError:
                pass
            thread.join(timeout=2.0)
            patcher.stop()
            os.close(read_fd)

        self.addCleanup(cleanup)
        return target, write_fd

    @staticmethod
    def _wait_lines(target: _FakeSteerTarget, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while len(target.lines) < count and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_burst_of_lines_becomes_one_steer(self):
        """粘贴多行：规范模式下逐行到达，窗口内连着来的合并成一条插话。"""
        from xiaoyu import tui

        #  窗口放大到 0.5s，行间 20ms 的间隔在繁忙机器上也稳落在窗口内
        with mock.patch.object(tui.SteerPoller, "_PASTE_WINDOW", 0.5):
            target, write_fd = self._start_on_pipe()
            for line in ("第一行", "", "第三行"):
                os.write(write_fd, f"{line}\n".encode())
                time.sleep(0.02)
            self._wait_lines(target, 1)
            self.assertEqual(target.lines, ["第一行\n\n第三行"])

    def test_lines_apart_beyond_window_stay_separate(self):
        from xiaoyu import tui

        with mock.patch.object(tui.SteerPoller, "_PASTE_WINDOW", 0.05):
            target, write_fd = self._start_on_pipe()
            os.write(write_fd, "先跑测试\n".encode())
            self._wait_lines(target, 1)
            #  间隔远大于窗口：用户分两次说的话仍是两条
            time.sleep(0.3)
            os.write(write_fd, "再改文档\n".encode())
            self._wait_lines(target, 2)
            self.assertEqual(target.lines, ["先跑测试", "再改文档"])

    def test_not_supported_without_tty(self):
        from xiaoyu import tui

        with mock.patch.object(tui.sys, "stdin", mock.Mock(isatty=lambda: False)):
            self.assertFalse(tui.SteerPoller.supported())


class LineEditorTest(unittest.TestCase):
    """自管行编辑的纯逻辑部分（不需要终端）。"""

    def test_utf8_split_across_reads_tab_and_wide_erase(self):
        from xiaoyu.tui import _LineEditor

        editor = _LineEditor()
        data = "中\t".encode()
        #  多字节字符被拆在两次 read 里：前半截不回显、不进文本
        self.assertEqual(editor.feed(data[:1]), "")
        self.assertEqual(editor.feed(data[1:]), "中 ")
        #  先删 tab（回显时占一格），再删中文（两格）
        self.assertEqual(editor.feed(b"\x7f\x7f"), "\b \b" + "\b\b  \b\b")
        self.assertEqual(editor.pending_text(), "")

    def test_combining_mark_erased_with_its_base(self):
        from xiaoyu.tui import _LineEditor

        editor = _LineEditor()
        editor.feed("xé".encode())
        self.assertEqual(editor.feed(b"\x7f"), "\b \b")
        self.assertEqual(editor.pending_text(), "x")

    def test_unfinished_escape_does_not_swallow_enter(self):
        from xiaoyu.tui import _LineEditor

        editor = _LineEditor()
        editor.feed(b"a\x1b[1\nb\x1bOAc")
        self.assertEqual(editor.lines, ["a"])
        self.assertEqual(editor.pending_text(), "bc")

    def test_disabled_cc_slot_is_not_a_key(self):
        from xiaoyu.tui import _LineEditor

        #  macOS 用 0xff、Linux 用 0 表示"该键被禁用"，不能把 ÿ / Ctrl-Space 当清行
        editor = _LineEditor(kill=b"\xff", werase=b"\x00")
        editor.feed("ÿ\x00".encode())
        self.assertEqual(editor.pending_text(), "ÿ")


@unittest.skipUnless(os.name == "posix", "自管行编辑仅 posix")
class OwnLineEditingTest(unittest.TestCase):
    """macOS/BSD 的自管行编辑（非规范模式）。测试里强制开启，Linux 上同样跑一遍。

    全部在真实 pty 上测：termios 的切换与还原、内核是否还在替我们回显，
    只有真终端设备说了算。
    """

    def setUp(self) -> None:
        import pty
        import select
        import termios
        import threading

        from xiaoyu import tui

        self.tui = tui
        self.termios = termios
        self.master, self.slave = pty.openpty()
        self.saved = termios.tcgetattr(self.slave)
        fake_stdin = os.fdopen(self.slave, "rb", buffering=0, closefd=False)
        for patcher in (
            mock.patch.object(tui.sys, "stdin", fake_stdin),
            mock.patch.object(tui.SteerPoller, "wants_own_editing", staticmethod(lambda: True)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.echo = bytearray()
        done = threading.Event()

        def pump() -> None:
            #  回显必须有人读走：pty 输出缓冲写满后回显阻塞，读线程随之停摆
            while not done.is_set():
                try:
                    if select.select([self.master], [], [], 0.05)[0]:
                        self.echo += os.read(self.master, 65536)
                except OSError:
                    return

        reader = threading.Thread(target=pump, daemon=True)
        reader.start()
        self.target = _FakeSteerTarget()
        self.poller = tui.SteerPoller(self.target)  # type: ignore[arg-type]

        def cleanup() -> None:
            self.poller.stop()
            done.set()
            reader.join(timeout=2.0)
            fake_stdin.close()
            os.close(self.slave)
            os.close(self.master)

        self.addCleanup(cleanup)

    #  ---------- 小工具 ----------

    def type(self, data: bytes) -> None:
        while data:
            data = data[os.write(self.master, data) :]

    @staticmethod
    def wait_for(predicate, timeout: float = 5.0) -> bool:
        #  正向等待：一满足就往下走，窗口给大是免费的
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            time.sleep(0.01)
        return bool(predicate())

    def restored(self) -> bool:
        #  切回规范模式后 lflag 可能多出内核维护的 PENDIN 位（待重显输入），不是我们设的
        pendin = getattr(self.termios, "PENDIN", 0)
        now = self.termios.tcgetattr(self.slave)
        expected = list(self.saved)
        now[3] &= ~pendin
        expected[3] &= ~pendin
        return now == expected

    def assert_raw(self) -> None:
        lflag = self.termios.tcgetattr(self.slave)[3]
        self.assertFalse(lflag & self.termios.ICANON)
        self.assertFalse(lflag & self.termios.ECHO)
        self.assertTrue(lflag & self.termios.ISIG, "Ctrl-C 必须照常发信号")

    #  ---------- 用例 ----------

    def test_long_single_line_arrives_whole(self):
        """超过 MAX_CANON 的单行（macOS 1024、Linux 4096）整行送达：规范模式下
        macOS 连回车一起丢，整行卡死在内核缓冲里。"""
        line = "长" * 1000 + "x" * 2000  # 5000 字节
        self.poller.start()
        self.assert_raw()
        self.type(line.encode() + b"\n")
        self.assertTrue(self.wait_for(lambda: self.target.lines), "超长行没有送达")
        self.assertEqual(self.target.lines, [line])
        self.assertEqual(self.poller.stop(), "")
        self.assertTrue(self.restored())

    def test_erase_kill_word_and_escape_sequences(self):
        self.poller.start()
        for typed, expected in (
            ("ab中\x7f\x7fc\n", "ac"),
            ("foo bar\x17baz\n", "foo baz"),
            ("丢掉\x15保留\n", "保留"),
            ("上\x1b[A一\n", "上一"),
        ):
            count = len(self.target.lines)
            self.type(typed.encode())
            self.assertTrue(self.wait_for(lambda: len(self.target.lines) > count), typed)
            self.assertEqual(self.target.lines[-1], expected)
        #  中文退格擦两列、ASCII 擦一列
        erased = "ab中\b\b  \b\b\b \bc".encode()
        self.assertTrue(self.wait_for(lambda: erased in bytes(self.echo)), bytes(self.echo))
        #  内核不再回显：否则"中"会出现两次（内核一份、我们一份）
        self.assertEqual(bytes(self.echo).count("中".encode()), 1)

    def test_pause_restores_tty_synchronously_and_nests(self):
        self.poller.start()
        self.poller.pause()
        #  pause 返回时终端必须已经还原——确认框紧接着就要接管
        self.assertTrue(self.restored())
        self.poller.pause()  # 嵌套：确认框里再弹规则编辑
        self.poller.resume()
        self.assertTrue(self.restored(), "内层 resume 提前切回了非规范模式")
        self.type(b"y\n")
        deadline = time.monotonic() + 10 * self.tui.SteerPoller._POLL
        while time.monotonic() < deadline:
            self.assertEqual(self.target.lines, [], "pause 期间的输入被偷读了")
            time.sleep(0.02)
        self.poller.resume()
        self.assert_raw()
        #  确认框没取走的输入，恢复后照常成为插话，不丢
        self.assertTrue(self.wait_for(lambda: self.target.lines))
        self.assertEqual(self.target.lines, ["y"])

    def test_stop_hands_back_partial_line_and_restores(self):
        self.poller.start()
        self.type("半截话".encode())
        self.assertTrue(self.wait_for(lambda: "半截话".encode() in bytes(self.echo)))
        #  紧接着再敲、立刻 stop：线程来不及读的字也要读干净交回
        self.type("尾巴".encode())
        self.assertEqual(self.poller.stop(), "半截话尾巴")
        self.assertEqual(self.target.lines, [])
        self.assertTrue(self.restored())

    def test_reader_crash_restores_tty(self):
        with mock.patch.object(self.tui._LineEditor, "feed", side_effect=RuntimeError("boom")):
            self.poller.start()
            self.assert_raw()
            self.type(b"x")
            self.assertTrue(self.wait_for(lambda: not self.poller._alive and self.restored()))
        #  线程没了：resume 不能再把终端切回无回显模式，否则用户敲字彻底看不见
        self.poller.pause()
        self.poller.resume()
        self.assertTrue(self.restored())

    def test_sigcont_reenters_after_shell_reset(self):
        import signal
        import threading

        if not hasattr(signal, "SIGCONT") or threading.current_thread() is not threading.main_thread():
            self.skipTest("需要 SIGCONT 且在主线程")
        before = signal.getsignal(signal.SIGCONT)
        #  pty 从端不是本进程的控制终端，前台判定在测试里只能替身
        with mock.patch.object(self.tui.os, "tcgetpgrp", lambda fd: os.getpgrp()):
            self.poller.start()
            #  模拟 Ctrl-Z 挂起期间 shell 把终端改回了它自己的模式，随后 fg
            self.termios.tcsetattr(self.slave, self.termios.TCSANOW, self.saved)
            os.kill(os.getpid(), signal.SIGCONT)
            self.assertTrue(
                self.wait_for(lambda: not self.termios.tcgetattr(self.slave)[3] & self.termios.ICANON)
            )
            self.poller.stop()
        self.assertTrue(self.restored())
        self.assertEqual(signal.getsignal(signal.SIGCONT), before)

    def test_canonical_path_kept_where_not_wanted(self):
        """不需要自管的平台保持内核行规程：终端模式一位不动。"""
        with mock.patch.object(self.tui.SteerPoller, "wants_own_editing", staticmethod(lambda: False)):
            self.poller.start()
            self.assertTrue(self.restored())
            self.type("改用 unittest\n".encode())
            self.assertTrue(self.wait_for(lambda: self.target.lines))
            self.assertEqual(self.target.lines, ["改用 unittest"])
            self.assertEqual(self.poller.stop(), "")


if __name__ == "__main__":
    unittest.main()
