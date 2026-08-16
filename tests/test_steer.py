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

    def test_not_supported_without_tty(self):
        from xiaoyu import tui

        with mock.patch.object(tui.sys, "stdin", mock.Mock(isatty=lambda: False)):
            self.assertFalse(tui.SteerPoller.supported())


if __name__ == "__main__":
    unittest.main()
