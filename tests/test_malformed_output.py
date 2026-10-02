"""模型输出畸形容错：隐形字符、双向控制、工具名畸变、重复 id、空回复、截断指引、超限识别、schema 消毒。"""

from __future__ import annotations

import contextlib
import io
import json
import types
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import invisible, mcp, promptfile, skills, ui
from xiaoyu.errors import classify
from xiaoyu.permissions import Permissions, parse_rule
from xiaoyu.tools import Tool, canonical_tool_name

from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk
from .test_errors import _DuckStatusError, length_chunk

TAG_HELLO = "\U000e0068\U000e0069"  # Tag 块拼出的 "hi"


class StripInvisibleTest(unittest.TestCase):
    def setUp(self) -> None:
        invisible._warned = False

    def test_tag_block_and_zero_width_space_are_removed(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            out = invisible.strip_invisible(f"正文{TAG_HELLO}继续​﻿⁠")
        self.assertEqual(out, "正文继续")

    def test_joiners_and_bidi_marks_are_kept(self) -> None:
        text = "👨‍👩‍👧 نص‌ عربي ‏"
        self.assertEqual(invisible.strip_invisible(text), text)

    def test_warns_once_per_process(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            invisible.strip_invisible(TAG_HELLO, "测试")
            invisible.strip_invisible(TAG_HELLO, "测试")
        self.assertEqual(err.getvalue().count("隐形 Unicode"), 1)
        self.assertIn("测试", err.getvalue())


class StripInvisibleEntryPointsTest(AgentTestCase):
    """五个入口都走同一个函数：工具结果、项目指令文件、技能、提示词文件、MCP 描述。"""

    def setUp(self) -> None:
        super().setUp()
        invisible._warned = True  # 入口测试不关心提示

    def test_tool_result_is_stripped(self) -> None:
        (self.root / "hidden.txt").write_text(f"看得见{TAG_HELLO}的", encoding="utf-8")
        agent = self.build([])
        out = agent.toolbox.run("read_file", {"path": "hidden.txt"})
        self.assertIn("看得见的", out)
        self.assertNotIn("\U000e0068", out)

    def test_project_docs_are_stripped(self) -> None:
        from xiaoyu.agent import collect_project_docs

        (self.root / "AGENTS.md").write_text(f"规范{TAG_HELLO}", encoding="utf-8")
        docs = collect_project_docs(self.root, ("AGENTS.md",), cap=1_000)
        self.assertEqual(docs[0][1], "规范")

    def test_skill_body_and_description_are_stripped(self) -> None:
        skill_dir = self.root / "skills" / "s"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: s\ndescription: 干活{TAG_HELLO}\n---\n\n步骤{TAG_HELLO}一\n",
            encoding="utf-8",
        )
        found = skills.scan_skills(directories=(self.root / "skills",))
        skill = next(item for item in found if item.name == "s")
        self.assertEqual(skill.description, "干活")
        self.assertEqual(skills.load_skill_body(skill).strip(), "步骤一")

    def test_prompt_file_is_stripped(self) -> None:
        self.assertEqual(promptfile.parse(f"你是{TAG_HELLO}炉匠").text, "你是炉匠")

    def test_mcp_tool_and_parameter_descriptions_are_stripped(self) -> None:
        server = types.SimpleNamespace(spec=types.SimpleNamespace(name="s", trust_content=False))
        remote = mcp._make_remote_tool(
            None,
            server,
            {
                "name": "t",
                "description": f"查{TAG_HELLO}询",
                "inputSchema": {
                    "type": "object",
                    "properties": {"q": {"type": "string", "description": f"关键{TAG_HELLO}词"}},
                },
            },
        )
        self.assertEqual(remote.description, "[MCP·s] 查询")
        self.assertEqual(remote.parameters["properties"]["q"]["description"], "关键词")


class BidiEscapeTest(unittest.TestCase):
    def test_overrides_become_visible_in_whole_strings(self) -> None:
        shown = ui.strip_sequences("rm -rf ‮elif.txt‬")
        self.assertEqual(shown, "rm -rf \\u202eelif.txt\\u202c")

    def test_isolates_and_marks_too(self) -> None:
        self.assertEqual(ui.strip_sequences("a⁦b⁩c‎"), "a\\u2066b\\u2069c\\u200e")

    def test_preview_of_tool_args_is_escaped(self) -> None:
        self.assertIn("\\u202e", ui.preview("echo ‮", 80))

    def test_streamed_text_keeps_bidi_marks(self) -> None:
        #  正文分片不转：阿拉伯/希伯来文段落的方向标记是正常内容
        self.assertEqual(ui.strip_controls("نص‏"), "نص‏")


class CanonicalToolNameTest(unittest.TestCase):
    KNOWN = frozenset({"bash", "read_file", mcp.public_tool_name("noc", "query"),
                       mcp.public_tool_name("a.b", "c"), mcp.public_tool_name("a", "b.c")})

    def test_namespace_prefixes_are_stripped(self) -> None:
        for given in ("functions.bash", "functions:bash", "tools.bash", "tools:read_file"):
            self.assertEqual(canonical_tool_name(given, self.KNOWN), given.split(".")[-1].split(":")[-1])

    def test_mcp_spellings_map_to_the_advertised_name(self) -> None:
        for given in ("noc.query", "noc__query", "noc:query", "mcp__noc.query", "functions.noc.query"):
            self.assertEqual(canonical_tool_name(given, self.KNOWN), "mcp__noc__query", given)

    def test_ambiguous_split_is_not_guessed(self) -> None:
        #  a.b.c 既可以是 (a.b, c) 也可以是 (a, b.c)，两个都真实存在：不猜
        self.assertIsNone(canonical_tool_name("a.b.c", self.KNOWN))

    def test_no_fuzzy_matching(self) -> None:
        self.assertIsNone(canonical_tool_name("bsh", self.KNOWN))
        self.assertIsNone(canonical_tool_name("functions.", self.KNOWN))
        self.assertIsNone(canonical_tool_name("", self.KNOWN))


class ToolNameRecoveryInAgentTest(AgentTestCase):
    def _call(self, name: str, args: dict) -> dict:
        return {"id": "c1", "function": {"name": name, "arguments": json.dumps(args)}}

    def test_prefixed_name_runs_the_real_tool(self) -> None:
        agent = self.build([])
        agent._begin_step()
        with contextlib.redirect_stdout(io.StringIO()):
            result = agent._execute(self._call("functions.read_file", {"path": "calc.py"}))
        self.assertIn("def add", result["content"])
        self.assertEqual(agent.trace[-1]["tool"], "read_file")

    def test_recovered_name_still_hits_deny_rules(self) -> None:
        agent = self.build(
            [], permissions=Permissions(self.root, [parse_rule("deny read_file")])
        )
        agent._begin_step()
        with contextlib.redirect_stdout(io.StringIO()):
            result = agent._execute(self._call("tools.read_file", {"path": "calc.py"}))
        self.assertIn("deny", result["content"])
        self.assertNotIn("def add", result["content"])

    def test_unrecoverable_name_lists_the_visible_tools(self) -> None:
        agent = self.build([])
        agent._begin_step()
        with contextlib.redirect_stdout(io.StringIO()):
            result = agent._execute(self._call("reed_file", {"path": "calc.py"}))
        self.assertIn("ERROR", result["content"])
        self.assertIn("read_file", result["content"])

    def test_toolbox_direct_callers_get_the_same_recovery(self) -> None:
        agent = self.build([])
        self.assertIn("def add", agent.toolbox.run("functions.read_file", {"path": "calc.py"}))
        self.assertIn("未知工具", agent.toolbox.run("nope", {}))


class DuplicateCallIdTest(AgentTestCase):
    def test_second_call_with_same_id_gets_a_fresh_local_id(self) -> None:
        agent = self.build([
            [
                chunk(tool_calls=[call_fragment(0, "dup", "read_file", '{"path": "calc.py"}')]),
                chunk(tool_calls=[call_fragment(1, "dup", "read_file", '{"path": "calc.py"}')]),
                usage_chunk(10, 5),
            ],
            [chunk(content="读完了"), usage_chunk(10, 2)],
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("读两遍")
        issued = [
            call["id"]
            for message in agent.messages
            if message["role"] == "assistant"
            for call in message.get("tool_calls") or []
        ]
        answered = [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"]
        self.assertEqual(len(issued), 2)
        self.assertEqual(issued[0], "dup")
        self.assertTrue(issued[1].startswith("call_local_"), issued)
        self.assertEqual(answered, issued)


class EmptyReplyNotRecordedTest(AgentTestCase):
    def _empties(self, agent) -> list:
        return [
            m for m in agent.messages
            if m.get("role") == "assistant" and not m.get("content") and not m.get("tool_calls")
        ]

    def test_empty_reply_is_not_written_into_history(self) -> None:
        from xiaoyu.agent import EMPTY_REPLY_NUDGE

        empty = [chunk(content=None)]
        agent = self.build([empty, empty, empty, [chunk(content="补上的结论")]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("改一下代码")
        self.assertEqual(self._empties(agent), [])
        self.assertEqual([m["role"] for m in agent.messages[1:]], ["user", "user", "assistant"])
        self.assertEqual(agent.messages[2]["content"], EMPTY_REPLY_NUDGE)
        self.assertEqual(agent.last_assistant_text(), "补上的结论")

    def test_persistent_empty_reply_leaves_no_shells(self) -> None:
        empty = [chunk(content=None)]
        agent = self.build([empty] * 6)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("改一下代码")
        self.assertEqual(self._empties(agent), [])

    def test_half_streamed_answer_still_recorded(self) -> None:
        agent = self.build([[chunk(content="说到一半"), length_chunk(), usage_chunk(10, 5)]] * 4)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertTrue(any("说到一半" in str(m.get("content")) for m in agent.messages))


class TruncatedToolCallGuidanceTest(AgentTestCase):
    def test_marker_tells_the_model_to_split_the_step(self) -> None:
        agent = self.build([
            [
                chunk(tool_calls=[call_fragment(0, "c1", "read_file", '{"path": "calc.py"}')]),
                chunk(tool_calls=[call_fragment(1, "c2", "write_file", '{"path": "b.py", "con')]),
                length_chunk(),
            ],
            [chunk(content="读完了"), usage_chunk(100, 5)],
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("读再写")
        cut = next(m for m in agent.messages if m["role"] == "assistant")
        self.assertIn("1 个没写完的工具调用已丢弃", cut["content"])
        self.assertIn("拆小", cut["content"])
        self.assertNotIn("合法 JSON", cut["content"])

    def test_invalid_json_under_length_limit_gets_truncation_guidance(self) -> None:
        agent = self.build([])
        call = {"id": "c1", "function": {"name": "write_file", "arguments": '{"path": "b.py", "con'}}
        agent._length_truncated = True
        with contextlib.redirect_stdout(io.StringIO()):
            result = agent._execute(call)
        self.assertIn("截断", result["content"])
        self.assertIn("拆小", result["content"])
        self.assertNotIn("合法 JSON", result["content"])
        agent._length_truncated = False
        with contextlib.redirect_stdout(io.StringIO()):
            result = agent._execute(call)
        self.assertIn("合法 JSON", result["content"])


class _BodyError(Exception):
    def __init__(self, message: str, status_code: int, body: dict) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class PayloadTooLargeTest(unittest.TestCase):
    def test_413_is_context_overflow(self) -> None:
        verdict = classify(_DuckStatusError("Payload Too Large", 413))
        self.assertEqual(verdict.kind, "context_overflow")
        self.assertTrue(verdict.should_compact)

    def test_body_size_wordings(self) -> None:
        for text in (
            "request body too large",
            "Request Entity Too Large",
            "content length 1234567 bytes exceeds the maximum of 1000000",
            "the request exceeds the available context size. try increasing the context size",
        ):
            with self.subTest(text=text):
                self.assertEqual(classify(_DuckStatusError(text, 400)).kind, "context_overflow")

    def test_content_length_header_errors_are_not_overflow(self) -> None:
        self.assertEqual(classify(_DuckStatusError("missing Content-Length header", 400)).kind, "fatal")

    def test_llama_cpp_structured_fields(self) -> None:
        typed = _BodyError(
            "bad request", 400,
            {"error": {"code": 400, "message": "x", "type": "exceed_context_size_error"}},
        )
        counted = _BodyError(
            "bad request", 400, {"error": {"n_prompt_tokens": 5000, "n_ctx": 4096, "message": "x"}}
        )
        fits = _BodyError(
            "bad request", 400, {"error": {"n_prompt_tokens": 100, "n_ctx": 4096, "message": "x"}}
        )
        self.assertEqual(classify(typed).kind, "context_overflow")
        self.assertEqual(classify(counted).kind, "context_overflow")
        self.assertEqual(classify(fits).kind, "fatal")


class SchemaSanitizeTest(unittest.TestCase):
    def test_oneof_consts_fold_to_enum_with_descriptions(self) -> None:
        out = mcp._normalize_schema({
            "type": "object",
            "properties": {"mode": {
                "description": "模式",
                "oneOf": [
                    {"const": "fast", "description": "快"},
                    {"const": "slow", "description": "慢"},
                    {"type": "null"},
                ],
            }},
        })
        mode = out["properties"]["mode"]
        self.assertNotIn("oneOf", mode)
        self.assertEqual(mode["enum"], ["fast", "slow"])
        self.assertEqual(mode["type"], "string")
        self.assertEqual(mode["description"], '模式\n"fast": 快\n"slow": 慢')

    def test_both_union_keys_present_left_alone(self) -> None:
        node = {"anyOf": [{"const": 1}], "oneOf": [{"const": 2}]}
        self.assertEqual(mcp._collapse_const_union(node), node)

    def test_leaf_ref_is_inlined_and_defs_pruned(self) -> None:
        out = mcp._inline_refs({
            "type": "object",
            "properties": {"who": {"$ref": "#/$defs/Person", "description": "谁"}},
            "$defs": {
                "Person": {"type": "object", "description": "人", "properties": {"n": {"type": "string"}}},
                "Unused": {"type": "string"},
            },
        })
        who = out["properties"]["who"]
        self.assertNotIn("$ref", who)
        self.assertEqual(who["type"], "object")
        self.assertEqual(who["description"], "谁")  # 引用处的注解优先
        self.assertNotIn("$defs", out)

    def test_chained_refs_converge_and_recursive_ones_stay(self) -> None:
        out = mcp._inline_refs({
            "type": "object",
            "properties": {"a": {"$ref": "#/definitions/A"}, "t": {"$ref": "#/definitions/Tree"}},
            "definitions": {
                "A": {"type": "object", "properties": {"b": {"$ref": "#/definitions/B"}}},
                "B": {"type": "integer"},
                "Tree": {"type": "object", "properties": {"kids": {
                    "type": "array", "items": {"$ref": "#/definitions/Tree"}}}},
            },
        })
        self.assertEqual(out["properties"]["a"]["properties"]["b"], {"type": "integer"})
        self.assertEqual(out["properties"]["t"], {"$ref": "#/definitions/Tree"})
        self.assertEqual(set(out["definitions"]), {"Tree"})

    def test_old_dialect_is_skipped(self) -> None:
        schema = {
            "$schema": "http://json-schema.org/draft-04/schema#",
            "type": "object",
            "properties": {"a": {"$ref": "#/definitions/A"}},
            "definitions": {"A": {"type": "string"}},
        }
        self.assertEqual(mcp._inline_refs(schema), schema)

    def test_literal_values_are_not_walked(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "object", "default": {"$ref": "#/$defs/X"}}},
            "$defs": {"X": {"type": "string"}},
        }
        out = mcp._inline_refs(schema)
        self.assertEqual(out["properties"]["a"]["default"], {"$ref": "#/$defs/X"})

    def test_remote_tool_schema_has_no_refs_left(self) -> None:
        server = types.SimpleNamespace(spec=types.SimpleNamespace(name="s", trust_content=False))
        remote = mcp._make_remote_tool(None, server, {
            "name": "t",
            "inputSchema": {
                "type": "object",
                "properties": {"p": {"$ref": "#/$defs/P"}},
                "$defs": {"P": {"type": "string", "oneOf": [{"const": "x"}, {"const": "y"}]}},
            },
        })
        self.assertEqual(json.dumps(remote.parameters).count("$ref"), 0)
        self.assertEqual(remote.parameters["properties"]["p"]["enum"], ["x", "y"])


if __name__ == "__main__":
    unittest.main()
