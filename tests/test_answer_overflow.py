"""结论类工具（子 agent / 检索 / 联网搜索）的结论过长时：落盘可召回，不硬切。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from xiaoyu.agents import ANSWER_INLINE_LIMIT, MAX_ANSWER_CHARS, AgentSpec, make_subagent_tool
from xiaoyu.config import Config
from xiaoyu.tools import Tool, Toolbox

from .test_agent_paths import AgentTestCase, call_fragment, chunk


class OutputLimitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(
            base_url="x", model="m", workspace=Path(self.tmp.name).resolve(),
            enable_plugins=False, enable_mcp=False,
        )
        self.box = Toolbox(self.config)

    def register(self, text: str, limit: int | None) -> None:
        self.box.register(
            Tool(
                name="report", description="d", parameters={"type": "object", "properties": {}},
                handler=lambda: text, requires_approval=False, output_limit=limit,
            )
        )

    def test_overflow_keeps_both_ends_and_the_middle_is_recallable(self) -> None:
        text = "开头结论\n" + "\n".join(f"证据 {i}" for i in range(400)) + "\n结尾的建议"
        self.register(text, limit=600)
        out = self.box.run("report", {})
        self.assertIn("开头结论", out)
        self.assertIn("结尾的建议", out)
        self.assertNotIn("证据 200", out)
        self.assertIn("召回 id: 1", out)
        self.assertIn("证据 200", self.box.run("recall", {"id": "1", "pattern": "证据 200$"}))

    def test_within_the_limit_nothing_changes(self) -> None:
        self.register("短结论", limit=600)
        self.assertEqual(self.box.run("report", {}), "短结论")

    def test_no_declared_limit_means_the_global_one(self) -> None:
        text = "x" * 5000
        self.register(text, limit=None)
        self.assertEqual(self.box.run("report", {}), text)

    def test_a_tool_cannot_raise_the_global_limit(self) -> None:
        self.config.max_tool_output = 300
        self.register("y" * 1000, limit=10_000)
        self.assertIn("召回 id", self.box.run("report", {}))


class SubagentAnswerTest(AgentTestCase):
    def test_long_conclusion_keeps_its_tail_and_the_resume_handle(self) -> None:
        spec = AgentSpec(
            name="doc_reader", description="查文档", system_prompt="工作区 {workspace}",
            tools=("read_file", "grep", "list_files"),
        )
        answer = "结论开头。" + "细节" * MAX_ANSWER_CHARS + "所以应该改 calc.py 的 add。"
        agent = self.build(
            [
                [chunk(tool_calls=[call_fragment(0, "c1", "doc_reader", json.dumps({"task": "查"}))])],
                [chunk(content=answer)],
                [chunk(content="收到")],
            ]
        )
        tool = make_subagent_tool(
            spec, self.config, agent.registry, agent.usage, agent.sink,
            agent.approver, agent.permissions,
        )
        self.assertEqual(tool.output_limit, ANSWER_INLINE_LIMIT)
        agent.toolbox.register(tool)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("查一下")
        (result,) = [m["content"] for m in agent.messages if m.get("role") == "tool"]
        self.assertLess(len(result), ANSWER_INLINE_LIMIT + 600)  # 仍然省上下文
        self.assertIn("结论开头。", result)
        self.assertIn("所以应该改 calc.py 的 add。", result)  # 后半截不再被切掉
        self.assertIn("resume_from:", result)
        self.assertIn("召回 id", result)


if __name__ == "__main__":
    unittest.main()
