"""内置参数声明须容纳既有可选空值，并与 handler 的必填项、参数名对账。"""

import copy
import inspect
from types import SimpleNamespace
from unittest import mock

from xiaoyu.agent import Agent
from xiaoyu.providers import Registry
from xiaoyu.tools import Toolbox, _argument_problem, _prepare_arguments
from .test_agent_paths import AgentTestCase, FakeClient


class BuiltinContractTest(AgentTestCase):
    def tools(self):
        box = Toolbox(self.config, mcp_view=SimpleNamespace(ready_tools=lambda: []))
        # 只为让 Agent 把内置工具登记进 box；模型端点用桩，测试机上没有任何 key 也能跑。
        Agent(self.config, box, registry=Registry.for_client(FakeClient([])))
        return box

    def test_declarations_match_handler_arguments_and_optional_nulls(self):
        for name, tool in self.tools()._tools.items():
            with self.subTest(tool=name):
                params = inspect.signature(tool.handler).parameters
                props = tool.parameters.get("properties", {})
                required = set(tool.parameters.get("required", []))
                self.assertLessEqual(required, props.keys())
                if not any(p.kind == p.VAR_KEYWORD for p in params.values()):
                    self.assertLessEqual(props.keys(), params.keys())
                for p in params.values():
                    if p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD) and p.default is p.empty:
                        self.assertIn(p.name, required)
                    if p.default is None and p.name in props and p.name not in required:
                        self.assertEqual(_argument_problem(None, props[p.name]), "", f"{name}.{p.name}")

    def test_nullable_coercion_preserves_supported_string_inputs(self):
        for kind, raw, expected in (("integer", "2", 2), ("number", "2.5", 2.5),
                                    ("boolean", "false", False), ("object", '{"x":1}', {"x": 1}),
                                    ("array", '["x"]', ["x"])):
            schema = {"type": "object", "properties": {"value": {"type": [kind, "null"]}}}
            with self.subTest(kind=kind):
                args = {"value": raw}
                original = copy.deepcopy(args)
                prepared, error = _prepare_arguments(args, schema)
                self.assertEqual(error, "")
                self.assertEqual(prepared, {"value": expected})
                self.assertEqual(args, original)
                self.assertEqual(_prepare_arguments({"value": None}, schema), ({"value": None}, ""))

    def test_read_null_means_omitted_and_numeric_text_still_works(self):
        box = self.tools()
        plain = box.run("read_file", {"path": "calc.py"})
        self.assertEqual(box.run("read_file", {"path": "calc.py", "offset": None, "limit": None}), plain)
        sliced = box.run("read_file", {"path": "calc.py", "offset": "2", "limit": "1"})
        self.assertIn("return a + b", sliced)
        self.assertNotIn("def add", sliced)
        self.assertTrue(box.run("read_file", {"path": "calc.py", "offset": []}).startswith("ERROR:"))

    def test_mcp_empty_inputs_accept_omitted_null_and_encoded_object(self):
        remote = SimpleNamespace(name="mcp__test__probe", server="test", fingerprint="one",
                                 parameters={"type": "object", "properties": {}},
                                 handler=mock.Mock(return_value="ok"), check_fn=lambda: True)
        box = Toolbox(self.config, mcp_view=SimpleNamespace(ready_tools=lambda: [remote]))
        for value in (None, {}, "{}"):
            self.assertEqual(box.run("use_tool", {"tool_name": remote.name, "tool_input": value}), "ok")
        self.assertEqual(box.run("use_tool", {"tool_name": remote.name}), "ok")
        self.assertEqual(remote.handler.call_count, 4)
        self.assertTrue(box.run("use_tool", {"tool_name": remote.name, "tool_input": []}).startswith("ERROR:"))
        self.assertEqual(remote.handler.call_count, 4)
