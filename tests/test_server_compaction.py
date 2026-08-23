"""服务端压缩透传（Anthropic compact_20260112）：内核出私有键，Transport 翻译，
compaction 块走 thinking 同一条管线回放；agent 侧用 floor 估算 + 本地兜底。"""

from __future__ import annotations

import contextlib
import io
import unittest
from types import SimpleNamespace
from typing import Any

from xiaoyu import messages as msgs
from xiaoyu.agent import SERVER_COMPACTION_FALLBACK, Agent
from xiaoyu.messages import COMPACTION_BETA, COMPACTION_KEY, supports_server_compaction
from xiaoyu.providers import Registry
from xiaoyu.responses import REASONING_KEY, Transport
from xiaoyu.tools import Toolbox

from .test_agent_paths import AgentTestCase, FakeClient, chunk, usage_chunk
from .test_messages import (
    FakeAnthropicClient,
    FakeInner,
    FakeMessagesAPI,
    block_start,
    block_stop,
    message_delta,
    message_start,
    message_stop,
    text_delta,
)

USER = [{"role": "user", "content": "hi"}]


def compaction_delta(index: int, content: str) -> SimpleNamespace:
    return SimpleNamespace(
        type="content_block_delta",
        index=index,
        delta=SimpleNamespace(type="compaction_delta", content=content),
    )


class SupportTest(unittest.TestCase):
    def test_listed_models_and_unknown(self):
        for name in ("claude-opus-5", "claude-sonnet-4-6", "anthropic/claude-fable-5",
                     "claude-opus-4-7-20260101", "claude-mythos-5"):
            self.assertTrue(supports_server_compaction(name), name)
        for name in ("claude-haiku-4-5", "claude-sonnet-4-5", "gpt-5.6", ""):
            self.assertFalse(supports_server_compaction(name), name)


class RequestTest(unittest.TestCase):
    def test_key_becomes_context_management_and_beta(self):
        request = msgs.to_request("m", USER, None, True, {COMPACTION_KEY: {"trigger": 120_000}})
        self.assertEqual(request["betas"], [COMPACTION_BETA])
        self.assertEqual(
            request["context_management"],
            {"edits": [{"type": "compact_20260112",
                        "trigger": {"type": "input_tokens", "value": 120_000}}]},
        )
        self.assertNotIn(COMPACTION_KEY, request)

    def test_trigger_clamped_to_server_minimum(self):
        """低于 50k 是硬 400：本地算出来的小阈值（小窗口模型）要抬到下限。"""
        request = msgs.to_request("m", USER, None, True, {COMPACTION_KEY: {"trigger": 20_000}})
        self.assertEqual(
            request["context_management"]["edits"][0]["trigger"]["value"], 50_000
        )

    def test_instructions_passed_when_given(self):
        request = msgs.to_request(
            "m", USER, None, True, {COMPACTION_KEY: {"trigger": 60_000, "instructions": "留下 TODO"}}
        )
        self.assertEqual(request["context_management"]["edits"][0]["instructions"], "留下 TODO")

    def test_without_key_nothing_added(self):
        request = msgs.to_request("m", USER, None, True, {})
        self.assertNotIn("betas", request)
        self.assertNotIn("context_management", request)


class StreamTest(unittest.TestCase):
    def test_compaction_block_becomes_reasoning_item(self):
        events = [
            message_start(input_tokens=23_000),
            block_start(0, "compaction"),
            compaction_delta(0, "摘要正文"),
            block_stop(0),
            block_start(1, "text"),
            text_delta("继续", index=1),
            block_stop(1),
            message_delta(stop_reason="end_turn", output_tokens=9),
            message_stop(),
        ]
        chunks = list(msgs.stream_chunks(iter(events)))
        items = [item for c in chunks for item in (c.reasoning or [])]
        self.assertEqual(items, [{"type": "compaction", "content": "摘要正文"}])
        text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
        self.assertEqual(text, "继续")


class ReplayTest(unittest.TestCase):
    HISTORY = [
        {"role": "user", "content": "旧问题"},
        {
            "role": "assistant",
            "content": "答",
            REASONING_KEY: {
                "model": "claude-opus-5",
                "provider": "anthropic",
                "items": [
                    {"type": "compaction", "content": "摘要"},
                    {"type": "thinking", "thinking": "t", "signature": "S"},
                ],
            },
        },
        {"role": "user", "content": "新问题"},
    ]

    def test_same_model_replays_everything_compaction_first(self):
        request = msgs.to_request("claude-opus-5", self.HISTORY, None, True, {}, "anthropic")
        blocks = request["messages"][1]["content"]
        self.assertEqual([b["type"] for b in blocks], ["compaction", "thinking", "text"])

    def test_model_switch_keeps_compaction_drops_thinking(self):
        """compaction 不绑型号（官方），thinking 绑：同家换模型只回放前者。"""
        request = msgs.to_request("claude-sonnet-5", self.HISTORY, None, True, {}, "anthropic")
        blocks = request["messages"][1]["content"]
        self.assertEqual([b["type"] for b in blocks], ["compaction", "text"])

    def test_provider_switch_drops_compaction(self):
        request = msgs.to_request("claude-opus-5", self.HISTORY, None, True, {}, "gateway")
        blocks = request["messages"][1]["content"]
        self.assertEqual([b["type"] for b in blocks], ["text"])


class DispatchTest(unittest.TestCase):
    def _client(self, beta_api: FakeMessagesAPI, plain_api: FakeMessagesAPI):
        client = FakeAnthropicClient(plain_api)
        client.beta = SimpleNamespace(messages=beta_api)
        return client

    def test_betas_route_to_beta_namespace(self):
        beta = FakeMessagesAPI(events=[message_start(1), message_delta("end_turn", 1), message_stop()])
        plain = FakeMessagesAPI(events=[message_start(1), message_delta("end_turn", 1), message_stop()])
        transport = Transport(FakeInner(), (), ("*",), lambda: self._client(beta, plain))
        list(transport.chat.completions.create(
            model="claude-opus-5", messages=USER, stream=True,
            **{COMPACTION_KEY: {"trigger": 60_000}},
        ))
        self.assertEqual(len(beta.calls), 1)
        self.assertEqual(plain.calls, [])
        self.assertIn("context_management", beta.calls[0])
        # 没有私有键 → 正式面，一个字节不差
        list(transport.chat.completions.create(model="claude-opus-5", messages=USER, stream=True))
        self.assertEqual(len(plain.calls), 1)
        self.assertNotIn("betas", plain.calls[0])

    def test_chat_path_strips_private_key(self):
        """私有键只有 Anthropic 一路认；漏到 chat 端点是上游 400。"""
        client = FakeClient([[chunk(content="ok"), usage_chunk(10, 2)]])
        transport = Transport(client, (), (), None)
        list(transport.chat.completions.create(
            model="any-model", messages=USER, stream=True, **{COMPACTION_KEY: {"trigger": 1}}
        ))
        self.assertNotIn(COMPACTION_KEY, client.chat.completions.calls[0])


class FakeAnthropicRouteClient(FakeClient):
    """chat 形状的假 client，但自报说 Anthropic 协议——agent 侧只看这个判定。"""

    def protocol_for(self, model: str) -> str:
        return "anthropic"


class AgentSideTest(AgentTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.config.model = "claude-opus-5"
        self.config.context_limit_override = 200_000

    def build(self, script: list, **kwargs) -> Agent:
        client = FakeAnthropicRouteClient(script)
        agent = Agent(self.config, Toolbox(self.config), registry=Registry.for_client(client), **kwargs)
        self.client = client
        return agent

    def test_request_carries_trigger_at_compact_at(self):
        agent = self.build([[chunk(content="ok"), usage_chunk(10, 2)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        sent = self.client.chat.completions.calls[-1]
        self.assertEqual(sent[COMPACTION_KEY], {"trigger": int(0.7 * 200_000)})

    def test_switch_off_removes_key(self):
        self.config.server_compaction = False
        agent = self.build([[chunk(content="ok"), usage_chunk(10, 2)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertNotIn(COMPACTION_KEY, self.client.chat.completions.calls[-1])

    def test_unsupported_model_gets_no_key(self):
        self.config.model = "claude-haiku-4-5"
        agent = self.build([[chunk(content="ok"), usage_chunk(10, 2)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertNotIn(COMPACTION_KEY, self.client.chat.completions.calls[-1])

    def test_compaction_arrival_clears_anchor_and_floors_estimate(self):
        agent = self.build([[chunk(content="ok"), usage_chunk(150_000, 2)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertEqual(agent._anchor, (150_000, 2))  # noqa: SLF001
        #  模拟服务端在上一轮返回了 compaction 块（流解析已单测，这里直接落消息）
        agent.messages.append({"role": "user", "content": "x" * 40_000})
        agent.messages.append({
            "role": "assistant", "content": "继续",
            REASONING_KEY: {"model": "claude-opus-5", "provider": "gateway",
                            "items": [{"type": "compaction", "content": "摘要"}]},
        })
        agent._anchor = None  # noqa: SLF001 —— 到达处的逻辑（见 _stream_once）
        self.assertEqual(agent._compaction_floor(), len(agent.messages) - 1)  # noqa: SLF001
        #  floor 之前那 4 万字符不计入
        self.assertLess(agent.context_tokens(), 5_000)

    def test_local_compaction_defers_until_fallback_line(self):
        agent = self.build([[chunk(content="ok"), usage_chunk(10, 2)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        #  估算落在 compact_at 与兜底线之间：服务端在岗，本地不动
        agent._anchor = (int(200_000 * 0.8), len(agent.messages))  # noqa: SLF001
        self.assertIsNone(agent.maybe_compact())
        #  涨过兜底线：本地接管（走到 microcompact/摘要路径——这里只验证不再早退）
        agent._anchor = (int(200_000 * SERVER_COMPACTION_FALLBACK) + 1, len(agent.messages))  # noqa: SLF001
        with contextlib.redirect_stdout(io.StringIO()):
            note = agent.maybe_compact()
        self.assertIsNotNone(note)


if __name__ == "__main__":
    unittest.main()
