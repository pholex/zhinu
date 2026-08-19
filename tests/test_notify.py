"""notify（系统通知搭工具结果送达）的测试。不打网络。

后台完成的"搭便车"提醒：通知包成 <system-reminder> 附在
下一条工具结果尾部；模型已给收尾正文、没有工具结果可搭时，在 step 边界
以独立消息补投并强制再跑一步。同一 key 整个会话只送达一次。

wake=False 的公告（MCP server 上线一类）只搭便车，绝不为自己多跑那一步——
多跑一步模型会把同一个问题再答一遍，client 端拼成一条就是答案重复。
"""

from __future__ import annotations

import contextlib
import io
import unittest

from xiaoyu.events import TextEnd, UIEvent

from .test_agent_paths import AgentTestCase
from .test_steer import ListSink, text_turn, tool_turn


class NotifyTest(AgentTestCase):
    def test_notification_rides_next_tool_result(self):
        """空闲时投递的通知在下一轮的第一条工具结果上送达，trace 不带它。"""
        agent = self.build([tool_turn("read_file", {"path": "calc.py"}), text_turn("好")])
        agent.notify("后台任务 job-1 已完成")
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("干活")
        tool_msg = next(m for m in agent.messages if m.get("role") == "tool")
        self.assertIn(
            "<system-reminder>\n后台任务 job-1 已完成\n</system-reminder>",
            tool_msg["content"],
        )
        #  trace 与事件记录的是不带通知的原始输出（通知只面向模型）
        self.assertNotIn("system-reminder", agent.trace[0]["output"])

    def test_same_key_delivered_once(self):
        agent = self.build([tool_turn("read_file", {"path": "calc.py"}), text_turn("好")])
        agent.notify("job 完成", key="job-1")
        agent.notify("job 完成（重复投递）", key="job-1")
        agent.notify("另一件事", key="job-2")
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("干活")
        tool_msg = next(m for m in agent.messages if m.get("role") == "tool")
        self.assertEqual(tool_msg["content"].count("job 完成"), 1)
        self.assertIn("另一件事", tool_msg["content"])

    def test_flush_at_final_text_forces_another_step(self):
        """收尾正文期间到达的通知：step 边界补投为独立消息，并再跑一步。"""
        agent = None

        def on_event(event: UIEvent) -> None:
            if isinstance(event, TextEnd) and not sink.saw_end:
                sink.saw_end = True
                agent.notify("部署已完成")

        sink = ListSink(on_event)
        sink.saw_end = False
        agent = self.build([text_turn("做完了"), text_turn("收到")], sink=sink)
        agent.send("干活")
        contents = [(m.get("role"), m.get("content")) for m in agent.messages]
        self.assertIn(("user", "<system-reminder>\n部署已完成\n</system-reminder>"), contents)
        self.assertEqual(contents[-1], ("assistant", "收到"))

    def test_non_waking_notification_does_not_force_another_step(self):
        """wake=False：收尾正文期间到达也不多跑一步——多跑一步的代价是模型
        把同一个问题再答一遍（client 端拼成一条 = 用户看到答案重复）。"""
        agent = None

        def on_event(event: UIEvent) -> None:
            if isinstance(event, TextEnd) and not sink.saw_end:
                sink.saw_end = True
                agent.notify("MCP server github 已上线", wake=False)

        sink = ListSink(on_event)
        sink.saw_end = False
        #  只排一轮脚本：真多跑一步会取到第二轮，这里取不到就是没跑
        agent = self.build([text_turn("做完了")], sink=sink)
        agent.send("干活")
        contents = [(m.get("role"), m.get("content")) for m in agent.messages]
        self.assertEqual(contents[-1], ("assistant", "做完了"))
        self.assertNotIn("MCP server github 已上线", str(contents))

    def test_non_waking_notification_still_rides_next_tool_result(self):
        """不唤醒 ≠ 丢弃：留在队列里，下一条工具结果照常带上。"""
        agent = self.build([tool_turn("read_file", {"path": "calc.py"}), text_turn("好")])
        agent.notify("MCP server github 已上线", wake=False)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("干活")
        tool_msg = next(m for m in agent.messages if m.get("role") == "tool")
        self.assertIn("MCP server github 已上线", tool_msg["content"])

    def test_waking_notification_carries_non_waking_ones_along(self):
        """这一步反正要跑（有 wake 项）：不唤醒的那条捎带一起送，不必再等。"""
        agent = None

        def on_event(event: UIEvent) -> None:
            if isinstance(event, TextEnd) and not sink.saw_end:
                sink.saw_end = True
                agent.notify("MCP server github 已上线", wake=False)
                agent.notify("部署已完成")

        sink = ListSink(on_event)
        sink.saw_end = False
        agent = self.build([text_turn("做完了"), text_turn("收到")], sink=sink)
        agent.send("干活")
        notes = [
            m["content"] for m in agent.messages
            if m.get("role") == "user" and "system-reminder" in str(m.get("content"))
        ]
        self.assertEqual(len(notes), 2)
        self.assertTrue(any("MCP server github 已上线" in note for note in notes))
        self.assertEqual(agent.messages[-1]["content"], "收到")

    def test_toolbox_announcements_are_non_waking(self):
        """接线定性：工具箱那条轨道（MCP 上线公告）挂的必须是不唤醒的钩子。"""
        agent = self.build([])
        agent.toolbox.notify_hook("某 server 已上线", "mcp:github")
        with agent._notify_lock:
            self.assertEqual([wake for _, _, wake in agent._notify_queue], [False])

    def test_reset_clears_queue_and_reported_keys(self):
        """reset 后：旧通知不再送达，已报 key 也清零（同 key 可重新送达）。"""
        agent = self.build([tool_turn("read_file", {"path": "calc.py"}), text_turn("好")])
        agent.notify("旧会话的通知", key="k")
        agent.reset()
        agent.notify("新会话的通知", key="k")
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("干活")
        tool_msg = next(m for m in agent.messages if m.get("role") == "tool")
        self.assertNotIn("旧会话的通知", tool_msg["content"])
        self.assertIn("新会话的通知", tool_msg["content"])

    def test_blank_notification_ignored(self):
        agent = self.build([])
        agent.notify("   ")
        self.assertEqual(agent._drain_notifications(), [])


if __name__ == "__main__":
    unittest.main()
