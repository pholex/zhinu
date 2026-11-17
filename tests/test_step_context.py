"""StepContext：一步之内只看步开头的快照，中途改动下一步生效。"""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from xiaoyu import modes
from xiaoyu.tools import Tool

from tests.test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk


def tool_call(call_id: str, name: str, args: dict) -> list:
    return [
        chunk(tool_calls=[call_fragment(0, call_id, name, json.dumps(args))]),
        usage_chunk(100, 10),
    ]


def two_tool_calls(first: tuple[str, str, dict], second: tuple[str, str, dict]) -> list:
    return [
        chunk(tool_calls=[call_fragment(0, first[0], first[1], json.dumps(first[2]))]),
        chunk(tool_calls=[call_fragment(1, second[0], second[1], json.dumps(second[2]))]),
        usage_chunk(100, 10),
    ]


class StepContextTest(AgentTestCase):
    def _register(self, agent, name: str, handler) -> None:
        agent.toolbox.register(
            Tool(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}},
                handler=handler,
                requires_approval=False,
            )
        )

    def test_mid_step_changes_apply_next_step(self) -> None:
        """第 1 步里切模型、切档、注册新工具：本步照旧，第 2 步全部生效。"""
        seen: dict[str, str] = {}
        script = [
            two_tool_calls(("c1", "flip", {}), ("c2", "late", {})),
            tool_call("c3", "late", {}),
            [chunk(content="done"), usage_chunk(100, 10)],
        ]
        agent = self.build(script)

        def flip() -> str:
            agent.switch_model("other-model")
            agent.set_mode(modes.AUTO)
            self._register(agent, "late", lambda: "late ran")
            seen["model"] = agent._step.model  # noqa: SLF001
            seen["mode"] = agent._step.mode  # noqa: SLF001
            return "flipped"

        self._register(agent, "flip", flip)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("go")

        #  快照里仍是步开头的值，尽管 flip 已经改了 self
        self.assertEqual(seen, {"model": "main-model", "mode": modes.DEFAULT})
        calls = self.client.completions.calls
        self.assertEqual([call["model"] for call in calls], ["main-model", "other-model", "other-model"])
        first_tools = {schema["function"]["name"] for schema in calls[0]["tools"]}
        second_tools = {schema["function"]["name"] for schema in calls[1]["tools"]}
        self.assertNotIn("late", first_tools)
        self.assertIn("late", second_tools)
        #  同一步里对刚注册工具的调用被拒；下一步正常执行
        outputs = [entry for entry in agent.trace if entry["tool"] == "late"]
        tool_messages = [m["content"] for m in agent.messages if m.get("role") == "tool"]
        self.assertTrue(any("不在本步可见集合" in text for text in tool_messages))
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["output"], "late ran")

    def test_step_is_frozen(self) -> None:
        agent = self.build([])
        ctx = agent._begin_step()  # noqa: SLF001
        with self.assertRaises(Exception):
            ctx.model = "x"  # type: ignore[misc]
        self.assertEqual(ctx.index, 1)
        self.assertEqual(ctx.history_version, agent.history_version)
        self.assertIn("read_file", ctx.tool_names)
        #  不带工具的收尾步：可见集合为空
        self.assertEqual(agent._begin_step(with_tools=False).tool_names, frozenset())  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
