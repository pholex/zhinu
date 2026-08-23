"""operator 消息：内核 role=user + 私有标记；Anthropic 支持型号翻成会话中 role=system
并按服务端位置约束放置，放不下折回 user；其余协议一个字节不差。"""

from __future__ import annotations

import contextlib
import io
import unittest

from xiaoyu import messages as msgs
from xiaoyu.agent import PLAN_MODE_LEAVE_NOTE
from xiaoyu.messages import supports_mid_system
from xiaoyu.agent import Agent
from xiaoyu.providers import Registry
from xiaoyu.responses import OPERATOR_KEY, Transport, strip_private
from xiaoyu.tools import Toolbox

from .test_agent_paths import AgentTestCase, FakeClient, chunk, usage_chunk

OPUS = "claude-opus-5"
SONNET = "claude-sonnet-5"


def op(text: str) -> dict:
    return {"role": "user", "content": text, OPERATOR_KEY: True}


def roles(request: dict) -> list[str]:
    return [m["role"] for m in request["messages"]]


class SupportTest(unittest.TestCase):
    def test_model_gate(self):
        for name in (OPUS, "claude-opus-4-8", "anthropic/claude-fable-5", "claude-mythos-5"):
            self.assertTrue(supports_mid_system(name), name)
        for name in (SONNET, "claude-opus-4-7", "claude-sonnet-4-6", "gpt-5.6"):
            self.assertFalse(supports_mid_system(name), name)


class PlacementTest(unittest.TestCase):
    def test_after_user_and_last_becomes_system(self):
        """user, operator（最后一条）：合法位置，直接成 system。"""
        request = msgs.to_request(OPUS, [{"role": "user", "content": "q"}, op("规则")], None, True, {})
        self.assertEqual(roles(request), ["user", "system"])
        self.assertEqual(request["messages"][1]["content"], "规则")

    def test_between_assistant_and_user_moves_after_user(self):
        """assistant, operator, user（两轮之间切档再提问）：挪到新提问之后。"""
        history = [
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            op("切到 plan"),
            {"role": "user", "content": "q2"},
        ]
        request = msgs.to_request(OPUS, history, None, True, {})
        self.assertEqual(roles(request), ["user", "assistant", "user", "system"])
        self.assertEqual(request["messages"][2]["content"][0]["text"], "q2")
        self.assertEqual(request["messages"][3]["content"], "切到 plan")

    def test_after_tool_result_before_assistant_is_system(self):
        """tool 结果（合并成 user）后、assistant 前：system 合法。"""
        history = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "function": {"name": "grep", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
            op("<system-reminder>\n通知\n</system-reminder>"),
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "next"},
        ]
        request = msgs.to_request(OPUS, history, None, True, {})
        self.assertEqual(roles(request), ["user", "assistant", "user", "system", "assistant", "user"])

    def test_after_assistant_with_no_following_user_folds_to_user(self):
        """assistant, operator（最后一条，比如 step 边界补投的通知）：前面不是
        user，放不下 → 折回 user，与旧行为相同。"""
        history = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
            op("通知"),
        ]
        request = msgs.to_request(OPUS, history, None, True, {})
        self.assertEqual(roles(request), ["user", "assistant", "user"])

    def test_first_message_folds(self):
        request = msgs.to_request(OPUS, [op("x"), {"role": "assistant", "content": "a"}], None, True, {})
        self.assertEqual(roles(request)[0], "user")

    def test_consecutive_operators_merge_into_one_system(self):
        history = [{"role": "user", "content": "q"}, op("甲"), op("乙")]
        request = msgs.to_request(OPUS, history, None, True, {})
        self.assertEqual(roles(request), ["user", "system"])
        self.assertEqual(request["messages"][1]["content"], "甲\n\n乙")

    def test_tail_cache_breakpoint_lands_on_user_before_trailing_system(self):
        request = msgs.to_request(OPUS, [{"role": "user", "content": "q"}, op("规则")], None, True, {})
        user_blocks = request["messages"][0]["content"]
        self.assertEqual(user_blocks[-1]["cache_control"], {"type": "ephemeral"})
        self.assertNotIn("cache_control", request["messages"][1])

    def test_unsupported_model_sends_user_byte_identical(self):
        """Sonnet 5 不认会话中 system：标记被忽略，输出与无标记完全一致。"""
        history = [{"role": "user", "content": "q"}, op("规则"), {"role": "assistant", "content": "a"},
                   {"role": "user", "content": "q2"}]
        plain = [dict(m) for m in history]
        plain[1] = {"role": "user", "content": "规则"}
        self.assertEqual(
            msgs.to_request(SONNET, history, None, True, {}),
            msgs.to_request(SONNET, plain, None, True, {}),
        )

    def test_top_level_system_untouched(self):
        request = msgs.to_request(
            OPUS, [{"role": "system", "content": "S"}, {"role": "user", "content": "q"}, op("r")],
            None, True, {},
        )
        self.assertEqual(request["system"][0]["text"], "S")
        self.assertEqual(roles(request), ["user", "system"])


class ChatPathTest(unittest.TestCase):
    def test_strip_private_drops_marker(self):
        cleaned = strip_private([op("x")])
        self.assertEqual(cleaned, [{"role": "user", "content": "x"}])


class AgentMarksTest(AgentTestCase):
    def test_plan_mode_notes_are_operator(self):
        agent = self.build([[chunk(content="ok"), usage_chunk(10, 2)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.set_mode("plan")
            agent.set_mode("default")
        leave = agent.messages[-1]
        self.assertEqual(leave["content"], PLAN_MODE_LEAVE_NOTE)
        self.assertTrue(leave.get(OPERATOR_KEY))
        self.assertEqual(leave["role"], "user")

    def test_user_prompt_and_steer_are_not_operator(self):
        agent = self.build([[chunk(content="ok"), usage_chunk(10, 2)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        users = [m for m in agent.messages if m["role"] == "user"]
        self.assertTrue(users)
        self.assertFalse(any(m.get(OPERATOR_KEY) for m in users))

    def test_marker_not_sent_on_chat_path(self):
        """真实装配链一律套 Transport（唯一出网口），私有键在那里被摘。"""
        client = FakeClient([[chunk(content="ok"), usage_chunk(10, 2)]])
        registry = Registry.for_client(Transport(client, (), (), None))
        agent = Agent(self.config, Toolbox(self.config), registry=registry)
        self.client = client
        with contextlib.redirect_stdout(io.StringIO()):
            agent.set_mode("plan")
            agent.send("hi")
        sent = self.client.chat.completions.calls[-1]["messages"]
        self.assertFalse(any(OPERATOR_KEY in m for m in sent))


if __name__ == "__main__":
    unittest.main()
