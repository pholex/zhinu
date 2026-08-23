"""--output-schema（结构化收尾）的 e2e：真子进程 + scripted 桩。

锁住：
1. 模型调 structured_output 即收尾，result.output 是参数本身，且不再多调一次模型；
2. 模型用正文收尾时被顶回一次（STRUCTURED_OUTPUT_NUDGE），第二轮给出即可；
3. 不合 schema 的参数被拒回（ERROR），重调后通过；
4. 顶回一次仍不给 → output 为 null、带 error、退出码 1；
5. 顶层非 object 的 schema 自动包 value 再拆；
6. 坏 schema / 非一次性模式 → 退出码 2，不调模型。
"""

from __future__ import annotations

import json
import unittest

from tests.test_e2e_scripted import E2ECase

SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "score": {"type": "integer"}},
    "required": ["title", "score"],
}


class OutputSchemaTest(E2ECase):
    def schema_args(self, schema=SCHEMA) -> list[str]:
        return ["--output-schema", json.dumps(schema)]

    def test_tool_call_ends_turn(self):
        script = (
            'tool_call: {"name": "structured_output", "arguments": {"title": "x", "score": 3}}\n'
            'usage: {"prompt_tokens": 10, "completion_tokens": 5}\n'
        )
        events, result, code, stderr = self.run_cli(script, extra_args=self.schema_args())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["output"], {"title": "x", "score": 3})
        self.assertNotIn("error", result)
        #  只有一轮 LLM 调用：脚本只有一轮，多调一次会 ScriptedExhausted
        self.assertEqual(self.kinds(events).count("request.started"), 1)

    def test_text_ending_is_nudged_once(self):
        script = (
            "text: 结论是 x\n"
            "---\n"
            'tool_call: {"name": "structured_output", "arguments": {"title": "x", "score": 1}}\n'
        )
        events, result, code, stderr = self.run_cli(script, extra_args=self.schema_args())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["output"], {"title": "x", "score": 1})
        self.assertEqual(self.kinds(events).count("request.started"), 2)

    def test_invalid_args_rejected_then_retry(self):
        script = (
            'tool_call: {"name": "structured_output", "arguments": {"title": "x", "score": "3"}}\n'
            "---\n"
            'tool_call: {"name": "structured_output", "arguments": {"title": "x", "score": 3}}\n'
        )
        events, result, code, stderr = self.run_cli(script, extra_args=self.schema_args())
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["output"], {"title": "x", "score": 3})
        outputs = [e["output"] for e in events if e["kind"] == "tool.completed"]
        self.assertTrue(outputs[0].startswith("ERROR"), outputs)

    def test_missing_output_is_failure(self):
        script = "text: 我就是不调工具\n---\ntext: 还是不调\n"
        _, result, code, stderr = self.run_cli(script, extra_args=self.schema_args())
        self.assertEqual(code, 1, stderr)
        self.assertIsNone(result["output"])
        self.assertIn("output-schema", result["error"])

    def test_non_object_schema_wrapped(self):
        schema = {"type": "array", "items": {"type": "string"}}
        script = 'tool_call: {"name": "structured_output", "arguments": {"value": ["a", "b"]}}\n'
        _, result, code, stderr = self.run_cli(script, extra_args=self.schema_args(schema))
        self.assertEqual(code, 0, stderr)
        self.assertEqual(result["output"], ["a", "b"])

    def test_bad_schema_exits_2(self):
        _, _, code, stderr = self.run_cli("text: 不该调到\n", extra_args=["--output-schema", "{nope"])
        self.assertEqual(code, 2)
        self.assertIn("output-schema", stderr)


if __name__ == "__main__":
    unittest.main()
