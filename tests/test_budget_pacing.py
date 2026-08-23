"""预算节奏：token 软预算（倒计时 + 到线前收尾 + Anthropic 原生 task_budget）与
轮数预算可申请延期（extend_turns 只在撞顶那一步可见，池子有上限）。"""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from xiaoyu import messages as msgs
from xiaoyu.agent import (
    BUDGET_WRAPUP_INSTRUCTION,
    EXTEND_TURNS_TOOL,
    TURN_EXTENSION_OFFER,
    WRAPUP_INSTRUCTION,
)
from xiaoyu.embedding import measured_send
from xiaoyu.messages import TASK_BUDGET_BETA, TASK_BUDGET_KEY, supports_task_budget
from xiaoyu.responses import OPERATOR_KEY

from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk
from .test_server_compaction import FakeAnthropicRouteClient

USER = [{"role": "user", "content": "hi"}]


def read_call(n: int) -> list:
    return [chunk(tool_calls=[call_fragment(0, f"c{n}", "read_file", '{"path": "calc.py"}')]),
            usage_chunk(1000, 50)]


def extend_call(turns: int, reason: str = "还差一步") -> list:
    args = json.dumps({"turns": turns, "reason": reason})
    return [chunk(tool_calls=[call_fragment(0, "ext", EXTEND_TURNS_TOOL, args)]), usage_chunk(1000, 20)]


def text(content: str, prompt: int = 1000) -> list:
    return [chunk(content=content), usage_chunk(prompt, 30)]


class TaskBudgetTranslationTest(unittest.TestCase):
    def test_total_only(self):
        request = msgs.to_request("m", USER, None, True, {TASK_BUDGET_KEY: {"total": 64_000}})
        self.assertEqual(request["output_config"]["task_budget"], {"type": "tokens", "total": 64_000})
        self.assertEqual(request["betas"], [TASK_BUDGET_BETA])
        self.assertNotIn(TASK_BUDGET_KEY, request)

    def test_remaining_passed_when_given(self):
        request = msgs.to_request(
            "m", USER, None, True, {TASK_BUDGET_KEY: {"total": 64_000, "remaining": 12_345}}
        )
        self.assertEqual(request["output_config"]["task_budget"]["remaining"], 12_345)

    def test_below_server_minimum_is_dropped(self):
        """官方下限 20k：更小的预算不发（发了是 400），本地倒计时照旧管用。"""
        request = msgs.to_request("m", USER, None, True, {TASK_BUDGET_KEY: {"total": 5_000}})
        self.assertNotIn("output_config", request)
        self.assertNotIn("betas", request)

    def test_model_gate(self):
        for name in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-7", "claude-fable-5"):
            self.assertTrue(supports_task_budget(name), name)
        for name in ("claude-sonnet-4-6", "claude-haiku-4-5", "gpt-5.6"):
            self.assertFalse(supports_task_budget(name), name)


class TurnExtensionTest(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config.max_iterations = 2
        self.config.turn_extension = 1.0  # 池子 = 2 轮

    def _send(self, agent):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send("干活")
        return buffer.getvalue()

    def test_model_requests_and_gets_extension_then_finishes(self):
        agent = self.build([read_call(0), read_call(1), extend_call(2), read_call(2), text("完成")])
        out = self._send(agent)
        self.assertIn("批准 2 轮", out)
        self.assertEqual(agent.last_assistant_text(), "完成")
        self.assertEqual(agent.last_stop, "done")
        #  邀约那一步只开 extend_turns 一个工具
        offer_call = self.client.completions.calls[2]
        self.assertEqual([t["function"]["name"] for t in offer_call["tools"]], [EXTEND_TURNS_TOOL])
        #  邀约文案走 operator 通道
        offers = [m for m in agent.messages if m.get("content") == TURN_EXTENSION_OFFER]
        self.assertTrue(offers and offers[0].get(OPERATOR_KEY))

    def test_grant_is_clamped_to_pool(self):
        agent = self.build([read_call(0), read_call(1), extend_call(50), read_call(2), text("完成")])
        out = self._send(agent)
        self.assertIn("申请追加 50 轮", out)
        self.assertIn("批准 2 轮", out)
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool" and "已批准" in m["content"]]
        self.assertIn("申请 50，剩余可追加 0", tool_msgs[0]["content"])

    def test_pool_exhausted_falls_back_to_wrapup(self):
        """池子用完再撞顶：不再邀约，直接要总结（带旧的收尾指令）。"""
        agent = self.build([
            read_call(0), read_call(1), extend_call(2),
            read_call(2), read_call(3),          # 追加的 2 轮也用光
            text("总结：做到一半"),               # 收尾
        ])
        out = self._send(agent)
        self.assertIn("已达到单轮工具调用上限 4", out)
        self.assertEqual(agent.last_stop, "turn_cap")
        self.assertTrue(any(m.get("content") == WRAPUP_INSTRUCTION for m in agent.messages))
        self.assertNotIn("tools", self.client.completions.calls[-1])

    def test_model_declines_extension_by_summarising(self):
        """邀约那步模型直接给正文 = 已收尾，不再追加收尾调用。"""
        agent = self.build([read_call(0), read_call(1), text("不用了，已完成")])
        self._send(agent)
        self.assertEqual(len(self.client.completions.calls), 3)
        self.assertEqual(agent.last_stop, "turn_cap")
        self.assertEqual(agent.last_assistant_text(), "不用了，已完成")

    def test_extension_disabled(self):
        self.config.turn_extension = 0
        agent = self.build([read_call(0), read_call(1), text("总结")])
        out = self._send(agent)
        self.assertIn("已达到单轮工具调用上限 2", out)
        self.assertNotIn("询问模型", out)


class TokenBudgetTest(AgentTestCase):
    def _send(self, agent, prompt="干活"):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send(prompt)
        return buffer.getvalue()

    def test_countdown_notices_once_per_threshold(self):
        self.config.budget_tokens = 10_000
        #  每次调用 prompt 3000：第 2 次后 60%（过 50%）、第 3 次后 90%（过 80%，随即到线收尾）
        agent = self.build([
            [chunk(tool_calls=[call_fragment(0, f"c{n}", "read_file", '{"path": "calc.py"}')]),
             usage_chunk(3000, 0)] for n in range(3)
        ] + [text("完成", prompt=100)])
        out = self._send(agent)
        notes = [m for m in agent.messages if m.get(OPERATOR_KEY) and "[预算]" in str(m.get("content"))]
        ratios = [n["content"].split("（")[1].split("%")[0] for n in notes]
        self.assertEqual(ratios, ["60", "90"])
        self.assertIn("预算 60%", out)
        self.assertIn("预算 90%", out)

    def test_exhaustion_wraps_up_before_hard_line(self):
        """还剩的钱不够再付一次上下文：停下来交代现场，而不是开工到一半被砍。"""
        self.config.budget_tokens = 7_000
        self.client_script = [
            [chunk(tool_calls=[call_fragment(0, "c0", "read_file", '{"path": "calc.py"}')]), usage_chunk(3000, 0)],
            [chunk(tool_calls=[call_fragment(0, "c1", "read_file", '{"path": "calc.py"}')]), usage_chunk(3000, 0)],
            text("已完成 A，剩 B", prompt=500),
        ]
        agent = self.build(self.client_script)
        out = self._send(agent)
        self.assertIn("token 预算即将用尽", out)
        self.assertEqual(agent.last_stop, "budget")
        self.assertTrue(any(m.get("content") == BUDGET_WRAPUP_INSTRUCTION for m in agent.messages))
        self.assertNotIn("tools", self.client.completions.calls[-1])

    def test_run_result_reports_stopped(self):
        self.config.turn_extension = 0
        self.config.max_iterations = 1
        agent = self.build([read_call(0), text("总结")])
        with contextlib.redirect_stdout(io.StringIO()):
            result = measured_send(agent, "干活")
        self.assertEqual(result.stopped, "turn_cap")

    def test_set_budget_resets_notices(self):
        agent = self.build([])
        agent._budget_notified.add(0.5)  # noqa: SLF001
        agent.set_budget_tokens(50_000)
        self.assertEqual(agent.config.budget_tokens, 50_000)
        self.assertEqual(agent._budget_notified, set())  # noqa: SLF001
        agent.set_budget_tokens(None)
        self.assertIsNone(agent.config.budget_tokens)


class NativeTaskBudgetTest(AgentTestCase):
    def build(self, script, **kwargs):
        from xiaoyu.agent import Agent
        from xiaoyu.providers import Registry
        from xiaoyu.tools import Toolbox

        client = FakeAnthropicRouteClient(script)
        self.client = client
        return Agent(self.config, Toolbox(self.config), registry=Registry.for_client(client), **kwargs)

    def test_hint_total_only_then_remaining_after_rewrite(self):
        self.config.model = "claude-opus-5"
        self.config.budget_tokens = 64_000
        agent = self.build([text("ok"), text("ok")])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertEqual(self.client.chat.completions.calls[-1][TASK_BUDGET_KEY], {"total": 64_000})
        #  历史被改写（压缩等）后补 remaining
        agent.history_version += 1
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        hint = self.client.chat.completions.calls[-1][TASK_BUDGET_KEY]
        self.assertEqual(hint["total"], 64_000)
        self.assertIn("remaining", hint)

    def test_no_budget_no_hint(self):
        self.config.model = "claude-opus-5"
        agent = self.build([text("ok")])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertNotIn(TASK_BUDGET_KEY, self.client.chat.completions.calls[-1])


if __name__ == "__main__":
    unittest.main()
