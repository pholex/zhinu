"""Responses 协议翻译层：请求形态、事件流、鸭子型 client。不打网络。

断言的对象是"两种 wire format 之间的翻译"，所以每条用例都拿真实观测到的形状
写死——事件名、字段名（output_index / call_id / input_tokens）一旦变，这里先红。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any

from xiaoyu import responses
from xiaoyu.responses import (
    REASONING_KEY,
    Transport,
    to_completion,
    to_input,
    to_request,
    to_tools,
)


def event(kind: str, **fields: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **fields)


def text_delta(text: str, index: int = 0) -> SimpleNamespace:
    return event("response.output_text.delta", delta=text, output_index=index)


def call_added(index: int, call_id: str, name: str) -> SimpleNamespace:
    return event(
        "response.output_item.added",
        output_index=index,
        item=SimpleNamespace(type="function_call", call_id=call_id, name=name),
    )


def args_delta(index: int, text: str) -> SimpleNamespace:
    return event(
        "response.function_call_arguments.delta", output_index=index, delta=text
    )


def completed(input_tokens: int = 0, output_tokens: int = 0) -> SimpleNamespace:
    return event(
        "response.completed",
        response=SimpleNamespace(
            usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
        ),
    )


TOOL = {
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读文件",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
}


class FakeResponses:
    """录下 responses.create 收到的参数，回放预设的事件流/响应。"""

    def __init__(self, events: list[Any] | None = None, result: Any = None) -> None:
        self._events = events or []
        self._result = result
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return iter(self._events) if kwargs.get("stream") else self._result


class FakeChatCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> str:
        self.calls.append(kwargs)
        return "chat-result"


class FakeClient:
    def __init__(self, responses_api: FakeResponses) -> None:
        self.responses = responses_api
        self.chat = SimpleNamespace(completions=FakeChatCompletions())
        self.marker = "inner"

    def with_options(self, **kwargs: Any) -> "FakeClient":
        clone = FakeClient(self.responses)
        clone.marker = f"inner+{kwargs}"
        return clone


def responses_transport(api: FakeResponses) -> Transport:
    return Transport(FakeClient(api), (responses.WILDCARD,))


class TestRequestTranslation(unittest.TestCase):
    def test_tool_calls_become_flat_items_paired_by_call_id(self) -> None:
        """assistant.tool_calls / role:tool 都要拍平成平级 item，靠 call_id 配对。"""
        messages = [
            {"role": "system", "content": "你是小羽"},
            {"role": "user", "content": "读文件"},
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
            {"role": "tool", "tool_call_id": "call_1", "content": "文件内容"},
        ]
        self.assertEqual(
            to_input(messages),
            [
                {"role": "system", "content": "你是小羽"},
                {"role": "user", "content": "读文件"},
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "read_file",
                    "arguments": '{"path":"a"}',
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "文件内容"},
            ],
        )

    def test_assistant_with_text_and_tool_calls_emits_both(self) -> None:
        """边说话边调工具：消息 item 在前，function_call 在后，顺序不能颠倒。"""
        items = to_input(
            [
                {
                    "role": "assistant",
                    "content": "我看一下",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "ls", "arguments": "{}"}}
                    ],
                }
            ]
        )
        self.assertEqual([item.get("role") or item["type"] for item in items],
                         ["assistant", "function_call"])

    def test_empty_content_assistant_emits_no_message_item(self) -> None:
        """只带 tool_calls 的 assistant 不发空消息——Responses 拒收空 content。"""
        items = to_input(
            [{"role": "assistant", "content": None,
              "tool_calls": [{"id": "c1", "function": {"name": "ls", "arguments": "{}"}}]}]
        )
        self.assertEqual([item["type"] for item in items], ["function_call"])

    def test_missing_arguments_defaults_to_empty_object(self) -> None:
        """arguments 为空串时补 "{}"：Responses 要求它是合法 JSON 串。"""
        items = to_input(
            [{"role": "assistant", "tool_calls": [{"id": "c1", "function": {"name": "ls"}}]}]
        )
        self.assertEqual(items[0]["arguments"], "{}")

    def test_tools_are_flattened_with_strict_off(self) -> None:
        """schema 拍平 + strict 显式关掉（Responses 侧默认 True，会整批拒收）。"""
        self.assertEqual(
            to_tools([TOOL]),
            [
                {
                    "type": "function",
                    "name": "read_file",
                    "description": "读文件",
                    "parameters": TOOL["function"]["parameters"],
                    "strict": False,
                }
            ],
        )

    def test_native_tools_pass_through(self) -> None:
        """已经是 Responses 形态的（web_search 等内置工具）原样透传。"""
        native = {"type": "web_search"}
        self.assertEqual(to_tools([native]), [native])

    def test_request_disables_store_and_renames_token_cap(self) -> None:
        """store=False 与 chat 行为对齐；max_tokens 系改名，其余参数原样透传。"""
        request = to_request(
            "gpt-5.6-sol",
            [{"role": "user", "content": "hi"}],
            [TOOL],
            {"max_completion_tokens": 16, "temperature": 0.2},
        )
        self.assertIs(request["store"], False)
        self.assertEqual(request["max_output_tokens"], 16)
        self.assertEqual(request["temperature"], 0.2)
        self.assertNotIn("max_completion_tokens", request)

    def test_no_tools_key_when_tools_absent(self) -> None:
        """收尾总结那类"不许干活"的调用不能带 tools 键。"""
        self.assertNotIn(
            "tools", to_request("m", [{"role": "user", "content": "hi"}], None, {})
        )


class TestStreamTranslation(unittest.TestCase):
    def collect(self, events: list[Any]) -> tuple[str, dict[int, dict[str, Any]], Any]:
        """按 agent._consume_stream 的原样攒法消费，保证断言的是内核真实读法。"""
        text: list[str] = []
        pending: dict[int, dict[str, Any]] = {}
        seen_usage = None
        for chunk in responses.stream_chunks(iter(events)):
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
        text, pending, _ = self.collect([text_delta("你"), text_delta("好"), completed()])
        self.assertEqual((text, pending), ("你好", {}))

    def test_function_call_assembles_from_fragments(self) -> None:
        """name 随 item 一次给全、arguments 分片累加——攒出来必须是完整 tool_call。"""
        _, pending, _ = self.collect(
            [
                call_added(0, "call_x", "read_file"),
                args_delta(0, '{"pa'),
                args_delta(0, 'th":"a"}'),
                completed(),
            ]
        )
        self.assertEqual(
            pending,
            {
                0: {
                    "id": "call_x",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": '{"path":"a"}'},
                }
            },
        )

    def test_parallel_calls_keep_output_index_as_slot(self) -> None:
        """output_index 直接当 tool_calls[].index：并行调用不能串槽。"""
        _, pending, _ = self.collect(
            [
                call_added(0, "c0", "a"),
                call_added(1, "c1", "b"),
                args_delta(1, "{}"),
                args_delta(0, "{}"),
                completed(),
            ]
        )
        self.assertEqual([pending[i]["id"] for i in sorted(pending)], ["c0", "c1"])

    def test_usage_field_names_are_translated(self) -> None:
        """input/output_tokens → prompt/completion_tokens：记账靠这一步。"""
        _, _, usage = self.collect([text_delta("x"), completed(11, 22)])
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens), (11, 22))

    def test_usage_chunk_carries_no_choices(self) -> None:
        """收尾 usage chunk 的 choices 必须为空——内核靠它跳过，形状同 chat 流末尾。"""
        chunks = list(responses.stream_chunks(iter([completed(1, 2)])))
        self.assertEqual([chunk.choices for chunk in chunks], [[]])

    def test_reasoning_and_unknown_events_are_ignored(self) -> None:
        """推理摘要等事件没有展示位，必须彻底忽略而不是混进正文。"""
        text, pending, _ = self.collect(
            [
                event("response.created"),
                event("response.reasoning_summary_text.delta", delta="想一想"),
                event("response.output_item.added",
                      output_index=0, item=SimpleNamespace(type="reasoning")),
                text_delta("答案"),
                event("response.output_item.done"),
                completed(),
            ]
        )
        self.assertEqual((text, pending), ("答案", {}))

    def test_failed_and_error_events_raise(self) -> None:
        """上游失败必须抛出去，交给 agent 那层的分类，不能静默收流。"""
        with self.assertRaises(RuntimeError) as failed:
            list(responses.stream_chunks(iter([
                event("response.failed",
                      response=SimpleNamespace(error=SimpleNamespace(message="内部错误"))),
            ])))
        self.assertIn("内部错误", str(failed.exception))
        with self.assertRaises(RuntimeError) as errored:
            list(responses.stream_chunks(iter([event("error", message="连接断了")])))
        self.assertIn("连接断了", str(errored.exception))

    def test_incomplete_keeps_partial_text_instead_of_raising(self) -> None:
        """截断/内容过滤不抛：chat 侧 finish_reason=length 也是照常返回半截正文。"""
        text, _, usage = self.collect([
            text_delta("半截"),
            event("response.incomplete",
                  response=SimpleNamespace(
                      usage=SimpleNamespace(input_tokens=3, output_tokens=4),
                      incomplete_details=SimpleNamespace(reason="max_output_tokens"))),
        ])
        self.assertEqual(text, "半截")
        self.assertEqual((usage.prompt_tokens, usage.completion_tokens), (3, 4))


class TestDuckClient(unittest.TestCase):
    def test_stream_call_translates_request_and_response(self) -> None:
        api = FakeResponses(events=[text_delta("好"), completed(5, 6)])
        client = responses_transport(api)
        chunks = list(
            client.chat.completions.create(
                model="gpt-5.6-sol",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
                stream_options={"include_usage": True},
                tools=[TOOL],
            )
        )
        sent = api.calls[0]
        self.assertEqual(sent["model"], "gpt-5.6-sol")
        self.assertEqual(sent["input"], [{"role": "user", "content": "hi"}])
        self.assertIs(sent["store"], False)
        self.assertIs(sent["stream"], True)
        self.assertEqual(sent["tools"][0]["name"], "read_file")
        #  stream_options 是 chat 独有的，不许漏进 Responses 请求
        self.assertNotIn("stream_options", sent)
        self.assertEqual(chunks[0].choices[0].delta.content, "好")

    def test_non_stream_call_looks_like_a_completion(self) -> None:
        """摘要路径读的是 choices[0].message.content 与 usage。"""
        api = FakeResponses(
            result=SimpleNamespace(
                output_text="摘要正文",
                usage=SimpleNamespace(input_tokens=7, output_tokens=8),
            )
        )
        client = responses_transport(api)
        result = client.chat.completions.create(
            model="m", messages=[{"role": "user", "content": "hi"}]
        )
        self.assertEqual(result.choices[0].message.content, "摘要正文")
        self.assertEqual(result.usage.prompt_tokens, 7)
        self.assertNotIn("stream", api.calls[0])

    def test_other_attributes_delegate_to_inner_client(self) -> None:
        """web_search 直接用 client.responses.create，这层不能挡住。"""
        api = FakeResponses()
        client = responses_transport(api)
        self.assertIs(client.responses, api)
        self.assertEqual(client.marker, "inner")

    def test_with_options_keeps_protocol(self) -> None:
        """带超时的副本仍须是包过的、且协议不变，否则会悄悄退回裸 client。"""
        client = responses_transport(FakeResponses()).with_options(timeout=1.0)
        self.assertIsInstance(client, Transport)
        self.assertEqual(client.protocol_for("any"), "responses")
        self.assertEqual(client.marker, "inner+{'timeout': 1.0}")

    def test_protocol_is_decided_per_model(self) -> None:
        """协议按型号定：同一家常常只有部分型号开了 Responses（deepseek 就是
        flash 通、pro 不通，而 pro 是默认主模型——按家切会当场切死）。"""
        inner = FakeClient(FakeResponses())
        partial = responses.wrap(inner, ("deepseek-v4-flash",))
        self.assertEqual(partial.protocol_for("deepseek-v4-flash"), "responses")
        self.assertEqual(partial.protocol_for("deepseek-v4-pro"), "chat")
        whole = responses.wrap(inner, (responses.WILDCARD,))
        self.assertEqual(whole.protocol_for("随便什么名字"), "responses")
        #  没声明 = 整家走 chat（净化仍然照做，见 TestChatPassthrough）
        self.assertEqual(responses.wrap(inner, ()).protocol_for("m"), "chat")

    def test_partial_provider_routes_each_model_to_its_own_protocol(self) -> None:
        """同一个 client 上两个型号各走各的路，不能串。"""
        inner = FakeClient(FakeResponses(events=[completed()]))
        client = responses.wrap(inner, ("deepseek-v4-flash",))
        list(client.chat.completions.create(
            model="deepseek-v4-flash", messages=[{"role": "user", "content": "hi"}], stream=True))
        client.chat.completions.create(
            model="deepseek-v4-pro", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual([c["model"] for c in inner.responses.calls], ["deepseek-v4-flash"])
        self.assertEqual(
            [c["model"] for c in inner.chat.completions.calls], ["deepseek-v4-pro"]
        )


class TestChatPassthrough(unittest.TestCase):
    """chat 协议也过 Transport：唯一的活儿是摘私有键，其余必须原样直发。"""

    def send(self, **kwargs: Any) -> dict[str, Any]:
        inner = FakeClient(FakeResponses())
        client = Transport(inner, responses.CHAT)
        client.chat.completions.create(**kwargs)
        return inner.chat.completions.calls[0]

    def test_request_is_forwarded_verbatim(self) -> None:
        sent = self.send(
            model="deepseek-v4-pro",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            stream_options={"include_usage": True},
            tools=[TOOL],
        )
        self.assertEqual(
            sent,
            {
                "model": "deepseek-v4-pro",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
                "tools": [TOOL],
            },
        )

    def test_absent_options_are_omitted_not_none(self) -> None:
        """显式 None 会被 SDK 当成"发个 null 上去"，有的端点因此 400。"""
        sent = self.send(model="m", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(sorted(sent), ["messages", "model"])

    def test_private_keys_never_reach_the_chat_endpoint(self) -> None:
        """`_reasoning` 是内核私有的。漏出去不报错——只是每轮白付一千字符的
        input token，所以这条断言是唯一能发现它的地方。"""
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok", REASONING_KEY: {"model": "m", "items": [{}]}},
        ]
        sent = self.send(model="deepseek-v4-pro", messages=history)
        self.assertEqual(
            sent["messages"],
            [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}],
        )
        #  原始历史不许被就地改写：内核还要拿它继续对话
        self.assertIn(REASONING_KEY, history[1])


class TestToolSignatures(unittest.TestCase):
    """Gemini thought_signature 的出网还原（restore_tool_extras）。

    纪律同 _reasoning：产出路由（provider+model 都对上）才还原；签名型号收到
    别家产的 tool_calls 历史时补占位签名（上游对当前轮缺签名的 function call
    直接 400）；并行调用只有第一路有签名，原样还原不多补。
    """

    SIG = {"google": {"thought_signature": "SIG"}}

    def history(self) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "f", "arguments": "{}"}},
                ],
                responses.TOOL_EXTRAS_KEY: {
                    "model": "gemini-3.7-flash",
                    "provider": "gemini",
                    "extras": {"a": self.SIG},
                },
            },
            {"role": "tool", "tool_call_id": "a", "content": "ok"},
            {"role": "tool", "tool_call_id": "b", "content": "ok"},
        ]

    def send(self, model: str, provider: str, history: list[dict[str, Any]]) -> dict[str, Any]:
        inner = FakeClient(FakeResponses())
        client = Transport(inner, (), provider=provider, signature_models=("*",))
        client.chat.completions.create(model=model, messages=history)
        return inner.chat.completions.calls[0]

    def test_matching_route_restores_signature_verbatim(self) -> None:
        history = self.history()
        sent = self.send("gemini-3.7-flash", "gemini", history)
        calls = sent["messages"][1]["tool_calls"]
        self.assertEqual(calls[0]["extra_content"], self.SIG)
        #  并行调用只有第一路带签名（上游原样给的）：第二路不许补占位——
        #  多补反而偏离模型的原始输出
        self.assertNotIn("extra_content", calls[1])
        #  私有键在净化点被摘掉；原始历史不许被就地改写
        self.assertNotIn(responses.TOOL_EXTRAS_KEY, sent["messages"][1])
        self.assertIn(responses.TOOL_EXTRAS_KEY, history[1])
        self.assertNotIn("extra_content", history[1]["tool_calls"][0])

    def test_foreign_history_gets_placeholder_signatures(self) -> None:
        """会话中途切到签名型号：历史是别家模型产的，没有签名会被 400 拒收。"""
        history = self.history()
        history[1][responses.TOOL_EXTRAS_KEY] = {
            "model": "deepseek-v4-pro",
            "provider": "deepseek",
            "extras": {"a": self.SIG},
        }
        sent = self.send("gemini-3.7-flash", "gemini", history)
        for call in sent["messages"][1]["tool_calls"]:
            self.assertEqual(
                call["extra_content"],
                {"google": {"thought_signature": responses.DUMMY_SIGNATURE}},
            )

    def test_non_signature_model_leaves_foreign_calls_untouched(self) -> None:
        """非签名型号不补占位；不匹配的签名也不许跨路由塞回去。"""
        inner = FakeClient(FakeResponses())
        client = Transport(inner, (), provider="deepseek")
        client.chat.completions.create(model="deepseek-v4-pro", messages=self.history())
        sent = inner.chat.completions.calls[0]
        for call in sent["messages"][1]["tool_calls"]:
            self.assertNotIn("extra_content", call)
        self.assertNotIn(responses.TOOL_EXTRAS_KEY, sent["messages"][1])


class TestReasoningPassthrough(unittest.TestCase):
    ITEM = {"type": "reasoning", "id": "rs_1", "encrypted_content": "ENC"}

    def test_reasoning_item_is_collected_at_done_not_added(self) -> None:
        """added 时 encrypted_content 还是半截的（实测 932 → 1124 字符）。"""
        chunks = list(responses.stream_chunks(iter([
            event("response.output_item.added", output_index=0,
                  item=SimpleNamespace(type="reasoning",
                                       model_dump=lambda **_: {"half": True})),
            event("response.output_item.done", output_index=0,
                  item=SimpleNamespace(type="reasoning",
                                       model_dump=lambda **_: self.ITEM)),
            completed(),
        ])))
        self.assertEqual([c.reasoning for c in chunks if c.reasoning], [[self.ITEM]])

    def test_plaintext_reasoning_is_not_kept(self) -> None:
        """deepseek 给明文 content、qwen 给明文 summary，都没有可回放的加密串。
        回传实测不被服务端消费（input_tokens 97→101），存下来只是让日志变大、
        让原始思维链落盘——一律不留。"""
        chunks = list(responses.stream_chunks(iter([
            event("response.output_item.done", output_index=0,
                  item=SimpleNamespace(type="reasoning", model_dump=lambda **_: {
                      "type": "reasoning", "content": [{"text": "我先算 27*43…"}]})),
            event("response.output_item.done", output_index=1,
                  item=SimpleNamespace(type="reasoning", model_dump=lambda **_: {
                      "type": "reasoning", "summary": [{"text": "思路摘要"}]})),
            completed(),
        ])))
        self.assertEqual([c.reasoning for c in chunks if c.reasoning], [])

    def test_reasoning_replays_before_the_items_it_produced(self) -> None:
        """服务端要求 reasoning item 排在它引出的 message / function_call 之前。"""
        items = to_input(
            [
                {
                    "role": "assistant",
                    "content": "算好了",
                    "tool_calls": [{"id": "c1", "function": {"name": "calc", "arguments": "{}"}}],
                    REASONING_KEY: {"model": "gpt-5.6-sol", "items": [self.ITEM]},
                }
            ],
            "gpt-5.6-sol",
        )
        self.assertEqual(
            [item.get("type") or item["role"] for item in items],
            ["reasoning", "assistant", "function_call"],
        )

    def test_reasoning_is_dropped_when_the_model_changed(self) -> None:
        """加密串是模型侧私有状态：/model 切走之后不能再塞回去。"""
        message = {
            "role": "assistant",
            "content": "算好了",
            REASONING_KEY: {"model": "gpt-5.6-sol", "items": [self.ITEM]},
        }
        self.assertEqual(
            to_input([message], "gpt-5.6-terra"),
            [{"role": "assistant", "content": "算好了"}],
        )

    def test_reasoning_is_dropped_when_the_provider_changed(self) -> None:
        """降级链让同名模型跑在两家上（直连 vs 网关）：signature 是服务侧
        私有状态，跨家回放属于喂错东西。provider 对不上整段跳过；
        旧会话存量（无 provider 标签）在真实路由上同样按不匹配处理。"""
        tagged = {
            "role": "assistant",
            "content": "算好了",
            REASONING_KEY: {"model": "gpt-5.6-sol", "provider": "openai", "items": [self.ITEM]},
        }
        #  同家回放、跨家丢弃
        self.assertEqual(
            to_input([dict(tagged)], "gpt-5.6-sol", "openai")[0].get("type"), "reasoning"
        )
        self.assertEqual(
            to_input([dict(tagged)], "gpt-5.6-sol", "gateway"),
            [{"role": "assistant", "content": "算好了"}],
        )
        #  存量无标签 + 真实路由 → 丢
        legacy = {
            "role": "assistant",
            "content": "算好了",
            REASONING_KEY: {"model": "gpt-5.6-sol", "items": [self.ITEM]},
        }
        self.assertEqual(
            to_input([legacy], "gpt-5.6-sol", "openai"),
            [{"role": "assistant", "content": "算好了"}],
        )

    def test_reasoning_survives_the_transport_not_just_to_input(self) -> None:
        """回归：净化若跑在翻译之前，`_reasoning` 会被当私有键摘掉——回传当场失效，
        且不报任何错，只是悄悄变慢变贵。这条断言走完整条 create 路径。"""
        api = FakeResponses(events=[completed()])
        client = responses_transport(api)
        list(client.chat.completions.create(
            model="gpt-5.6-sol",
            messages=[{"role": "assistant", "content": "算好了",
                       REASONING_KEY: {"model": "gpt-5.6-sol", "items": [self.ITEM]}}],
            stream=True,
        ))
        self.assertEqual(api.calls[0]["input"][0], self.ITEM)

    def test_request_asks_for_encrypted_reasoning(self) -> None:
        """store=False 下不带这个 include 就拿不回推理状态，回传也就无从谈起。"""
        request = to_request("m", [{"role": "user", "content": "hi"}], None, {})
        self.assertEqual(request["include"], ["reasoning.encrypted_content"])


class TestCompletionShape(unittest.TestCase):
    def test_missing_usage_is_none(self) -> None:
        """拿不到 usage 时给 None：内核据此退化为本地估算（见 tokens.py）。"""
        result = to_completion(SimpleNamespace(output_text="x", usage=None))
        self.assertIsNone(result.usage)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
