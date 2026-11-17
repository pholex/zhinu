"""参数必须在产生副作用之前校验；本地工具与 MCP 两种暴露方式保持一致。"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest import mock

from xiaoyu.agent import Agent, Allow
from xiaoyu.providers import Registry
from xiaoyu.tools import Tool, Toolbox

from .test_agent_paths import AgentTestCase, FakeClient


SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["read"]},
        "count": {"type": "integer"},
        "options": {
            "type": "array",
            "items": {
                "type": "object", "properties": {"enabled": {"type": "boolean"}},
                "required": ["enabled"], "additionalProperties": False,
            },
        },
    },
    "required": ["mode", "count"],
    "additionalProperties": False,
}


class ToolValidationTest(AgentTestCase):
    def box(self, schema=None, **kwargs):
        box = Toolbox(self.config, only=[])
        handler = mock.Mock(return_value="ok")
        box.register(Tool("probe", "参数验证", copy.deepcopy(schema or SCHEMA), handler, **kwargs))
        return box, handler

    def test_invalid_arguments_never_reach_handler(self):
        invalid = [
            {"mode": "read"},
            {"mode": "write", "count": 1},
            {"mode": "read", "count": []},
            {"mode": "read", "count": True},
            {"mode": "read", "count": 1.5},
            {"mode": "read", "count": 1, "extra": True},
            {"mode": "read", "count": 1, "options": [{}]},
            {"mode": "read", "count": 1, "options": [{"enabled": []}]},
            [],
        ]
        for args in invalid:
            with self.subTest(args=args):
                box, handler = self.box()
                self.assertTrue(box.run("probe", args).startswith("ERROR:"))
                handler.assert_not_called()

    def test_coercion_precedes_validation_without_mutating_input(self):
        box, handler = self.box()
        args = {"mode": "read", "count": "2", "options": [{"enabled": "false"}]}
        original = copy.deepcopy(args)
        self.assertEqual(box.run("probe", args), "ok")
        handler.assert_called_once_with(mode="read", count=2, options=[{"enabled": False}])
        self.assertEqual(args, original)

    def test_additional_properties_follow_schema(self):
        for extra in (None, True, {"type": "string"}):
            with self.subTest(extra=extra):
                schema = copy.deepcopy(SCHEMA)
                schema.pop("additionalProperties")
                if extra is not None:
                    schema["additionalProperties"] = extra
                box, handler = self.box(schema)
                args = {"mode": "read", "count": 1, "extra": "retained"}
                self.assertEqual(box.run("probe", args), "ok")
                handler.assert_called_once_with(**args)
        box, handler = self.box({"type": "object", "additionalProperties": {"type": "string"}})
        self.assertTrue(box.run("probe", {"extra": []}).startswith("ERROR:"))
        handler.assert_not_called()

    def test_union_and_enum_use_json_types(self):
        schema = {"type": "object", "properties": {
            "value": {"type": ["string", "null"]}, "choice": {"enum": [{"flag": True}]}},
        }
        box, handler = self.box(schema)
        self.assertEqual(box.run("probe", {"value": None, "choice": {"flag": True}}), "ok")
        handler.reset_mock()
        self.assertTrue(box.run("probe", {"value": None, "choice": {"flag": 1}}).startswith("ERROR:"))
        handler.assert_not_called()

    def test_complex_schema_nodes_are_left_to_tool_validator(self):
        schema = {"type": "object", "properties": {
            "ref": {"$ref": "#/$defs/value"},
            "branch": {"oneOf": [{"type": "string"}, {"type": "number"}]},
            "labels": {"type": "object", "patternProperties": {"^x-": {"type": "string"}},
                       "additionalProperties": False},
        }}
        box, handler = self.box(schema)
        args = {"ref": {"nested": True}, "branch": "text", "labels": {"x-tag": "yes"}}
        self.assertEqual(box.run("probe", args), "ok")
        handler.assert_called_once_with(**args)

    def test_boolean_schema_const_and_nonfinite_numbers(self):
        cases = [
            ({"const": True}, 1), ({"enum": [1]}, True),
            ({"type": "number"}, float("inf")),
            ({"type": "integer"}, float("nan")),
            (False, "anything"),
        ]
        for declared, value in cases:
            with self.subTest(declared=declared):
                box, handler = self.box({"type": "object", "properties": {"value": declared}})
                self.assertTrue(box.run("probe", {"value": value}).startswith("ERROR:"))
                handler.assert_not_called()

    def test_sdk_handler_retains_its_own_strict_validation(self):
        box, handler = self.box(coerce_arguments=False)
        args = {"mode": "read", "count": "2"}
        box.run("probe", args)
        handler.assert_called_once_with(**args)

    def test_approval_rewrite_is_checked_before_side_effect(self):
        self.config.auto_approve = False
        box, handler = self.box()
        agent = Agent(self.config, box, registry=Registry.for_client(FakeClient([])),
                      approver=lambda *_: Allow(updated_args={"mode": "write", "count": 1}))
        result = agent._execute({"id": "c1", "type": "function", "function": {
            "name": "probe", "arguments": json.dumps({"mode": "read", "count": 1})}})
        self.assertIn("ERROR:", result["content"])
        handler.assert_not_called()

    def test_mcp_search_and_registered_paths_validate_before_forwarding(self):
        for search in (False, True):
            with self.subTest(search=search):
                self.config.mcp_tool_search = search
                handler = mock.Mock(return_value="ok")
                remote = SimpleNamespace(name="mcp__fake__probe", description="probe",
                    parameters=SCHEMA, handler=handler, check_fn=lambda: True)
                view = SimpleNamespace(ready_tools=lambda: [remote])
                box = Toolbox(self.config, mcp_view=view)
                name = "use_tool" if search else remote.name
                def invoke(args):
                    return box.run(name, {"tool_name": remote.name, "tool_input": args} if search else args)
                for args in ({"mode": "read"}, {"mode": "write", "count": 1},
                             {"mode": "read", "count": 1, "extra": "no"}):
                    self.assertTrue(invoke(args).startswith("ERROR:"))
                handler.assert_not_called()
                self.assertEqual(invoke({"mode": "read", "count": "2"}), "ok")
                handler.assert_called_once_with(mode="read", count=2)
