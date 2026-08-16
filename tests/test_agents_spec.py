"""声明式 subagent（agents/*.toml）的测试。不打网络。

spec 解析与安全边界（权限不因声明放大）各卡一处；委托执行用假 client：
主 agent 的脚本先出 tool_call 调子 agent，子 agent 消费后续脚本轮。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import agents as agents_mod
from xiaoyu.agents import AgentSpec, load_agent_specs, make_subagent_tool
from xiaoyu.permissions import Permissions, parse_rule

from .test_agent_paths import AgentTestCase, call_fragment, chunk


def write_spec(directory: Path, name: str, body: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.toml"
    path.write_text(body, encoding="utf-8")
    return path


GOOD_SPEC = """
description = "查文档的子 agent"
system_prompt = "你只查不改。工作区：{workspace}"
tools = ["read_file", "grep", "list_files"]
"""


class LoadSpecTest(AgentTestCase):
    def _load(self):
        #  用户级目录指进临时区，绝不扫真机配置
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            return load_agent_specs(self.root)

    def test_good_spec_loaded(self):
        write_spec(self.root / ".xiaoyu" / "agents", "doc_reader", GOOD_SPEC)
        specs, problems = self._load()
        self.assertEqual(problems, [])
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].name, "doc_reader")
        self.assertTrue(specs[0].readonly)
        self.assertEqual(specs[0].source, "workspace")

    def test_user_level_wins_name_conflict(self):
        write_spec(self.root / "cfg" / "agents", "dup", GOOD_SPEC)
        write_spec(self.root / ".xiaoyu" / "agents", "dup", GOOD_SPEC)
        specs, problems = self._load()
        self.assertEqual([s.source for s in specs], ["user"])
        self.assertTrue(any("同名" in p for p in problems))

    def test_bad_specs_skipped_with_problems(self):
        base = self.root / ".xiaoyu" / "agents"
        write_spec(base, "no_desc", 'system_prompt = "x"\ntools = ["grep"]\n')
        write_spec(
            base, "bad_tool",
            'description = "x"\nsystem_prompt = "y"\ntools = ["explore"]\n',
        )
        write_spec(base, "BadName", GOOD_SPEC)
        specs, problems = self._load()
        self.assertEqual(specs, [])
        self.assertEqual(len(problems), 3)

    def test_write_tools_not_readonly(self):
        spec = AgentSpec(
            name="coder", description="d", system_prompt="s",
            tools=("read_file", "write_file"),
        )
        self.assertFalse(spec.readonly)


def _sub_tool_call(name: str, task: str) -> list:
    return [chunk(tool_calls=[call_fragment(0, "c1", name, json.dumps({"task": task}))])]


def text_turn(text: str) -> list:
    return [chunk(content=text)]


class DelegationTest(AgentTestCase):
    """挂载 + 委托端到端（假 client 脚本按调用顺序被主/子 agent 依次消费）。"""

    def _mount(self, agent, spec: AgentSpec):
        agent.toolbox.register(
            make_subagent_tool(
                spec, self.config, agent.registry, agent.usage, agent.sink,
                agent.approver, agent.permissions,
            )
        )

    def test_readonly_delegation_round_trip(self):
        spec = AgentSpec(
            name="doc_reader", description="查文档", system_prompt="只查不改，工作区 {workspace}",
            tools=("read_file", "grep", "list_files"),
        )
        agent = self.build(
            [
                _sub_tool_call("doc_reader", "查 calc.py 里有什么函数"),  # 主：委托
                text_turn("calc.py 里有 add 函数"),                      # 子：结论
                text_turn("查完了：有 add"),                             # 主：收尾
            ]
        )
        self._mount(agent, spec)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("看看文档")
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("doc_reader 子 agent 的结论", tool_msgs[-1]["content"])
        self.assertIn("add 函数", tool_msgs[-1]["content"])
        self.assertEqual(agent.last_assistant_text(), "查完了：有 add")

    def test_parent_deny_rules_pierce_into_subagent(self):
        """deny 规则穿透：子 agent 声明了 bash 也拦得住（bypass-immune）。"""
        spec = AgentSpec(
            name="runner", description="跑命令", system_prompt="工作区 {workspace}",
            tools=("read_file", "bash"),
        )
        permissions = Permissions(self.root, [parse_rule("deny bash(curl *)")])
        agent = self.build(
            [
                _sub_tool_call("runner", "下载一个东西"),
                [chunk(tool_calls=[call_fragment(0, "s1", "bash", '{"command": "curl x.com"}')])],
                text_turn("被拦了，做不了"),
                text_turn("子 agent 说被拦了"),
            ],
            permissions=permissions,
        )
        self._mount(agent, spec)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("去下载")
        #  子 agent 的 bash 被 deny 规则拦下（trace 在子 agent 里，主历史里看结论）
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("被拦了", tool_msgs[-1]["content"])

    def test_agent_mounts_specs_from_workspace(self):
        """Agent 构造时自动挂载工作区 spec（enable_agents 门控）。"""
        write_spec(self.root / ".xiaoyu" / "agents", "doc_reader", GOOD_SPEC)
        self.config.enable_agents = True
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            agent = self.build([])
        self.assertIsNotNone(agent.toolbox.get("doc_reader"))
        #  子 agent 不套娃：allow_explore=False 的构造不挂
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            sub = self.build([], allow_explore=False)
        self.assertIsNone(sub.toolbox.get("doc_reader"))

    def test_name_collision_with_existing_tool_skipped(self):
        write_spec(
            self.root / ".xiaoyu" / "agents", "bash",
            'description = "假冒"\nsystem_prompt = "x"\ntools = ["grep"]\n',
        )
        self.config.enable_agents = True
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            agent = self.build([])
        #  bash 仍是内置工具，没有被 spec 顶掉
        self.assertTrue(callable(agent.toolbox.get("bash").handler))
        self.assertIn("bash", agent.toolbox.names())


if __name__ == "__main__":
    unittest.main()
