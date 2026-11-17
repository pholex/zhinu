"""验收目标（/goal、--goal）：模型准备收尾时顶回去核对一次，每轮最多一次，走 operator 通道。"""

from __future__ import annotations

import contextlib
import io
import unittest

from xiaoyu.agent import GOAL_CHECK_NUDGE
from xiaoyu.cli import SLASH_COMMANDS, build_parser, handle_slash
from xiaoyu.responses import OPERATOR_KEY

from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk


def text(content: str) -> list:
    return [chunk(content=content), usage_chunk(100, 10)]


def read_call(n: int) -> list:
    return [chunk(tool_calls=[call_fragment(0, f"c{n}", "read_file", '{"path": "calc.py"}')]),
            usage_chunk(100, 10)]


class GoalCheckTest(AgentTestCase):
    def _send(self, agent, prompt="干活"):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send(prompt)
        return buffer.getvalue()

    @staticmethod
    def _nudges(agent) -> list[dict]:
        return [
            m for m in agent.messages
            if m.get(OPERATOR_KEY) and "收尾前先核对用户设定的验收目标" in str(m.get("content"))
        ]

    def test_nudged_once_before_finishing(self):
        agent = self.build([text("做完了"), text("核对过：测试全绿，目标达成")])
        agent.set_goal("tests 目录下全部测试通过")
        out = self._send(agent)
        nudges = self._nudges(agent)
        self.assertEqual(len(nudges), 1)
        expected = GOAL_CHECK_NUDGE.format(goal="tests 目录下全部测试通过")
        self.assertEqual(nudges[0]["content"], expected)
        #  合成文本：压缩时不会被当成用户原话
        self.assertIn(expected, agent.compactor.synthetic_user_texts)
        self.assertEqual(len(self.client.completions.calls), 2)
        self.assertEqual(agent.last_assistant_text(), "核对过：测试全绿，目标达成")
        self.assertIn("核对验收目标", out)

    def test_only_once_per_turn_but_again_next_turn(self):
        agent = self.build([text("完成"), text("已达成"), text("第二轮完成"), text("第二轮已达成")])
        agent.set_goal("目标")
        self._send(agent)
        self._send(agent, "继续")
        self.assertEqual(len(self._nudges(agent)), 2)
        self.assertEqual(len(self.client.completions.calls), 4)

    def test_model_keeps_working_after_nudge(self):
        """顶回去之后模型继续调用工具属正常路径：核对只发一次，不干扰续做。"""
        agent = self.build([text("差不多了"), read_call(0), text("补做完了，已达成")])
        agent.set_goal("目标")
        self._send(agent)
        self.assertEqual(len(self._nudges(agent)), 1)
        self.assertEqual(agent.last_assistant_text(), "补做完了，已达成")

    def test_no_goal_no_nudge(self):
        agent = self.build([text("完成")])
        self._send(agent)
        self.assertEqual(self._nudges(agent), [])
        self.assertEqual(len(self.client.completions.calls), 1)

    def test_cleared_goal_stops_nudging(self):
        agent = self.build([text("完成")])
        agent.set_goal("目标")
        agent.set_goal("")
        self._send(agent)
        self.assertEqual(self._nudges(agent), [])

    def test_content_filtered_reply_is_not_nudged(self):
        """被服务端内容过滤截断的回答不是模型自己的收尾，不顶。"""
        agent = self.build([text("部分回答")])
        agent.set_goal("目标")
        original = agent._stream_once

        def filtered(*args, **kwargs):
            message = original(*args, **kwargs)
            agent._content_filtered = True
            return message

        agent._stream_once = filtered
        self._send(agent)
        self.assertEqual(self._nudges(agent), [])


class GoalSlashTest(AgentTestCase):
    def run_slash(self, agent, line: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            handle_slash(agent, line)
        return buffer.getvalue()

    def test_set_show_clear(self):
        agent = self.build([])
        self.assertIn("未设验收目标", self.run_slash(agent, "/goal"))
        self.assertIn("验收目标已设为：全部 测试 通过", self.run_slash(agent, "/goal 全部 测试 通过"))
        self.assertEqual(agent.goal, "全部 测试 通过")
        self.assertIn("当前验收目标：全部 测试 通过", self.run_slash(agent, "/goal"))
        self.assertIn("验收目标已清除", self.run_slash(agent, "/goal clear"))
        self.assertEqual(agent.goal, "")

    def test_listed_in_slash_table(self):
        """补全菜单与 /help 都从 SLASH_COMMANDS 生成：漏登记就等于没有这条命令。"""
        self.assertIn("/goal", SLASH_COMMANDS)


class GoalFlagTest(unittest.TestCase):
    def test_flag_parsed(self):
        args = build_parser().parse_args(["--goal", "构建通过", "任务"])
        self.assertEqual(args.goal, "构建通过")

    def test_default_is_none(self):
        self.assertIsNone(build_parser().parse_args(["任务"]).goal)


if __name__ == "__main__":
    unittest.main()
