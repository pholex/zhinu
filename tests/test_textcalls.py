"""文本工具协议（textcalls.py）：请求改写、流式解析、Transport 接入、provider 开关。

全部用假 client，不打网络。
"""

from __future__ import annotations

import json
import types
import unittest
from unittest import mock

from xiaoyu import providers, textcalls
from xiaoyu.responses import wrap
from xiaoyu.textcalls import FENCE_CLOSE, FENCE_OPEN, TAG_CLOSE, TAG_OPEN

from .test_providers import config, isolated_env


def chunk(content: str | None = None):
    delta = types.SimpleNamespace(content=content, tool_calls=None)
    return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)], usage=None)


def usage_chunk():
    return types.SimpleNamespace(
        choices=[], usage=types.SimpleNamespace(prompt_tokens=10, completion_tokens=5)
    )


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读文件",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }
]


def drain(stream):
    """流 → (正文, {index: tool_call dict}, 其它 chunk 数)。镜像 Agent._consume_stream 的攒法。"""
    text, pending, others = [], {}, 0
    for item in stream:
        choices = getattr(item, "choices", None) or []
        if not choices:
            others += 1
            continue
        delta = choices[0].delta
        if delta.content:
            text.append(delta.content)
        for fragment in delta.tool_calls or []:
            slot = pending.setdefault(fragment.index, {"id": "", "name": "", "arguments": ""})
            if fragment.id:
                slot["id"] = fragment.id
            if fragment.function.name:
                slot["name"] = fragment.function.name
            if fragment.function.arguments:
                slot["arguments"] += fragment.function.arguments
    return "".join(text), pending, others


class TestRequestRewrite(unittest.TestCase):
    def test_protocol_note_appended_to_system_and_tools_dropped(self) -> None:
        messages = [{"role": "system", "content": "你是小羽"}, {"role": "user", "content": "hi"}]
        out = textcalls.to_text_messages(messages, TOOLS)
        self.assertEqual(out[0]["role"], "system")
        self.assertTrue(out[0]["content"].startswith("你是小羽"))
        self.assertIn("## read_file", out[0]["content"])
        self.assertIn(FENCE_OPEN, out[0]["content"])
        #  反例必须在：源头就告诉模型"没有 tool_call 等于没做"
        self.assertIn("禁止", out[0]["content"])
        self.assertEqual(out[1], messages[1])

    def test_system_inserted_when_absent(self) -> None:
        out = textcalls.to_text_messages([{"role": "user", "content": "hi"}], TOOLS)
        self.assertEqual(out[0]["role"], "system")
        self.assertIn("# 工具调用规则", out[0]["content"])

    def test_no_tools_means_no_note_but_history_still_rewritten(self) -> None:
        """收尾总结（with_tools=False）：不教协议，但 role=tool 仍必须消失——
        不会工具的 chat template 多半不认 tool 角色。"""
        messages = [
            {"role": "system", "content": "s"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path": "a.py"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "print(1)"},
        ]
        out = textcalls.to_text_messages(messages, None)
        self.assertEqual(out[0]["content"], "s")
        self.assertEqual([m["role"] for m in out], ["system", "assistant", "user"])
        self.assertNotIn("tool_calls", out[1])
        self.assertIn(FENCE_OPEN, out[1]["content"])
        self.assertIn('"name": "read_file"', out[1]["content"])
        self.assertIn('<tool_result name="read_file" id="c1">', out[2]["content"])
        self.assertIn("print(1)", out[2]["content"])

    def test_consecutive_tool_results_merge_and_private_keys_dropped(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "看两个文件",
                "_reasoning": {"items": []},
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "read_file", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "grep", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "a", "content": "A"},
            {"role": "tool", "tool_call_id": "b", "content": "B"},
            {"role": "user", "content": "继续"},
        ]
        out = textcalls.to_text_messages(messages, None)
        self.assertEqual([m["role"] for m in out], ["assistant", "user", "user"])
        self.assertNotIn("_reasoning", out[0])
        self.assertTrue(out[0]["content"].startswith("看两个文件"))
        self.assertIn('name="grep" id="b"', out[1]["content"])


class TestParse(unittest.TestCase):
    def test_fenced_block_becomes_call_and_trailing_text_dropped(self) -> None:
        text = (
            "先看一下。\n" + FENCE_OPEN + '\n{"name": "read_file", "arguments": {"path": "a.py"}}\n'
            + FENCE_CLOSE + "\n文件内容是……（幻觉）"
        )
        lead, calls = textcalls.parse_calls(text)
        self.assertEqual(lead, "先看一下。")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["function"]["name"], "read_file")
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "a.py"})
        self.assertTrue(calls[0]["id"].startswith("call_text_"))

    def test_tag_style_and_multiple_blocks(self) -> None:
        text = (
            TAG_OPEN + '{"name": "read_file", "arguments": {"path": "a"}}' + TAG_CLOSE
            + "\n" + FENCE_OPEN + '\n{"name": "grep", "parameters": {"pattern": "x"}}\n' + FENCE_CLOSE
        )
        lead, calls = textcalls.parse_calls(text)
        self.assertEqual(lead, "")
        self.assertEqual([c["function"]["name"] for c in calls], ["read_file", "grep"])
        self.assertEqual(json.loads(calls[1]["function"]["arguments"]), {"pattern": "x"})

    def test_unclosed_block_still_parsed(self) -> None:
        """max_tokens 把闭合栅栏截掉了：JSON 完整就照样认。"""
        text = FENCE_OPEN + '\n{"name": "read_file", "arguments": {"path": "a"}}'
        _lead, calls = textcalls.parse_calls(text)
        self.assertEqual(len(calls), 1)

    def test_bad_json_keeps_whole_text(self) -> None:
        text = "试试\n" + FENCE_OPEN + '\n{"name": "read_file", "arguments": {oops}}\n' + FENCE_CLOSE
        lead, calls = textcalls.parse_calls(text)
        self.assertEqual(calls, [])
        self.assertEqual(lead, text)

    def test_braces_inside_strings_do_not_break_balance(self) -> None:
        text = FENCE_OPEN + '\n{"name": "bash", "arguments": {"command": "echo \\"{\\" }"}}\n' + FENCE_CLOSE
        _lead, calls = textcalls.parse_calls(text)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"command": 'echo "{" }'})

    def test_string_arguments_are_decoded(self) -> None:
        text = FENCE_OPEN + '\n{"name": "read_file", "arguments": "{\\"path\\": \\"a\\"}"}\n' + FENCE_CLOSE
        _lead, calls = textcalls.parse_calls(text)
        self.assertEqual(json.loads(calls[0]["function"]["arguments"]), {"path": "a"})


class TestStream(unittest.TestCase):
    def test_plain_text_passes_through_with_held_tail_flushed(self) -> None:
        text, pending, _ = drain(textcalls.stream_chunks(iter([chunk("你好"), chunk("，世界`"), chunk("x")])))
        self.assertEqual(text, "你好，世界`x")
        self.assertEqual(pending, {})

    def test_marker_split_across_chunks_is_held_then_parsed(self) -> None:
        """标记被切成三段到达：正文只发到标记之前，半截反引号不能漏到屏幕上。"""
        parts = ["我来读", "一下。\n``", "`tool_", 'call\n{"name": "read_file", ', '"arguments": {"path": "a.py"}}\n```']
        emitted = []
        stream = textcalls.stream_chunks(iter(chunk(p) for p in parts))
        for item in stream:
            if item.choices and item.choices[0].delta.content:
                emitted.append(item.choices[0].delta.content)
            elif item.choices:
                break
        self.assertEqual("".join(emitted).rstrip(), "我来读一下。")
        for piece in emitted:
            self.assertNotIn("`", piece)

    def test_calls_emitted_as_fragments_at_end(self) -> None:
        body = FENCE_OPEN + '\n{"name": "read_file", "arguments": {"path": "a.py"}}\n' + FENCE_CLOSE
        text, pending, others = drain(
            textcalls.stream_chunks(iter([chunk("好的。"), chunk(body), usage_chunk()]))
        )
        self.assertEqual(text, "好的。")
        self.assertEqual(pending[0]["name"], "read_file")
        self.assertEqual(json.loads(pending[0]["arguments"]), {"path": "a.py"})
        self.assertEqual(others, 1, "usage chunk 必须原样放行")

    def test_marker_without_valid_call_flushes_everything(self) -> None:
        body = FENCE_OPEN + "\n不是 JSON\n" + FENCE_CLOSE
        text, pending, _ = drain(textcalls.stream_chunks(iter([chunk("a"), chunk(body)])))
        self.assertEqual(text, "a" + body)
        self.assertEqual(pending, {})

    def test_parallel_calls_get_distinct_indexes(self) -> None:
        body = (
            FENCE_OPEN + '\n{"name": "read_file", "arguments": {"path": "a"}}\n' + FENCE_CLOSE
            + "\n" + FENCE_OPEN + '\n{"name": "read_file", "arguments": {"path": "b"}}\n' + FENCE_CLOSE
        )
        _text, pending, _ = drain(textcalls.stream_chunks(iter([chunk(body)])))
        self.assertEqual(sorted(pending), [0, 1])
        self.assertNotEqual(pending[0]["id"], pending[1]["id"])


class FakeChat:
    def __init__(self, script):
        self.calls = []
        self.script = list(script)

    def create(self, **request):
        self.calls.append(request)
        item = self.script.pop(0)
        return iter(item) if isinstance(item, list) else item


class TestTransport(unittest.TestCase):
    def _client(self, script):
        inner = types.SimpleNamespace(chat=types.SimpleNamespace(completions=FakeChat(script)))
        return inner, wrap(inner, (), text_tool_models=("small",))

    def test_tools_never_reach_the_wire_and_calls_come_back_native_shaped(self) -> None:
        inner, client = self._client(
            [[chunk(FENCE_OPEN + '\n{"name": "read_file", "arguments": {"path": "a"}}\n' + FENCE_CLOSE)]]
        )
        stream = client.chat.completions.create(
            model="small",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            stream_options={"include_usage": True},
            tools=TOOLS,
        )
        _text, pending, _ = drain(stream)
        sent = inner.chat.completions.calls[0]
        self.assertNotIn("tools", sent)
        self.assertEqual(sent["messages"][0]["role"], "system")
        self.assertIn("## read_file", sent["messages"][0]["content"])
        self.assertEqual(pending[0]["name"], "read_file")
        self.assertEqual(client.tool_mode_for("small"), "text")
        self.assertEqual(client.tool_mode_for("big"), "native")

    def test_native_models_untouched(self) -> None:
        inner, client = self._client([[chunk("x")]])
        client.chat.completions.create(
            model="big", messages=[{"role": "user", "content": "hi"}], stream=True, tools=TOOLS
        )
        self.assertEqual(inner.chat.completions.calls[0]["tools"], TOOLS)

    def test_non_stream_completion_parsed(self) -> None:
        message = types.SimpleNamespace(
            content=FENCE_OPEN + '\n{"name": "grep", "arguments": {"pattern": "x"}}\n' + FENCE_CLOSE
        )
        response = types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=message)],
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        )
        _inner, client = self._client([response])
        completion = client.chat.completions.create(
            model="small", messages=[{"role": "user", "content": "hi"}], tools=TOOLS
        )
        call = completion.choices[0].message.tool_calls[0]
        self.assertEqual(call["function"]["name"], "grep")
        self.assertIsNone(completion.choices[0].message.content)
        self.assertEqual(completion.usage.prompt_tokens, 1)

    def test_with_options_keeps_text_tool_models(self) -> None:
        inner = types.SimpleNamespace(with_options=lambda **kw: inner, chat=None)
        client = wrap(inner, (), text_tool_models=("*",))
        self.assertTrue(client.with_options(timeout=1).uses_text_tools("anything"))


class TestProviderSwitch(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch("xiaoyu.config._read_from_keychain", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_generic_provider_opts_into_text_tools(self) -> None:
        env = {
            "XIAOYU_PROVIDER_LOCAL_BASE_URL": "http://localhost:8000/v1",
            "XIAOYU_PROVIDER_LOCAL_API_KEY": "k",
            "XIAOYU_PROVIDER_LOCAL_TOOLS": "text",
        }
        with isolated_env(env):
            registry = providers.build(config(base_url="", model="qwen3-8b"))
            client = registry.client("local")
            self.assertEqual(client.tool_mode_for("qwen3-8b"), "text")
            #  与 wire protocol 正交：没设 PROTOCOL 仍是 chat
            self.assertEqual(client.protocol_for("qwen3-8b"), "chat")

    def test_default_is_native(self) -> None:
        env = {
            "XIAOYU_PROVIDER_LOCAL_BASE_URL": "http://localhost:8000/v1",
            "XIAOYU_PROVIDER_LOCAL_API_KEY": "k",
        }
        with isolated_env(env):
            registry = providers.build(config(base_url="", model="m"))
            self.assertEqual(registry.client("local").tool_mode_for("m"), "native")


if __name__ == "__main__":
    unittest.main()
