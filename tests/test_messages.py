"""Anthropic Messages 协议翻译层：请求形态、事件流、鸭子型 client。不打网络。

与 test_responses.py 同一纪律：每条用例都拿真实观测/官方文档确认过的形状写死
——事件名、字段名（content_block / input_json_delta / cache_read_input_tokens）
一旦变，这里先红。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from xiaoyu import messages as msgs
from xiaoyu import responses
from xiaoyu.responses import ANTHROPIC, CHAT, REASONING_KEY, RESPONSES, Transport


def event(kind: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


def message_start(
    input_tokens: int = 0, cache_read: int = 0, cache_creation: int = 0
) -> SimpleNamespace:
    return event(
        "message_start",
        message=SimpleNamespace(
            usage=SimpleNamespace(
                input_tokens=input_tokens,
                cache_read_input_tokens=cache_read,
                cache_creation_input_tokens=cache_creation,
            )
        ),
    )


def block_start(index: int, block_type: str, **fields: Any) -> SimpleNamespace:
    return event(
        "content_block_start",
        index=index,
        content_block=SimpleNamespace(type=block_type, **fields),
    )


def text_delta(text: str, index: int = 0) -> SimpleNamespace:
    return event(
        "content_block_delta", index=index, delta=SimpleNamespace(type="text_delta", text=text)
    )


def json_delta(index: int, partial: str) -> SimpleNamespace:
    return event(
        "content_block_delta",
        index=index,
        delta=SimpleNamespace(type="input_json_delta", partial_json=partial),
    )


def thinking_delta(index: int, text: str) -> SimpleNamespace:
    return event(
        "content_block_delta",
        index=index,
        delta=SimpleNamespace(type="thinking_delta", thinking=text),
    )


def signature_delta(index: int, signature: str) -> SimpleNamespace:
    return event(
        "content_block_delta",
        index=index,
        delta=SimpleNamespace(type="signature_delta", signature=signature),
    )


def block_stop(index: int) -> SimpleNamespace:
    return event("content_block_stop", index=index)


def message_delta(
    stop_reason: str | None = None, output_tokens: int = 0, **details: Any
) -> SimpleNamespace:
    return event(
        "message_delta",
        delta=SimpleNamespace(
            stop_reason=stop_reason,
            stop_details=SimpleNamespace(**details) if details else None,
        ),
        usage=SimpleNamespace(output_tokens=output_tokens),
    )


def message_stop() -> SimpleNamespace:
    return event("message_stop")


TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}

THINKING_ITEM = {"type": "thinking", "thinking": "先看目录", "signature": "SIG"}


class FakeMessagesAPI:
    """录下 messages.create 收到的参数，回放预设的事件流/响应。"""

    def __init__(self, events: list[Any] | None = None, result: Any = None) -> None:
        self._events = events or []
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return iter(self._events) if kwargs.get("stream") else self._result


class FakeAnthropicClient:
    def __init__(self, api: FakeMessagesAPI) -> None:
        self.messages = api


class FakeInner:
    """被包的 OpenAI client 桩：anthropic 分支不该碰它，碰了当场看得见。"""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._explode))
        self.marker = "inner"

    @staticmethod
    def _explode(**kwargs: Any) -> None:
        raise AssertionError("anthropic 协议的请求漏进了 chat 端点")

    def with_options(self, **kwargs: Any) -> "FakeInner":
        clone = FakeInner()
        clone.marker = f"inner+{kwargs}"
        return clone


def anthropic_transport(api: FakeMessagesAPI) -> tuple[Transport, list[int]]:
    """返回 (transport, 工厂调用计数)：计数用列表暴露给断言。"""
    built: list[int] = []

    def factory() -> FakeAnthropicClient:
        built.append(1)
        return FakeAnthropicClient(api)

    return Transport(FakeInner(), (), (responses.WILDCARD,), factory), built


class TestRequestTranslation(unittest.TestCase):
    def test_system_is_hoisted_with_cache_control(self) -> None:
        """system 提升为顶层参数并带缓存断点 #1；消息流里不许再出现 system。"""
        request = msgs.to_request(
            "claude-opus-5",
            [{"role": "system", "content": "你是小羽"}, {"role": "user", "content": "hi"}],
            None,
            False,
            {},
        )
        self.assertEqual(
            request["system"],
            [{"type": "text", "text": "你是小羽", "cache_control": {"type": "ephemeral"}}],
        )
        self.assertEqual([m["role"] for m in request["messages"]], ["user"])

    def test_stray_system_messages_hoist_and_concatenate(self) -> None:
        """内核只在 messages[0] 产生 system，但混进来的也要收齐拼接（防御）。"""
        request = msgs.to_request(
            "m",
            [
                {"role": "system", "content": "A"},
                {"role": "user", "content": "hi"},
                {"role": "system", "content": "B"},
            ],
            None,
            False,
            {},
        )
        self.assertEqual(request["system"][0]["text"], "A\n\nB")

    def test_tool_calls_become_tool_use_blocks(self) -> None:
        """arguments 是 JSON 字符串，Messages 的 input 要 dict——必须解析。"""
        request = msgs.to_request(
            "m",
            [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                        }
                    ],
                },
                {"role": "user", "content": "继续"},
            ],
            None,
            False,
            {},
        )
        self.assertEqual(
            request["messages"][0]["content"],
            [{"type": "tool_use", "id": "call_1", "name": "read_file", "input": {"path": "a"}}],
        )

    def test_malformed_arguments_become_empty_object(self) -> None:
        for raw in ("{oops", "", None, '["not","object"]'):
            with self.subTest(raw=raw):
                self.assertEqual(msgs._load_arguments(raw), {})

    def test_consecutive_tool_messages_coalesce_into_one_user_message(self) -> None:
        """同一轮的所有 tool_result 必须并入同一条 user 消息——分开发是硬 400。"""
        request = msgs.to_request(
            "m",
            [
                {"role": "tool", "tool_call_id": "c1", "content": "结果一"},
                {"role": "tool", "tool_call_id": "c2", "content": "结果二"},
                {"role": "user", "content": "然后呢"},
            ],
            None,
            False,
            {},
        )
        first = request["messages"][0]
        self.assertEqual(first["role"], "user")
        self.assertEqual(
            [(b["type"], b["tool_use_id"], b["content"]) for b in first["content"]],
            [("tool_result", "c1", "结果一"), ("tool_result", "c2", "结果二")],
        )
        #  隔着别的消息的 tool 不能被并进来
        self.assertEqual(len(request["messages"]), 2)

    def test_assistant_text_and_tool_calls_order(self) -> None:
        """text 在 tool_use 之前（与 chat 侧"先说话后调工具"的语义一致）。"""
        request = msgs.to_request(
            "m",
            [
                {
                    "role": "assistant",
                    "content": "我看一下",
                    "tool_calls": [{"id": "c1", "function": {"name": "ls", "arguments": "{}"}}],
                },
                {"role": "user", "content": "好"},
            ],
            None,
            False,
            {},
        )
        self.assertEqual(
            [b["type"] for b in request["messages"][0]["content"]], ["text", "tool_use"]
        )

    def test_whitespace_only_assistant_message_is_dropped(self) -> None:
        """Anthropic 拒空/纯空白 text block；翻空了的消息整条丢（相邻同角色合法）。"""
        request = msgs.to_request(
            "m",
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "  \n"},
                {"role": "user", "content": "again"},
            ],
            None,
            False,
            {},
        )
        self.assertEqual([m["role"] for m in request["messages"]], ["user", "user"])

    def test_user_string_content_passes_through_unwrapped(self) -> None:
        """非尾部的 user 字符串直通，不无谓展开成 blocks。"""
        request = msgs.to_request(
            "m",
            [{"role": "user", "content": "hi"}, {"role": "user", "content": "尾巴"}],
            None,
            False,
            {},
        )
        self.assertEqual(request["messages"][0]["content"], "hi")

    def test_data_url_image_becomes_base64_source(self) -> None:
        request = msgs.to_request(
            "m",
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,AAAA"},
                        },
                    ],
                },
                {"role": "user", "content": "尾"},
            ],
            None,
            False,
            {},
        )
        self.assertEqual(
            request["messages"][0]["content"][1],
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
            },
        )

    def test_external_image_url_becomes_url_source(self) -> None:
        """外链（以及缓存文件丢失时残留的引用）走 url source，让上游给可读报错。"""
        block = msgs._image_block("https://example.com/a.png")
        self.assertEqual(block["source"], {"type": "url", "url": "https://example.com/a.png"})

    def test_last_message_gets_second_cache_breakpoint(self) -> None:
        """尾消息最后一个 block 打断点 #2：多轮增量缓存靠它逐轮后移。"""
        request = msgs.to_request(
            "m", [{"role": "user", "content": "hi"}], None, False, {}
        )
        self.assertEqual(
            request["messages"][-1]["content"],
            [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}],
        )

    def test_tail_cache_skips_assistant_final_message(self) -> None:
        """assistant 尾消息（内核不会产生，防御回放场景）不打断点——它的 blocks
        里可能有必须字节一致回放的 thinking dict。"""
        converted = [{"role": "assistant", "content": [dict(THINKING_ITEM)]}]
        msgs._apply_tail_cache(converted)
        self.assertNotIn("cache_control", converted[0]["content"][0])

    def test_tools_flatten_to_input_schema(self) -> None:
        self.assertEqual(
            msgs.to_tools([TOOL]),
            [
                {
                    "name": "read_file",
                    "description": "读文件",
                    "input_schema": TOOL["function"]["parameters"],
                }
            ],
        )

    def test_max_tokens_defaults_differ_by_stream_and_caller_wins(self) -> None:
        user = [{"role": "user", "content": "hi"}]
        self.assertEqual(msgs.to_request("m", user, None, True, {})["max_tokens"], 64_000)
        self.assertEqual(msgs.to_request("m", user, None, False, {})["max_tokens"], 16_000)
        self.assertEqual(
            msgs.to_request("m", user, None, True, {"max_tokens": 123})["max_tokens"], 123
        )
        self.assertEqual(
            msgs.to_request("m", user, None, True, {"max_completion_tokens": 45})["max_tokens"],
            45,
        )

    def test_sampling_params_are_dropped_but_unknown_ones_pass(self) -> None:
        """采样参数是已知必炸（Claude 5 线硬 400），丢；未知参数照旧透传让上游炸。"""
        request = msgs.to_request(
            "m",
            [{"role": "user", "content": "hi"}],
            None,
            False,
            {"temperature": 0.2, "top_p": 0.9, "stream_options": {}, "metadata": {"k": "v"}},
        )
        for key in ("temperature", "top_p", "stream_options"):
            self.assertNotIn(key, request)
        self.assertEqual(request["metadata"], {"k": "v"})


class TestStreamTranslation(unittest.TestCase):
    def collect(self, events: list[Any]) -> tuple[str, dict[int, dict[str, Any]], Any]:
        """按 agent._consume_stream 的原样攒法消费，保证断言的是内核真实读法。"""
        text: list[str] = []
        pending: dict[int, dict[str, Any]] = {}
        seen_usage = None
        for chunk in msgs.stream_chunks(iter(events)):
            if chunk.usage:
                seen_usage = chunk.usage
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                text.append(delta.content)
            for fragment in delta.tool_calls or []:
                slot = pending.setdefault(
                    fragment.index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if fragment.id:
                    slot["id"] = fragment.id
                if fragment.function is None:
                    continue
                if fragment.function.name and not slot["function"]["name"]:
                    slot["function"]["name"] = fragment.function.name
                if fragment.function.arguments:
                    slot["function"]["arguments"] += fragment.function.arguments
        return "".join(text), pending, seen_usage

    def test_text_deltas_concatenate(self) -> None:
        text, pending, _ = self.collect(
            [block_start(0, "text"), text_delta("你"), text_delta("好"), message_stop()]
        )
        self.assertEqual((text, pending), ("你好", {}))

    def test_tool_use_assembles_from_start_and_json_deltas(self) -> None:
        """id/name 随 start 一次给全、input 按 partial_json 分片累加。"""
        _, pending, _ = self.collect(
            [
                block_start(1, "tool_use", id="toolu_x", name="read_file"),
                json_delta(1, '{"pa'),
                json_delta(1, 'th":"a"}'),
                block_stop(1),
                message_stop(),
            ]
        )
        self.assertEqual(
            pending,
            {
                1: {
                    "id": "toolu_x",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                }
            },
        )

    def test_parallel_tool_use_keeps_block_index_as_slot(self) -> None:
        _, pending, _ = self.collect(
            [
                block_start(1, "tool_use", id="t1", name="a"),
                block_start(2, "tool_use", id="t2", name="b"),
                json_delta(2, "{}"),
                json_delta(1, "{}"),
                block_stop(1),
                block_stop(2),
                message_stop(),
            ]
        )
        self.assertEqual([pending[i]["id"] for i in sorted(pending)], ["t1", "t2"])

    def test_empty_input_tool_use_backfills_empty_object(self) -> None:
        """无参调用可能一个 input_json_delta 都不发：不补 "{}" 内核会攒出空串。"""
        _, pending, _ = self.collect(
            [
                block_start(0, "tool_use", id="t1", name="ls"),
                block_stop(0),
                message_stop(),
            ]
        )
        self.assertEqual(pending[0]["function"]["arguments"], "{}")

    def test_usage_sums_input_and_cache_tokens(self) -> None:
        """prompt_tokens = input + cache_read + cache_creation：三个字段合起来
        才是真实 prompt 大小，内核的上下文锚点（agent._anchor）靠它。"""
        _, _, usage = self.collect(
            [
                message_start(input_tokens=100, cache_read=900, cache_creation=50),
                message_delta(stop_reason="end_turn", output_tokens=7),
                message_stop(),
            ]
        )
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens), (1050, 7))

    def test_final_usage_chunk_carries_no_choices(self) -> None:
        chunks = list(msgs.stream_chunks(iter([message_stop()])))
        self.assertEqual([chunk.choices for chunk in chunks], [[]])

    def test_thinking_block_assembles_at_block_stop(self) -> None:
        chunks = list(
            msgs.stream_chunks(
                iter(
                    [
                        block_start(0, "thinking", thinking="", signature=""),
                        thinking_delta(0, "先看"),
                        thinking_delta(0, "目录"),
                        signature_delta(0, "SIG"),
                        block_stop(0),
                        message_stop(),
                    ]
                )
            )
        )
        self.assertEqual(
            [c.reasoning for c in chunks if c.reasoning],
            [[{"type": "thinking", "thinking": "先看目录", "signature": "SIG"}]],
        )

    def test_omitted_display_thinking_is_still_captured(self) -> None:
        """display 默认 omitted：thinking 文本为空但 signature 仍在——照样攒块
        回放，不能按"有无文本"过滤（过滤掉 = 回传功能静默失效）。"""
        chunks = list(
            msgs.stream_chunks(
                iter(
                    [
                        block_start(0, "thinking", thinking="", signature=""),
                        signature_delta(0, "SIG"),
                        block_stop(0),
                        message_stop(),
                    ]
                )
            )
        )
        self.assertEqual(
            [c.reasoning for c in chunks if c.reasoning],
            [[{"type": "thinking", "thinking": "", "signature": "SIG"}]],
        )

    def test_redacted_thinking_is_captured_for_replay(self) -> None:
        chunks = list(
            msgs.stream_chunks(
                iter(
                    [
                        block_start(0, "redacted_thinking", data="ENC"),
                        block_stop(0),
                        message_stop(),
                    ]
                )
            )
        )
        self.assertEqual(
            [c.reasoning for c in chunks if c.reasoning],
            [[{"type": "redacted_thinking", "data": "ENC"}]],
        )

    def test_refusal_raises_with_detail(self) -> None:
        """安全分类器拒绝：HTTP 200 但没有可用内容，必须抛出去（fatal）。"""
        with self.assertRaises(RuntimeError) as ctx:
            list(
                msgs.stream_chunks(
                    iter([message_delta(stop_reason="refusal", explanation="分类器命中")])
                )
            )
        self.assertIn("分类器命中", str(ctx.exception))

    def test_max_tokens_keeps_partial_instead_of_raising(self) -> None:
        """截断不抛：chat 侧 finish_reason=length 也是照常返回半截正文。"""
        text, _, usage = self.collect(
            [
                message_start(input_tokens=3),
                block_start(0, "text"),
                text_delta("半截"),
                message_delta(stop_reason="max_tokens", output_tokens=4),
                message_stop(),
            ]
        )
        self.assertEqual(text, "半截")
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens), (3, 4))

    def test_ping_and_unknown_events_are_ignored(self) -> None:
        text, pending, _ = self.collect(
            [
                event("ping"),
                event("某个未来新增的事件"),
                block_start(0, "text"),
                text_delta("答案"),
                message_stop(),
            ]
        )
        self.assertEqual((text, pending), ("答案", {}))


class TestReasoningReplay(unittest.TestCase):
    def test_thinking_replays_first_and_byte_identical(self) -> None:
        """thinking 必须排在它引出的 text/tool_use 之前，且原样引用存储的 dict
        ——signature 校验容不得重建。"""
        stored = dict(THINKING_ITEM)
        request = msgs.to_request(
            "claude-opus-5",
            [
                {
                    "role": "assistant",
                    "content": "算好了",
                    "tool_calls": [{"id": "c1", "function": {"name": "calc", "arguments": "{}"}}],
                    REASONING_KEY: {"model": "claude-opus-5", "items": [stored]},
                },
                {"role": "user", "content": "好"},
            ],
            None,
            False,
            {},
        )
        blocks = request["messages"][0]["content"]
        self.assertEqual([b["type"] for b in blocks], ["thinking", "text", "tool_use"])
        self.assertIs(blocks[0], stored)

    def test_thinking_dropped_when_model_changed(self) -> None:
        """signature 是模型侧私有状态：/model 切走之后不能再塞回去。"""
        request = msgs.to_request(
            "claude-sonnet-5",
            [
                {
                    "role": "assistant",
                    "content": "算好了",
                    REASONING_KEY: {"model": "claude-opus-5", "items": [dict(THINKING_ITEM)]},
                },
                {"role": "user", "content": "好"},
            ],
            None,
            False,
            {},
        )
        self.assertEqual(
            request["messages"][0]["content"], [{"type": "text", "text": "算好了"}]
        )

    def test_thinking_dropped_when_provider_changed(self) -> None:
        """降级链让同名模型跑在两家上（直连 vs 网关）：signature 是服务侧
        私有状态，跨家回放过不了校验。provider 对不上也要丢。"""
        message = {
            "role": "assistant",
            "content": "算好了",
            REASONING_KEY: {
                "model": "claude-opus-5",
                "provider": "anthropic",
                "items": [dict(THINKING_ITEM)],
            },
        }
        #  同家：照常回放
        request = msgs.to_request(
            "claude-opus-5", [dict(message)], None, False, {}, provider="anthropic"
        )
        self.assertEqual(request["messages"][0]["content"][0]["type"], "thinking")
        #  跨家（同名模型走网关）：整段跳过
        request = msgs.to_request(
            "claude-opus-5", [dict(message)], None, False, {}, provider="gateway"
        )
        self.assertEqual(
            request["messages"][0]["content"], [{"type": "text", "text": "算好了"}]
        )

    def test_legacy_reasoning_without_provider_tag_is_dropped_on_real_route(self) -> None:
        """旧会话存量 reasoning 没有 provider 标签：真实路由（provider 非空）上
        按不匹配处理——丢 reasoning 永远无害，跨家回放才是事故。"""
        request = msgs.to_request(
            "claude-opus-5",
            [
                {
                    "role": "assistant",
                    "content": "算好了",
                    REASONING_KEY: {"model": "claude-opus-5", "items": [dict(THINKING_ITEM)]},
                }
            ],
            None,
            False,
            {},
            provider="anthropic",
        )
        self.assertEqual(
            request["messages"][0]["content"], [{"type": "text", "text": "算好了"}]
        )


class TestDuckClient(unittest.TestCase):
    def test_stream_call_translates_request_and_response(self) -> None:
        api = FakeMessagesAPI(
            events=[
                message_start(input_tokens=5),
                block_start(0, "text"),
                text_delta("好"),
                message_delta(stop_reason="end_turn", output_tokens=6),
                message_stop(),
            ]
        )
        client, _ = anthropic_transport(api)
        chunks = list(
            client.chat.completions.create(
                model="claude-opus-5",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                stream_options={"include_usage": True},
                tools=[TOOL],
            )
        )
        sent = api.calls[0]
        self.assertEqual(sent["model"], "claude-opus-5")
        self.assertIs(sent["stream"], True)
        self.assertEqual(sent["max_tokens"], 64_000)
        self.assertEqual(sent["tools"][0]["name"], "read_file")
        #  stream_options 是 chat 独有的，不许漏进 Messages 请求
        self.assertNotIn("stream_options", sent)
        self.assertEqual(chunks[0].choices[0].delta.content, "好")
        self.assertEqual(chunks[-1].usage.completion_tokens, 6)

    def test_non_stream_call_looks_like_a_completion(self) -> None:
        """摘要路径读的是 choices[0].message.content 与 usage。"""
        api = FakeMessagesAPI(
            result=SimpleNamespace(
                content=[SimpleNamespace(type="text", text="摘要正文")],
                usage=SimpleNamespace(
                    input_tokens=7,
                    output_tokens=8,
                    cache_read_input_tokens=2,
                    cache_creation_input_tokens=1,
                ),
            )
        )
        client, _ = anthropic_transport(api)
        result = client.chat.completions.create(
            model="claude-opus-5", messages=[{"role": "user", "content": "hi"}]
        )
        self.assertEqual(result.choices[0].message.content, "摘要正文")
        self.assertEqual((result.usage.prompt_tokens, result.usage.completion_tokens), (10, 8))
        self.assertNotIn("stream", api.calls[0])

    def test_reasoning_survives_the_transport_not_just_to_request(self) -> None:
        """回归：净化若跑在翻译之前，`_reasoning` 会被当私有键摘掉——回传当场
        失效且不报错。这条断言走完整条 create 路径（对齐 responses 的同名测试）。"""
        stored = dict(THINKING_ITEM)
        api = FakeMessagesAPI(events=[message_stop()])
        client, _ = anthropic_transport(api)
        list(
            client.chat.completions.create(
                model="claude-opus-5",
                messages=[
                    {
                        "role": "assistant",
                        "content": "算好了",
                        REASONING_KEY: {"model": "claude-opus-5", "items": [stored]},
                    },
                    {"role": "user", "content": "好"},
                ],
                stream=True,
            )
        )
        self.assertIs(api.calls[0]["messages"][0]["content"][0], stored)

    def test_anthropic_client_is_lazy_and_cached(self) -> None:
        """不碰 Claude 直连就不构造；碰了也只构造一次（连接池复用）。"""
        api = FakeMessagesAPI(events=[message_stop()], result=SimpleNamespace(content=[]))
        client, built = anthropic_transport(api)
        self.assertEqual(built, [])
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "a"}])
        client.chat.completions.create(model="m", messages=[{"role": "user", "content": "b"}])
        self.assertEqual(built, [1])

    def test_with_options_keeps_anthropic_protocol_and_factory(self) -> None:
        api = FakeMessagesAPI(result=SimpleNamespace(content=[]))
        client, built = anthropic_transport(api)
        copy = client.with_options(timeout=1.0)
        self.assertIsInstance(copy, Transport)
        self.assertEqual(copy.protocol_for("any"), ANTHROPIC)
        copy.chat.completions.create(model="m", messages=[{"role": "user", "content": "a"}])
        self.assertEqual(built, [1])

    def test_protocol_precedence_is_anthropic_over_responses_over_chat(self) -> None:
        inner = FakeInner()
        both = responses.wrap(inner, (responses.WILDCARD,), (responses.WILDCARD,), lambda: None)
        self.assertEqual(both.protocol_for("m"), ANTHROPIC)
        self.assertEqual(responses.wrap(inner, (responses.WILDCARD,)).protocol_for("m"), RESPONSES)
        self.assertEqual(responses.wrap(inner, ()).protocol_for("m"), CHAT)

    def test_client_factory_strips_v1_suffix(self) -> None:
        """Provider.base_url 是 .../v1（OpenAI 兼容面），anthropic SDK 自己拼
        /v1/messages——不剥会打到 /v1/v1/messages。只构造不发网络。"""
        made = msgs.client("https://api.anthropic.com/v1", "k", 30.0)
        self.assertEqual(str(made.base_url).rstrip("/"), "https://api.anthropic.com")
        self.assertEqual(made.max_retries, 0)


class TestCompletionShape(unittest.TestCase):
    def test_missing_usage_is_none(self) -> None:
        result = msgs.to_completion(SimpleNamespace(content=[], usage=None))
        self.assertIsNone(result.usage)
        self.assertEqual(result.choices[0].message.content, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
