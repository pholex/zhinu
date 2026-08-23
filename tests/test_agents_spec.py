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


class ContextScopeNoteTest(AgentTestCase):
    """子 agent 上下文范围点名：工作区有项目约定文件、而没喂给子 agent 时，
    在子 agent 的 system prompt 里明确"这些约定存在但你没拿到"，防止它拿默认
    当项目约定去猜。文件不存在时不加这句噪声。"""

    def _run_and_capture_system(self, spec: AgentSpec) -> str:
        from xiaoyu.agents import execute_delegation

        agent = self.build([text_turn("done")])  # 子 agent 只需一轮结论
        #  on_agent 在构造后、system_text 落定前触发，只能拿句柄；system prompt
        #  在 execute_delegation 返回后读（那时 messages[0] 已是 spec+范围点名）
        handle: list = []
        with contextlib.redirect_stdout(io.StringIO()):
            execute_delegation(
                spec, self.config, agent.registry, agent.usage, agent.sink,
                agent.approver, agent.permissions, {},
                task="做点事",
                on_agent=handle.append,
            )
        return handle[0].messages[0]["content"]

    def _readonly_spec(self, prompt: str = "只查不改，工作区 {workspace}") -> AgentSpec:
        return AgentSpec(
            name="reader", description="查", system_prompt=prompt,
            tools=("read_file", "grep", "list_files"),
        )

    def test_note_added_when_project_docs_exist(self):
        (self.root / "AGENTS.md").write_text("提交不加 Co-Authored-By", encoding="utf-8")
        system = self._run_and_capture_system(self._readonly_spec())
        self.assertIn("[上下文范围]", system)
        self.assertIn("项目约定", system)

    def test_no_note_when_no_project_docs(self):
        #  裸工作区没有约定文件 → 不加噪声
        system = self._run_and_capture_system(self._readonly_spec())
        self.assertNotIn("[上下文范围]", system)

    def test_note_suppressed_when_spec_handles_agents_md(self):
        #  spec 作者已在 prompt 里点了 AGENTS.md → 视为已处理，不重复加
        (self.root / "AGENTS.md").write_text("x", encoding="utf-8")
        system = self._run_and_capture_system(
            self._readonly_spec("先读 AGENTS.md 再动手，工作区 {workspace}")
        )
        self.assertNotIn("[上下文范围]", system)


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


class DistillHistoryTest(unittest.TestCase):
    """inherit = "distilled"：父会话历史 → 只剩用户原话与最终答复的精简副本。"""

    def _history(self):
        from xiaoyu.agent import WRAPUP_INSTRUCTION
        from xiaoyu.compaction import CONTEXT_PREFIX

        return [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "任务一" + CONTEXT_PREFIX + "【摘要】旧摘要"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}], "_reasoning": "想"},
            {"role": "tool", "tool_call_id": "c1", "content": "工具输出"},
            {"role": "user", "content": "<system-reminder>\n通知\n</system-reminder>"},
            {"role": "assistant", "content": "答一"},
            {"role": "user", "content": WRAPUP_INSTRUCTION},
            {"role": "user", "content": "[系统提示] 进入 plan mode /tmp/x.md"},
            {"role": "user", "content": [{"type": "text", "text": "任务二"}, {"type": "image_url", "image_url": {"url": "data:x"}}]},
            {"role": "assistant", "content": "答二"},
            {"role": "user", "content": "任务三"},
        ]

    def test_keeps_user_voice_and_final_answers_only(self):
        from xiaoyu.agent import SYNTHETIC_USER_TEXTS

        out = agents_mod.distill_history(
            self._history(), max_tokens=100_000, synthetic_texts=SYNTHETIC_USER_TEXTS
        )
        self.assertEqual(
            out,
            [
                {"role": "user", "content": "任务一"},
                {"role": "assistant", "content": "答一"},
                {"role": "user", "content": "任务二[图片]"},
                {"role": "assistant", "content": "答二"},
                {"role": "user", "content": "任务三"},
            ],
        )
        #  纯函数：再蒸馏一次不变
        self.assertEqual(agents_mod.distill_history(out, max_tokens=100_000), out)

    def test_budget_cuts_oldest_whole_turns(self):
        history = self._history()
        full = agents_mod.distill_history(history, max_tokens=100_000)
        #  只够装最后两轮（任务二/答二 + 任务三）：最老一轮整轮丢、不留半轮
        from xiaoyu import tokens

        last_two = tokens.estimate_messages(full[2:])
        out = agents_mod.distill_history(history, max_tokens=last_two)
        self.assertEqual(out, full[2:])
        self.assertEqual(agents_mod.distill_history(history, max_tokens=0), [])
        self.assertEqual(agents_mod.distill_history([], max_tokens=100), [])


class InheritSpecTest(AgentTestCase):
    def _load(self):
        with mock.patch.object(agents_mod, "user_config_dir", lambda: self.root / "cfg"):
            return load_agent_specs(self.root)

    def test_inherit_parsed_and_validated(self):
        base = self.root / ".xiaoyu" / "agents"
        write_spec(base, "aa", GOOD_SPEC + 'inherit = "distilled"\n')
        write_spec(base, "bb", GOOD_SPEC + 'inherit = "none"\n')
        write_spec(base, "cc", GOOD_SPEC + 'inherit = "full"\n')
        specs, problems = self._load()
        self.assertEqual({s.name: s.inherit for s in specs}, {"aa": "distilled", "bb": ""})
        self.assertTrue(any("cc.toml" in p and "inherit" in p for p in problems))

    def test_distilled_delegation_seeds_child_history(self):
        spec = AgentSpec(
            name="doc_reader", description="查文档", system_prompt="只查不改，工作区 {workspace}",
            tools=("read_file", "grep", "list_files"), inherit="distilled",
        )
        runs: dict = {}
        agent = self.build(
            [
                text_turn("先回一句"),                                   # 主：第一轮答复
                _sub_tool_call("doc_reader", "查 calc.py"),              # 主：第二轮委托
                text_turn("calc.py 里有 add"),                           # 子：结论
                text_turn("查完了"),                                     # 主：收尾
            ]
        )
        agent.toolbox.register(
            make_subagent_tool(
                spec, self.config, agent.registry, agent.usage, agent.sink,
                agent.approver, agent.permissions, runs=runs,
                parent_history=lambda: agent.messages,
            )
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("第一句")
            agent.send("看看文档")
        record = next(iter(runs.values()))
        roles = [(m["role"], m.get("content")) for m in record.messages[:4]]
        self.assertEqual(roles[0][0], "system")
        self.assertIn("[继承上下文]", roles[0][1])
        #  父会话两轮的用户原话 + 第一轮最终答复进了子 agent 起始历史，
        #  委托任务紧随其后；父 agent 发起委托的那条 tool_call 没带过来
        self.assertEqual(roles[1:4], [("user", "第一句"), ("assistant", "先回一句"), ("user", "看看文档")])
        self.assertEqual(record.messages[4]["content"], "查 calc.py")
        self.assertFalse(any(m.get("tool_calls") for m in record.messages[:5]))

    def test_default_inherit_leaves_child_blank(self):
        spec = AgentSpec(
            name="doc_reader", description="查文档", system_prompt="工作区 {workspace}",
            tools=("read_file",),
        )
        runs: dict = {}
        agent = self.build([_sub_tool_call("doc_reader", "查"), text_turn("子答"), text_turn("主答")])
        agent.toolbox.register(
            make_subagent_tool(
                spec, self.config, agent.registry, agent.usage, agent.sink,
                agent.approver, agent.permissions, runs=runs,
                parent_history=lambda: agent.messages,
            )
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("看看")
        record = next(iter(runs.values()))
        self.assertEqual(record.messages[1]["content"], "查")
        self.assertNotIn("[继承上下文]", record.messages[0]["content"])
