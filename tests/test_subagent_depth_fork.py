"""子 agent 深度上限（默认不套娃、可显式有界放开）+ fork 继承（完整上下文逐字带走）。"""

from __future__ import annotations

import contextlib
import io
import unittest

from xiaoyu.agent import Agent
from xiaoyu.agents import AgentSpec, RunStore, load_agent_specs, make_subagent_tool
from xiaoyu.tools import Toolbox

from .test_agent_paths import AgentTestCase
from .test_agents_spec import GOOD_SPEC, text_turn, write_spec, _sub_tool_call
from . import test_agents_spec as spec_mod
from unittest import mock


class DepthConfigTest(AgentTestCase):
    def test_main_agent_mounts_subagent_tools(self):
        """主会话 depth=0 < max=1：委托工具照挂（回归保护）。"""
        write_spec(self.root / ".xiaoyu" / "agents", "doc", GOOD_SPEC)
        self.config.enable_agents = True
        with mock.patch.object(spec_mod.agents_mod if hasattr(spec_mod, "agents_mod") else __import__("xiaoyu.agents", fromlist=["x"]),
                               "user_config_dir", lambda: self.root / "cfg"):
            agent = self.build([])
        self.assertIsNotNone(agent.toolbox.get("doc"))
        self.assertIsNotNone(agent.toolbox.get("qixiang"))

    def test_default_no_nesting(self):
        """默认 max_depth=1：子 agent（depth 会被设为 1）不挂任何委托工具。"""
        self.config.enable_agents = True
        self.config.subagent_depth = 1  # 模拟"我就是一个子 agent"
        agent = Agent(self.config, Toolbox(self.config), registry=self.build([]).registry,
                      allow_nesting=False)
        self.assertIsNone(agent.toolbox.get("qixiang"))

    def test_opt_in_bounded_nesting_mounts_at_permitted_depth(self):
        """XIAOYU_SUBAGENT_MAX_DEPTH=2：depth 0/1 可挂，depth 2 不可。"""
        import xiaoyu.agents as agents_mod
        self.config.enable_agents = True
        self.config.subagent_max_depth = 2
        write_spec(self.root / ".xiaoyu" / "agents", "doc", GOOD_SPEC)
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            # depth 1 < 2 → 挂
            self.config.subagent_depth = 1
            a1 = Agent(self.config, Toolbox(self.config), registry=self.build([]).registry,
                       allow_nesting=True)
            self.assertIsNotNone(a1.toolbox.get("doc"))
            # depth 2 == max → 不挂（到顶即止）
            self.config.subagent_depth = 2
            a2 = Agent(self.config, Toolbox(self.config), registry=self.build([]).registry,
                       allow_nesting=True)
            self.assertIsNone(a2.toolbox.get("doc"))


class ForkInheritTest(AgentTestCase):
    def _run(self, inherit: str) -> list:
        """跑一段父会话（含工具轮）再委托，返回子 agent 的起始历史（存档记录）。"""
        from .test_agent_paths import call_fragment, chunk, usage_chunk

        spec = AgentSpec(
            name="worker", description="干活", system_prompt="接着干，工作区 {workspace}",
            tools=("read_file", "grep", "list_files"), inherit=inherit,
        )
        store = RunStore()
        # 主脚本：先一轮工具调用产生 tool 消息（fork 要带上、distilled 要剔掉），
        # 再委托 worker，worker 一句结论，主收尾
        agent = self.build([
            [chunk(tool_calls=[call_fragment(0, "c1", "read_file", '{"path": "calc.py"}')]), usage_chunk(10, 2)],
            [chunk(content="读到了 add 函数"), usage_chunk(10, 2)],   # 主：第一轮收尾
            _sub_tool_call("worker", "接着干"),                      # 主：委托
            text_turn("子 agent 结论"),                              # 子
            text_turn("主收尾"),
        ])
        agent.toolbox.register(
            make_subagent_tool(
                spec, self.config, agent.registry, agent.usage, agent.sink,
                agent.approver, agent.permissions, runs=store,
                parent_history=lambda: agent.messages,
            )
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("第一件事")
            agent.send("委托一下")
        run = next(iter(store.values()))
        return run.messages

    def test_fork_seeds_full_verbatim_history(self):
        msgs = self._run("fork")
        roles = [m["role"] for m in msgs]
        #  完整逐字：父的 user 原话、assistant 工具调用、tool 结果都在
        self.assertIn("tool", roles)
        self.assertTrue(any(m.get("content") == "第一件事" for m in msgs))
        self.assertTrue(any(m["role"] == "assistant" and m.get("tool_calls") for m in msgs))

    def test_distilled_strips_tool_turns(self):
        msgs = self._run("distilled")
        roles = [m["role"] for m in msgs]
        #  精简：工具过程被剔除，只留 user 原话 + 最终答复
        self.assertNotIn("tool", roles)
        self.assertFalse(any(m["role"] == "assistant" and m.get("tool_calls") for m in msgs))
        self.assertTrue(any(m.get("content") == "第一件事" for m in msgs))

    def test_none_carries_no_parent_history(self):
        msgs = self._run("none")
        #  不继承：起始历史只有 system + 委托任务，没有父的"第一件事"
        self.assertFalse(any(m.get("content") == "第一件事" for m in msgs))

    def test_fork_and_distilled_are_valid_spec_values(self):
        import xiaoyu.agents as agents_mod
        for mode in ("fork", "distilled", "none"):
            write_spec(self.root / ".xiaoyu" / "agents", f"a_{mode}",
                       GOOD_SPEC + (f'inherit = "{mode}"\n' if mode != "none" else ""))
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            specs, problems = load_agent_specs(self.root)
        self.assertEqual(problems, [])
        self.assertEqual({s.name: s.inherit for s in specs},
                         {"a_fork": "fork", "a_distilled": "distilled", "a_none": ""})

    def test_bad_inherit_rejected(self):
        import xiaoyu.agents as agents_mod
        write_spec(self.root / ".xiaoyu" / "agents", "bad", GOOD_SPEC + 'inherit = "clone"\n')
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            specs, problems = load_agent_specs(self.root)
        self.assertEqual(specs, [])
        self.assertTrue(any("inherit" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
