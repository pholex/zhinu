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


class TurnCountingTest(AgentTestCase):
    """中途插话是用户的原话，但不是一轮的开头。"""

    def run_two_turns_with_a_steer(self):
        from xiaoyu.agent import SYNTHETIC_USER_TEXTS

        agent = None

        def on_event(event):
            if isinstance(event, TextEnd) and not fired:
                fired.append(True)
                agent.steer("顺便把测试也跑一下")

        fired: list = []
        sink = ListSink(on_event)
        agent = self.build(
            [[chunk(content="第一轮答完")], [chunk(content="测试也跑了")], [chunk(content="第二轮")]],
            sink=sink,
        )
        agent.send("第一个问题")
        agent.send("第二个问题")
        return agent, SYNTHETIC_USER_TEXTS

    def test_steer_is_not_counted_as_a_turn(self):
        from xiaoyu import media
        from xiaoyu.session_log import turn_starts

        agent, synthetic = self.run_two_turns_with_a_steer()
        steers = [m for m in agent.messages if m.get(media.MIDTURN_KEY)]
        self.assertEqual(len(steers), 1)
        starts = turn_starts(agent.messages, synthetic)
        self.assertEqual(
            [agent.messages[index]["content"] for index in starts], ["第一个问题", "第二个问题"]
        )

    def test_old_logs_without_the_mark_are_recognised_by_wording(self):
        from xiaoyu.session_log import turn_starts

        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "第一个问题"},
            {"role": "assistant", "content": "答"},
            {"role": "user", "content": wrap_interjection("插一句")},
            {"role": "assistant", "content": "好"},
        ]
        self.assertEqual(turn_starts(messages), [1])

    def test_steer_still_counts_as_the_users_own_words(self):
        """压缩时要当原话备份：不能被归进 harness 注入的那一类。"""
        from xiaoyu import media

        agent, synthetic = self.run_two_turns_with_a_steer()
        (steer,) = [m for m in agent.messages if m.get(media.MIDTURN_KEY)]
        self.assertFalse(media.is_injected_message(steer, synthetic))

    def test_the_mark_never_leaves_the_process(self):
        from xiaoyu import media

        from xiaoyu import responses

        agent, _ = self.run_two_turns_with_a_steer()
        #  出网口统一摘掉下划线开头的私有键；桩 client 接在那之前，所以直接验出网口
        for message in responses.strip_private(agent.messages):
            self.assertNotIn(media.MIDTURN_KEY, message)


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

    def test_slash_command_is_queued_not_steered(self):
        """运行中敲的斜杠命令是对程序说的，不进模型的历史。"""
        from xiaoyu import tui

        with mock.patch.object(tui.SteerPoller, "_PASTE_WINDOW", 0.05):
            target, write_fd = self._start_on_pipe()
            os.write(write_fd, "/usage\n".encode())
            time.sleep(0.3)
            #  紧跟一条普通插话当哨兵：它到了，说明前一行已经处理完
            os.write(write_fd, "接着干\n".encode())
            self._wait_lines(target, 1)
            self.assertEqual(target.lines, ["接着干"])


class DeliverRoutingTest(unittest.TestCase):
    """提交后的分流：只有命令形状的单行进命令队列。"""

    def _deliver(self, data: str):
        from xiaoyu import tui

        target = _FakeSteerTarget()
        poller = tui.SteerPoller(target)  # type: ignore[arg-type]
        poller._apply(poller.draft.feed(data.encode()))
        poller._apply(poller.draft.settle())
        return target.lines, list(poller.commands)

    def test_command_with_arguments(self):
        self.assertEqual(self._deliver("  /model foo \n"), ([], ["/model foo"]))

    def test_path_like_text_goes_to_the_model(self):
        lines, commands = self._deliver("/etc/hosts 也看一下\n")
        self.assertEqual((lines, commands), (["/etc/hosts 也看一下"], []))

    def test_multiline_paste_goes_to_the_model(self):
        lines, commands = self._deliver("/usage\n第二行\n")
        self.assertEqual((lines, commands), (["/usage\n第二行"], []))


class _QueueTarget:
    """带待送达队列的插话收集器：撤回要用到 pending_steers / retract_steer。"""

    def __init__(self) -> None:
        self.queue: list[tuple[str, str]] = []

    def steer(self, text: str) -> None:
        self.queue.append((f"id{len(self.queue)}", text))

    def pending_steers(self) -> tuple[tuple[str, str], ...]:
        return tuple(self.queue)

    def retract_steer(self, ident: str) -> str | None:
        for index, (key, text) in enumerate(self.queue):
            if key == ident:
                del self.queue[index]
                return text
        return None


class RetractTest(unittest.TestCase):
    """Esc：有草稿清草稿，没草稿就撤回最后一条还没生效的输入。"""

    def setUp(self) -> None:
        from xiaoyu import tui

        self.target = _QueueTarget()
        self.poller = tui.SteerPoller(self.target)  # type: ignore[arg-type]

    def _type(self, data: str) -> None:
        self.poller._apply(self.poller.draft.feed(data.encode()))
        self.poller._apply(self.poller.draft.settle())

    def test_escape_pulls_the_last_pending_steer_back_into_the_draft(self):
        self._type("第一条\r")
        self._type("第二条\r")
        self._type("\x1b")
        self.assertEqual(self.poller.draft.text, "第二条")
        self.assertEqual([text for _, text in self.target.queue], ["第一条"])

    def test_escape_with_a_draft_only_clears_it(self):
        self._type("已送出\r")
        self._type("写了一半\x1b")
        self.assertEqual(self.poller.draft.text, "")
        self.assertEqual(len(self.target.queue), 1, "有草稿时 Esc 不该动已送出的")

    def test_escape_pulls_back_a_queued_command_first(self):
        self._type("补充一句\r")
        self._type("/clear\r")
        self._type("\x1b")
        self.assertEqual(self.poller.draft.text, "/clear")
        self.assertFalse(self.poller.commands)
        self.assertEqual(len(self.target.queue), 1)

    def test_escape_with_nothing_pending_is_a_no_op(self):
        self._type("\x1b")
        self.assertEqual(self.poller.draft.text, "")


@unittest.skipUnless(os.name == "posix", "逐键模式仅 posix（termios）")
class RawModeTest(unittest.TestCase):
    """pty 实测：逐键模式关回显、逐键可读，pause / stop 把终端模式还回去。"""

    def setUp(self) -> None:
        import pty
        import termios

        from xiaoyu import tui

        self.termios = termios
        self.master, self.slave = pty.openpty()
        fake_stdin = os.fdopen(self.slave, "rb", buffering=0, closefd=False)
        patcher = mock.patch.object(tui.sys, "stdin", fake_stdin)
        patcher.start()
        #  模式开关序列写的是真终端 fd：测试里拦下来，别往跑测试的终端上写
        self.written: list[str] = []
        writer = mock.patch.object(
            tui.SteerPoller, "_terminal_write", staticmethod(self.written.append)
        )
        writer.start()
        self.target = _QueueTarget()
        self.poller = tui.SteerPoller(self.target, raw=True)  # type: ignore[arg-type]

        def cleanup() -> None:
            self.poller.stop()
            writer.stop()
            patcher.stop()
            fake_stdin.close()
            os.close(self.slave)
            os.close(self.master)

        self.addCleanup(cleanup)

    def _lflag(self) -> int:
        #  只比我们动过的三位：内核自己会在 lflag 里置状态位（macOS 切回规范模式时
        #  的 PENDIN），整字相等会把那个也算成"没还原"
        mask = self.termios.ICANON | self.termios.ECHO | self.termios.ISIG
        return self.termios.tcgetattr(self.slave)[3] & mask

    def _wait(self, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while not condition() and time.monotonic() < deadline:
            time.sleep(0.01)

    def test_keys_arrive_without_enter_and_mode_is_restored(self):
        from xiaoyu import tui

        before = self._lflag()
        self.assertTrue(before & self.termios.ICANON, "前提：pty 起始在规范模式")
        self.poller.start()
        self.assertTrue(self.poller.raw)
        self.assertFalse(self._lflag() & (self.termios.ICANON | self.termios.ECHO))
        self.assertTrue(self._lflag() & self.termios.ISIG, "Ctrl-C 必须仍是信号")
        self.assertEqual(self.written, [tui._PASTE_ON])

        os.write(self.master, "半句".encode())
        self._wait(lambda: self.poller.draft.text == "半句")
        self.assertEqual(self.poller.draft.text, "半句", "没按回车的按键也该进草稿")
        self.assertEqual(self.target.queue, [])

        os.write(self.master, b"\r")
        self._wait(lambda: self.target.queue)
        self.assertEqual([text for _, text in self.target.queue], ["半句"])

        self.poller.pause()
        self.assertEqual(self._lflag(), before, "确认框接管前要把终端模式还回去")
        self.poller.resume()
        self.assertFalse(self._lflag() & self.termios.ICANON)
        self.poller.stop()
        self.assertEqual(self._lflag(), before)
        self.assertEqual(self.written[-1], tui._PASTE_OFF)

    def test_stop_keeps_the_unsent_draft_for_the_input_line(self):
        self.poller.start()
        os.write(self.master, "没发完".encode())
        self._wait(lambda: self.poller.draft.text == "没发完")
        self.poller.stop()
        self.assertEqual(self.poller.draft.take(), "没发完")
        self.assertEqual(self.target.queue, [])


class MidturnCommandTest(unittest.TestCase):
    """主线程在安全点取走命令：只读的当场跑，其余留到轮末预填。"""

    def setUp(self) -> None:
        import collections
        import io
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace

        from rich.console import Console

        from xiaoyu.permissions import Permissions
        from xiaoyu.tui import Tui

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.output = io.StringIO()
        self.tui = Tui(Permissions(Path(tmp.name)), console=Console(file=self.output, width=120))
        self.commands: collections.deque[str] = collections.deque()
        #  /usage 只读 agent.usage；别的属性一碰就 AttributeError，正好当"没被执行"的哨兵
        agent = SimpleNamespace(usage="用量哨兵 42 tok")
        self.tui._poller = SimpleNamespace(commands=self.commands, agent=agent)

    def test_read_only_command_runs_in_place(self):
        self.commands.append("/usage")
        self.tui._run_midturn_commands()
        shown = self.output.getvalue()
        self.assertIn("↳ /usage", shown)
        self.assertIn("用量哨兵 42 tok", shown)
        self.assertEqual(self.tui._held_commands, [])
        self.assertFalse(self.commands)

    def test_other_command_is_held_for_the_input_line(self):
        self.commands.append("/clear")
        self.tui._run_midturn_commands()
        self.assertEqual(self.tui._held_commands, ["/clear"])
        self.assertIn("/clear 要等本轮结束", self.output.getvalue())

    def test_waits_while_a_text_block_is_open(self):
        from xiaoyu.events import Notice

        self.commands.append("/usage")
        self.tui.sink._text_open = True
        self.tui.sink.emit(Notice("正文还没收尾", "info"))
        self.assertEqual(list(self.commands), ["/usage"], "半截正文悬着时不该插进输出")
        self.tui.sink._text_open = False
        self.tui.sink.emit(Notice("收尾了", "info"))
        self.assertFalse(self.commands)
        self.assertIn("用量哨兵 42 tok", self.output.getvalue())

    def test_worker_thread_never_runs_commands(self):
        import threading

        self.commands.append("/usage")
        worker = threading.Thread(target=self.tui._run_midturn_commands)
        worker.start()
        worker.join()
        self.assertEqual(list(self.commands), ["/usage"])

    def test_footer_shows_waiting_items_and_the_draft(self):
        from types import SimpleNamespace

        from xiaoyu import midturn

        draft = midturn.Draft()
        agent = SimpleNamespace(pending_steers=lambda: (("id0", "先跑测试\n再提交"),))
        self.tui._poller = SimpleNamespace(
            commands=self.commands, agent=agent, draft=draft, raw=True
        )
        self.commands.append("/usage")
        lines = [line.plain for line in self.tui._live_footer()]
        self.assertEqual(lines[0], "↳ 待执行：/usage")
        self.assertEqual(lines[1], "↳ 待送达：先跑测试 ⏎ 再提交")
        self.assertIn("Esc 撤回", lines[2])

        draft.text = "正在敲"
        lines = [line.plain for line in self.tui._live_footer()]
        self.assertTrue(lines[-1].startswith("› 正在敲▏"), lines[-1])

        #  整行模式：回显归终端，页脚不再画一遍草稿
        self.tui._poller.raw = False
        self.assertEqual(len(self.tui._live_footer()), 2)

    def test_footer_keeps_the_tail_of_a_long_draft_on_one_line(self):
        from types import SimpleNamespace

        from rich.cells import cell_len

        from xiaoyu import midturn

        draft = midturn.Draft()
        draft.text = "很长的一句话" * 60 + "结尾"
        self.tui._poller = SimpleNamespace(
            commands=self.commands, agent=SimpleNamespace(pending_steers=tuple), draft=draft, raw=True
        )
        (line,) = self.tui._live_footer()
        self.assertLessEqual(cell_len(line.plain), self.tui.console.width - 2)
        self.assertIn("…", line.plain)
        self.assertIn("结尾▏", line.plain)

    def test_every_midturn_command_is_a_registered_command(self):
        from xiaoyu.cli import MIDTURN_COMMANDS, SLASH_COMMANDS

        self.assertLessEqual(MIDTURN_COMMANDS, set(SLASH_COMMANDS))


if __name__ == "__main__":
    unittest.main()
