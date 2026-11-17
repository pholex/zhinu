"""确定性测试桩（scripted provider）的单元测试。不打网络。

DSL 解析、chunk 形状、队列耗尽、providers 接线各卡一处。
子进程黑盒 e2e 在 test_e2e_scripted.py。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from xiaoyu import providers
from xiaoyu.scripted import (
    SCRIPTED_ENV,
    SCRIPTED_PROVIDER,
    ScriptedClient,
    ScriptedExhausted,
    ScriptError,
    parse_scripts,
)

from .test_providers import config, isolated_env


class ParseTest(unittest.TestCase):
    def test_turns_split_and_comments_ignored(self):
        turns = parse_scripts(
            "# 注释\n"
            "text: 你好\n"
            "\n"
            "---\n"
            'tool_call: {"name": "bash", "arguments": {"command": "ls"}}\n'
            "---\n"
        )
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0], [("text", "你好")])
        self.assertEqual(turns[1][0][0], "tool_call")

    def test_text_json_string_form_keeps_newline(self):
        turns = parse_scripts('text: "第一行\\n第二行"')
        self.assertEqual(turns[0], [("text", "第一行\n第二行")])

    def test_unknown_kind_raises_with_lineno(self):
        with self.assertRaises(ScriptError) as ctx:
            parse_scripts("text: ok\nbogus: x\n")
        self.assertIn("第 2 行", str(ctx.exception))

    def test_tool_call_requires_name(self):
        with self.assertRaises(ScriptError):
            parse_scripts('tool_call: {"arguments": {}}')

    def test_bad_json_payload_raises(self):
        with self.assertRaises(ScriptError):
            parse_scripts("usage: {不是json}")


class ClientTest(unittest.TestCase):
    def _stream_chunks(self, script: str, **request):
        client = ScriptedClient(parse_scripts(script))
        return list(client.chat.completions.create(stream=True, **request))

    def test_text_and_usage_chunks(self):
        chunks = self._stream_chunks('text: hi\nusage: {"prompt_tokens": 9, "completion_tokens": 2}')
        self.assertEqual(chunks[0].choices[0].delta.content, "hi")
        self.assertEqual(chunks[1].usage.prompt_tokens, 9)
        self.assertEqual(chunks[1].choices, [])

    def test_tool_call_whole_and_parts_assemble_identically(self):
        whole = self._stream_chunks(
            'tool_call: {"name": "bash", "arguments": {"command": "ls"}, "id": "c1"}'
        )
        parts = self._stream_chunks(
            'tool_call_part: {"index": 0, "id": "c1", "name": "bash"}\n'
            'tool_call_part: {"index": 0, "arguments": "{\\"command\\""}\n'
            'tool_call_part: {"index": 0, "arguments": ": \\"ls\\"}"}'
        )
        #  按 agent._consume_stream 的累加逻辑拼装两种形态，结果必须一致
        for chunks in (whole, parts):
            slot = {"id": "", "name": "", "arguments": ""}
            for chunk in chunks:
                for fragment in chunk.choices[0].delta.tool_calls or []:
                    if fragment.id:
                        slot["id"] = fragment.id
                    if fragment.function.name and not slot["name"]:
                        slot["name"] = fragment.function.name
                    if fragment.function.arguments:
                        slot["arguments"] += fragment.function.arguments
            self.assertEqual(slot["id"], "c1")
            self.assertEqual(slot["name"], "bash")
            self.assertEqual(json.loads(slot["arguments"]), {"command": "ls"})

    def test_error_raises_at_position(self):
        client = ScriptedClient(parse_scripts("text: 半截\nerror: 上游 429"))
        stream = client.chat.completions.create(stream=True)
        self.assertEqual(next(stream).choices[0].delta.content, "半截")
        with self.assertRaises(RuntimeError) as ctx:
            next(stream)
        self.assertIn("429", str(ctx.exception))

    def test_exhausted_is_loud(self):
        client = ScriptedClient(parse_scripts("text: only"))
        list(client.chat.completions.create(stream=True))
        with self.assertRaises(ScriptedExhausted):
            client.chat.completions.create(stream=True)

    def test_non_stream_response_for_summary_path(self):
        client = ScriptedClient(parse_scripts('text: 摘要\nusage: {"prompt_tokens": 5}'))
        response = client.chat.completions.create(model="x", messages=[])
        self.assertEqual(response.choices[0].message.content, "摘要")
        self.assertEqual(response.usage.prompt_tokens, 5)


class WiringTest(unittest.TestCase):
    """providers 接线：环境变量一设，路由世界里只剩 scripted。"""

    def test_scripted_is_exclusive_and_wildcard(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "s.txt"
            script.write_text("text: ok\n", encoding="utf-8")
            #  即使配了直连 key，scripted 一开也不注册别家——独占是硬保证
            with isolated_env(
                {SCRIPTED_ENV: str(script), "DEEPSEEK_API_KEY": "leak-should-not-register"}
            ):
                registry = providers.build(config())
                self.assertEqual([p.name for p in registry.providers], [SCRIPTED_PROVIDER])
                route = registry.resolve("any-model-name")
                self.assertEqual(route.provider, SCRIPTED_PROVIDER)

    def test_client_is_scripted_and_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "s.txt"
            script.write_text("text: ok\n", encoding="utf-8")
            with isolated_env({SCRIPTED_ENV: str(script)}):
                registry = providers.build(config())
                first = registry.client(SCRIPTED_PROVIDER)
                second = registry.client(SCRIPTED_PROVIDER)
            self.assertIsInstance(first, ScriptedClient)
            self.assertIs(first, second)


if __name__ == "__main__":
    unittest.main()
