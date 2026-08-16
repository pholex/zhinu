"""主循环、中断恢复、摘要回退链、REPL 命令的测试。

这些路径此前只在真实运行里"看起来没问题"，没有测试锁住。
用假 client 注入，不打网络。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.agent import Agent
from xiaoyu.cli import handle_slash
from xiaoyu.config import Config
from xiaoyu.providers import Registry
from xiaoyu.tools import Toolbox


# ---------- 假 client ----------


def chunk(content: str | None = None, tool_calls=None, usage=None):
    """造一个流式 chunk。字段形状对齐 openai SDK 实际用到的那几个。"""
    delta = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    choice = types.SimpleNamespace(delta=delta)
    return types.SimpleNamespace(choices=[choice], usage=usage)


def usage_chunk(prompt: int, completion: int):
    return types.SimpleNamespace(
        choices=[],
        usage=types.SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


def call_fragment(index: int, call_id: str | None, name: str | None, arguments: str | None):
    function = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(index=index, id=call_id, function=function)


class FakeCompletions:
    def __init__(self, script: list) -> None:
        #  script 里每一项是 list[chunk]（流式）、Exception（抛错）或普通 response 对象
        self.script = list(script)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("假 client 的脚本用完了，说明调用次数超出预期")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return iter(item) if isinstance(item, list) else item


class FakeClient:
    def __init__(self, script: list) -> None:
        self.completions = FakeCompletions(script)
        self.chat = types.SimpleNamespace(completions=self.completions)


def text_response(content: str, prompt: int = 100, completion: int = 20):
    message = types.SimpleNamespace(content=content)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message)],
        usage=types.SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion),
    )


class AgentTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        (self.root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        self.config = Config(
            base_url="http://unused",
            model="main-model",
            summary_model="cheap-model",
            explore_model="cheap-model",
            workspace=self.root,
            auto_approve=True,
            #  测试必须与跑测试机器上装的技能库/插件隔离
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, script: list, **kwargs) -> Agent:
        client = FakeClient(script)
        agent = Agent(self.config, Toolbox(self.config), registry=Registry.for_client(client), **kwargs)
        self.client = client
        return agent


# ---------- 主循环 ----------


class TestToolCallLoop(AgentTestCase):
    def test_streamed_tool_call_fragments_are_assembled(self) -> None:
        """arguments 是分片下发的，拼错就会 JSON 解析失败 —— 这是流式最易错的一环。"""
        first = [
            chunk(tool_calls=[call_fragment(0, "call_1", "read_file", '{"pa')]),
            chunk(tool_calls=[call_fragment(0, None, None, 'th": "cal')]),
            chunk(tool_calls=[call_fragment(0, None, None, 'c.py"}')]),
            usage_chunk(500, 30),
        ]
        second = [chunk(content="文件里有 add 函数"), usage_chunk(700, 40)]
        agent = self.build([first, second])

        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("看一下 calc.py")

        self.assertEqual([entry["tool"] for entry in agent.trace], ["read_file"])
        self.assertTrue(agent.trace[0]["ok"])
        self.assertIn("def add", agent.trace[0]["output"])
        #  消息序列：system, user, assistant(tool_calls), tool, assistant
        self.assertEqual(
            [m["role"] for m in agent.messages],
            ["system", "user", "assistant", "tool", "assistant"],
        )
        self.assertEqual(agent.messages[3]["tool_call_id"], "call_1")
        self.assertEqual(agent.usage.by_model["gateway/main-model"].calls, 2)
        self.assertEqual(agent.usage.prompt_tokens, 1200)

    def test_parallel_tool_calls(self) -> None:
        first = [
            chunk(
                tool_calls=[
                    call_fragment(0, "a", "read_file", '{"path": "calc.py"}'),
                    call_fragment(1, "b", "list_files", '{"pattern": "*.py"}'),
                ]
            )
        ]
        agent = self.build([first, [chunk(content="都看完了")]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("并行看两个")
        self.assertEqual([entry["tool"] for entry in agent.trace], ["read_file", "list_files"])
        self.assertEqual([m["role"] for m in agent.messages][-3:], ["tool", "tool", "assistant"])

    def test_malformed_arguments_are_reported_not_crashed(self) -> None:
        first = [chunk(tool_calls=[call_fragment(0, "x", "read_file", "{不是合法 JSON")])]
        agent = self.build([first, [chunk(content="我重试")]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("试试")
        self.assertIn("不是合法 JSON", agent.messages[3]["content"])

    def test_accept_note_rides_the_tool_result(self) -> None:
        """approver 返回 (True, 附言)（TUI 确认框的 Tab）：附言拼进本次
        tool result 回灌模型——不另插 user 消息（那会破坏 tool 结果与
        tool_calls 的相邻不变量）。"""
        self.config.auto_approve = False
        first = [
            chunk(tool_calls=[call_fragment(0, "c1", "bash", '{"command": "echo hello-note"}')])
        ]
        agent = self.build(
            [first, [chunk(content="好")]],
            approver=lambda name, args: (True, "顺带留意退出码"),
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("跑一下")
        tool_message = agent.messages[3]
        self.assertEqual(tool_message["role"], "tool")
        self.assertIn("hello-note", tool_message["content"])
        self.assertIn("顺带留意退出码", tool_message["content"])
        #  消息序列没有被附言打乱：system, user, assistant(tool_calls), tool, assistant
        self.assertEqual(
            [m["role"] for m in agent.messages],
            ["system", "user", "assistant", "tool", "assistant"],
        )

    def test_max_iterations_wraps_up_with_summary(self) -> None:
        #  模型一直要求调工具，必须被轮次上限刹住，且要产出收尾总结而不是静默截断
        self.config.max_iterations = 3
        forever = [
            [chunk(tool_calls=[call_fragment(0, f"c{n}", "read_file", '{"path": "calc.py"}')])]
            for n in range(3)
        ]
        wrapup = [chunk(content="已完成 A，剩 B，建议继续")]
        agent = self.build(forever + [wrapup])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send("无限循环")
        self.assertIn("已达到单轮工具调用上限", buffer.getvalue())
        self.assertEqual(agent.last_assistant_text(), "已完成 A，剩 B，建议继续")
        #  收尾调用必须不带 tools——只许说话不许继续干活
        self.assertNotIn("tools", self.client.completions.calls[-1])

    def test_repeated_identical_calls_warned_then_blocked(self) -> None:
        """打转检测：相同 (工具, 参数) 连续第 3 次附加提示，第 5 次拒绝执行。"""
        agent = self.build([])
        call = {
            "id": "c1",
            "function": {"name": "list_files", "arguments": '{"pattern": "*.py"}'},
        }
        outputs = []
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(5):
                outputs.append(agent._execute(call)["content"])
        self.assertNotIn("[提示]", outputs[1])
        self.assertIn("连续第 3 次", outputs[2])
        self.assertIn("拒绝执行", outputs[4])
        #  换了参数就重置
        other = {
            "id": "c2",
            "function": {"name": "list_files", "arguments": '{"pattern": "*.md"}'},
        }
        with contextlib.redirect_stdout(io.StringIO()):
            fresh = agent._execute(other)["content"]
        self.assertNotIn("拒绝执行", fresh)
        self.assertNotIn("[提示]", fresh)


# ---------- 中断恢复 ----------


class TestInterruptRecovery(AgentTestCase):
    def test_dangling_tool_calls_are_closed(self) -> None:
        """Ctrl-C 打在工具执行前时，assistant 的 tool_calls 没有对应结果，
        下一次请求会直接 400。close_open_tool_calls 必须补齐。"""
        agent = self.build([])
        agent.messages.append({"role": "user", "content": "改点东西"})
        agent.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                    {"id": "t2", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                ],
            }
        )
        agent.close_open_tool_calls("用户按 Ctrl-C 中断了本轮。")

        tool_messages = [m for m in agent.messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_messages], ["t1", "t2"])
        self.assertIn("中断", tool_messages[0]["content"])

    def test_noop_when_nothing_dangling(self) -> None:
        agent = self.build([])
        agent.messages.append({"role": "assistant", "content": "说完了"})
        before = len(agent.messages)
        agent.close_open_tool_calls("中断")
        self.assertEqual(len(agent.messages), before, "没有悬空调用时不该乱加消息")

    def test_mid_batch_interrupt_repaired(self) -> None:
        """中断打在批量工具执行到一半：t1 已有结果、t2 悬空，
        此时最后一条是 tool 消息——只看最后一条 assistant 的旧实现修不了这种。"""
        agent = self.build([])
        agent.messages.append({"role": "user", "content": "改点东西"})
        agent.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                    {"id": "t2", "type": "function", "function": {"name": "bash", "arguments": "{}"}},
                ],
            }
        )
        agent.messages.append({"role": "tool", "tool_call_id": "t1", "content": "已完成"})
        agent.close_open_tool_calls("用户中断")

        tool_messages = [m for m in agent.messages if m["role"] == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_messages], ["t1", "t2"])
        #  已有的结果不许被动
        self.assertEqual(tool_messages[0]["content"], "已完成")
        #  补的结果必须紧跟在 tool 段末尾（相邻性），不能落在别处
        roles = [m["role"] for m in agent.messages]
        self.assertEqual(roles[-2:], ["tool", "tool"])

    def test_repair_scans_all_history_not_just_tail(self) -> None:
        """悬空调用在历史中段（后面已经又有别的消息）也要补齐。"""
        agent = self.build([])
        agent.messages.append({"role": "user", "content": "第一轮"})
        agent.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "old", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
                ],
            }
        )
        #  悬空之后又插入了一条 user（比如历史被手工修补过）
        agent.messages.append({"role": "user", "content": "第二轮"})
        agent.close_open_tool_calls("补齐")
        roles = [m["role"] for m in agent.messages]
        self.assertEqual(roles, ["system", "user", "assistant", "tool", "user"])
        self.assertEqual(agent.messages[3]["tool_call_id"], "old")

    def test_partial_stream_interrupt_keeps_spoken_text(self) -> None:
        """Ctrl-C 打在流式中途：已经说出的半截话必须入历史，不能整轮消失。"""

        def interrupted_stream():
            yield chunk(content="我先分析一下")
            raise KeyboardInterrupt

        agent = self.build([interrupted_stream()])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                agent.send("做点什么")

        last = agent.messages[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertIn("我先分析一下", last["content"])
        self.assertIn("被用户中断", last["content"])
        self.assertNotIn("tool_calls", last, "拼了一半的 tool_calls 必须丢弃")

    def test_can_continue_after_interrupt(self) -> None:
        """补齐之后必须能继续对话（序列合法）。"""
        agent = self.build([[chunk(content="继续")]])
        agent.messages.append({"role": "user", "content": "第一轮"})
        agent.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "t1", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
                ],
            }
        )
        agent.close_open_tool_calls("用户中断")
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("第二轮")
        roles = [m["role"] for m in agent.messages]
        #  每个 assistant(tool_calls) 后面都紧跟 tool 结果
        for index, role in enumerate(roles):
            if role == "assistant" and agent.messages[index].get("tool_calls"):
                self.assertEqual(roles[index + 1], "tool")


# ---------- 项目级指令文件 ----------


class TestProjectInstructions(AgentTestCase):
    def test_agents_md_enters_system_prompt(self) -> None:
        (self.root / "AGENTS.md").write_text("跑测试用 make check", encoding="utf-8")
        agent = self.build([])
        self.assertIn("跑测试用 make check", agent.messages[0]["content"])
        self.assertIn("AGENTS.md", agent.messages[0]["content"])

    def test_priority_order_first_hit_wins(self) -> None:
        (self.root / "AGENTS.md").write_text("A 文件", encoding="utf-8")
        (self.root / "CLAUDE.md").write_text("C 文件", encoding="utf-8")
        agent = self.build([])
        self.assertIn("A 文件", agent.messages[0]["content"])
        self.assertNotIn("C 文件", agent.messages[0]["content"])

    def test_claude_md_as_fallback(self) -> None:
        (self.root / "CLAUDE.md").write_text("C 文件", encoding="utf-8")
        agent = self.build([])
        self.assertIn("C 文件", agent.messages[0]["content"])

    def test_no_doc_no_section(self) -> None:
        agent = self.build([])
        self.assertNotIn("项目指令", agent.messages[0]["content"])

    def _nested_workspace(self) -> Path:
        """root(.git + AGENTS.md) / sub / ws(XIAOYU.md)，返回 ws。"""
        (self.root / ".git").mkdir(exist_ok=True)
        (self.root / "AGENTS.md").write_text("根目录总规范", encoding="utf-8")
        ws = self.root / "sub" / "ws"
        ws.mkdir(parents=True)
        (ws / "XIAOYU.md").write_text("子项目细则", encoding="utf-8")
        return ws

    def test_multi_level_collects_root_to_leaf(self) -> None:
        """monorepo 子目录启动：根规范 + 本地细则都进 prompt，深层靠后。"""
        self.config.workspace = self._nested_workspace()
        agent = self.build([])
        content = agent.messages[0]["content"]
        self.assertIn("根目录总规范", content)
        self.assertIn("子项目细则", content)
        self.assertLess(content.index("根目录总规范"), content.index("子项目细则"))
        self.assertIn("冲突时以靠后者为准", content)

    def test_leaf_first_budget_never_squeezes_deep_doc(self) -> None:
        from xiaoyu.agent import collect_project_docs

        ws = self._nested_workspace()
        (self.root / "AGENTS.md").write_text("规" * 20_000, encoding="utf-8")
        docs = collect_project_docs(ws, ("AGENTS.md", "XIAOYU.md"), cap=12_000)
        self.assertEqual(len(docs), 2)
        #  深层完整保留；浅层被截断而不是反过来
        self.assertEqual(docs[1][1], "子项目细则")
        self.assertIn("已截断", docs[0][1])
        self.assertLess(len(docs[0][1]), 12_100)

    def test_budget_exhausted_shallow_doc_omitted_with_note(self) -> None:
        from xiaoyu.agent import collect_project_docs

        ws = self._nested_workspace()
        (ws / "XIAOYU.md").write_text("细" * 12_000, encoding="utf-8")
        docs = collect_project_docs(ws, ("AGENTS.md", "XIAOYU.md"), cap=12_000)
        self.assertEqual(len(docs), 2)
        self.assertIn("整体省略", docs[0][1])
        self.assertNotIn("规", docs[0][1])

    def test_never_walks_past_git_root(self) -> None:
        from xiaoyu.agent import collect_project_docs

        #  root 没有 .git：层链就是工作区自己，root 的 AGENTS.md 不该被捡
        (self.root / "AGENTS.md").write_text("越界内容", encoding="utf-8")
        ws = self.root / "sub"
        ws.mkdir()
        (ws / "AGENTS.md").write_text("本地内容", encoding="utf-8")
        docs = collect_project_docs(ws, ("AGENTS.md",), cap=12_000)
        self.assertEqual([text for _, text in docs], ["本地内容"])


# ---------- 宿主注入的身份/人格（--append-system-prompt） ----------


class TestAppendSystemPrompt(AgentTestCase):
    def test_appended_when_set(self) -> None:
        self.config.append_system_prompt = "你现在是小助手，用户的私人助理"
        agent = self.build([])
        self.assertIn("你现在是小助手，用户的私人助理", agent.messages[0]["content"])

    def test_unset_leaves_prompt_unchanged(self) -> None:
        agent = self.build([])
        self.assertNotIn("小助手", agent.messages[0]["content"])

    def test_appears_before_project_instructions(self) -> None:
        """人格先于项目指令：宿主身份注入排在"我是谁"这一段，不是项目备注。"""
        (self.root / "AGENTS.md").write_text("跑测试用 make check", encoding="utf-8")
        self.config.append_system_prompt = "你现在是小助手"
        agent = self.build([])
        content = agent.messages[0]["content"]
        self.assertLess(content.index("你现在是小助手"), content.index("项目指令"))

    def test_oversized_doc_truncated(self) -> None:
        (self.root / "AGENTS.md").write_text("规" * 20_000, encoding="utf-8")
        agent = self.build([])
        self.assertIn("已截断", agent.messages[0]["content"])
        self.assertLess(len(agent.messages[0]["content"]), 20_000)


# ---------- 摘要回退链 ----------

#  过退化门（MIN_SUMMARY_CHARS）的合格摘要桩：真实摘要都远超这个长度
GOOD_SUMMARY = "这是摘要。" + "内容详实：任务目标、关键事实、文件改动、错误修复与后续计划俱全。" * 10


class TestSummaryFallback(AgentTestCase):
    def test_chain_order(self) -> None:
        agent = self.build([])
        self.assertEqual(
            [route.qualified for route in agent.summary_models()],
            ["gateway/cheap-model", "gateway/main-model"],
        )

    def test_falls_back_to_main_model_when_cheap_one_fails(self) -> None:
        agent = self.build([RuntimeError("网关 502"), text_response(GOOD_SUMMARY)])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            summary = agent._summarize("一段历史")

        self.assertEqual(summary, GOOD_SUMMARY)
        self.assertIn("回退", buffer.getvalue())
        #  两次调用分别用了两个模型，且都记了账
        used = [call["model"] for call in self.client.completions.calls]
        self.assertEqual(used, ["cheap-model", "main-model"])
        self.assertIn("gateway/main-model", agent.usage.by_model)

    def test_empty_summary_also_triggers_fallback(self) -> None:
        agent = self.build([text_response("   "), text_response(GOOD_SUMMARY)])
        with contextlib.redirect_stdout(io.StringIO()):
            summary = agent._summarize("一段历史")
        self.assertEqual(summary, GOOD_SUMMARY)

    def test_degenerate_short_summary_also_triggers_fallback(self) -> None:
        """非空但几十个字的应付式摘要与空摘要同罪。"""
        agent = self.build([text_response("摘要：修了个 bug。"), text_response(GOOD_SUMMARY)])
        with contextlib.redirect_stdout(io.StringIO()):
            summary = agent._summarize("一段历史")
        self.assertEqual(summary, GOOD_SUMMARY)

    def test_all_degenerate_raises_for_compaction_ladder(self) -> None:
        """全链退化时抛错——compact 的降级阶梯（砍半重试）靠这个异常触发。"""
        agent = self.build([text_response("太短"), text_response("还是太短")])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError) as ctx:
                agent._summarize("一段历史")
        self.assertIn("退化摘要", str(ctx.exception))

    def test_raises_when_all_models_fail(self) -> None:
        agent = self.build([RuntimeError("挂了1"), RuntimeError("挂了2")])
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(RuntimeError):
                agent._summarize("一段历史")

    def test_compaction_survives_total_summary_failure(self) -> None:
        """摘要全挂时，压缩必须放弃并保住历史，而不是把内容丢掉。"""
        agent = self.build([RuntimeError("挂了1"), RuntimeError("挂了2")])
        for index in range(10):
            agent.messages.append({"role": "user", "content": f"第 {index} 轮"})
            agent.messages.append({"role": "assistant", "content": f"回答 {index}"})
        before = list(agent.messages)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.maybe_compact(force=True)
        self.assertEqual(agent.messages, before)


# ---------- REPL 斜杠命令 ----------


class TestSlashCommands(AgentTestCase):
    def run_slash(self, agent: Agent, line: str) -> tuple[bool, str]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            should_exit = handle_slash(agent, line)
        return should_exit, buffer.getvalue()

    def test_exit(self) -> None:
        agent = self.build([])
        self.assertTrue(self.run_slash(agent, "/exit")[0])
        self.assertTrue(self.run_slash(agent, "/quit")[0])

    def test_help_and_tools(self) -> None:
        agent = self.build([])
        _, out = self.run_slash(agent, "/help")
        self.assertIn("/context", out)
        _, out = self.run_slash(agent, "/tools")
        for name in ("read_file", "str_replace", "bash", "explore"):
            self.assertIn(name, out)

    def test_model_switch(self) -> None:
        agent = self.build([])
        _, out = self.run_slash(agent, "/model")
        self.assertIn("main-model", out)
        self.run_slash(agent, "/model 另一个模型")
        self.assertEqual(agent.config.model, "另一个模型")

    def test_context_reports_budget_and_calibration(self) -> None:
        agent = self.build([])
        _, out = self.run_slash(agent, "/context")
        self.assertIn("压缩阈值", out)
        self.assertIn("摘要模型链", out)
        self.assertIn("cheap-model", out)

    def test_usage_before_any_call(self) -> None:
        agent = self.build([])
        _, out = self.run_slash(agent, "/usage")
        self.assertIn("还没有", out)

    def test_clear_keeps_system_prompt(self) -> None:
        agent = self.build([])
        agent.messages.append({"role": "user", "content": "一些历史"})
        self.run_slash(agent, "/clear")
        self.assertEqual(len(agent.messages), 1)
        self.assertEqual(agent.messages[0]["role"], "system")

    def test_compact_when_nothing_to_compact(self) -> None:
        agent = self.build([])
        _, out = self.run_slash(agent, "/compact")
        self.assertIn("跳过", out)

    def test_unknown_command(self) -> None:
        agent = self.build([])
        _, out = self.run_slash(agent, "/不存在")
        self.assertIn("未知命令", out)


class TestUpdatePlan(AgentTestCase):
    """update_plan：存储 + 固定返回；状态机约束靠 prompt 不靠代码。"""

    def plan_call(self, agent, arguments: str) -> str:
        call = {"id": "p1", "function": {"name": "update_plan", "arguments": arguments}}
        with contextlib.redirect_stdout(io.StringIO()):
            return agent._execute(call)["content"]

    def test_registered_by_default(self) -> None:
        agent = self.build([])
        self.assertIsNotNone(agent.toolbox.get("update_plan"))

    def test_happy_path_stores_and_returns_constant(self) -> None:
        agent = self.build([])
        content = self.plan_call(
            agent,
            '{"plan": [{"step": "读懂现有实现", "status": "completed"},'
            ' {"step": "改压缩逻辑", "status": "in_progress"},'
            ' {"step": "跑测试", "status": "pending"}]}',
        )
        #  返回恒为固定短语：不回显计划，不给模型复读的诱因
        self.assertEqual(content, "已更新计划")
        self.assertEqual(len(agent.plan), 3)
        self.assertEqual(agent.plan[1]["status"], "in_progress")

    def test_invalid_status_rejected(self) -> None:
        agent = self.build([])
        content = self.plan_call(agent, '{"plan": [{"step": "x", "status": "done"}]}')
        self.assertIn("ERROR", content)
        self.assertEqual(agent.plan, [])

    def test_unknown_fields_rejected(self) -> None:
        #  deny_unknown_fields：多传字段直接报错，逼模型守约
        agent = self.build([])
        content = self.plan_call(
            agent, '{"plan": [{"step": "x", "status": "pending", "note": "y"}]}'
        )
        self.assertIn("ERROR", content)
        content = self.plan_call(
            agent, '{"plan": [{"step": "x", "status": "pending"}], "priority": 1}'
        )
        self.assertIn("ERROR", content)

    def test_empty_plan_rejected(self) -> None:
        agent = self.build([])
        self.assertIn("ERROR", self.plan_call(agent, '{"plan": []}'))

    def test_clear_resets_plan(self) -> None:
        agent = self.build([])
        self.plan_call(agent, '{"plan": [{"step": "x", "status": "pending"}]}')
        agent.reset()
        self.assertEqual(agent.plan, [])


class TestRepairHistory(AgentTestCase):
    """发请求前的惰性历史修复：悬空补齐、孤儿删除。"""

    def test_dangling_tool_call_filled_before_request(self) -> None:
        script = [[chunk(content="继续"), usage_chunk(100, 10)]]
        agent = self.build(script)
        #  模拟中断残局：assistant 带 tool_calls 但没有 tool 结果
        agent.messages.append(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "orphan_call", "type": "function",
                     "function": {"name": "bash", "arguments": "{}"}}
                ],
            }
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("继续吧")
        roles = [m["role"] for m in agent.messages]
        #  修复后 assistant(tool_calls) 后面必须紧跟 tool 结果
        index = next(i for i, m in enumerate(agent.messages) if m.get("tool_calls"))
        self.assertEqual(agent.messages[index + 1]["role"], "tool")
        self.assertEqual(agent.messages[index + 1]["tool_call_id"], "orphan_call")
        self.assertNotIn("assistant", roles[index + 1 : index + 2])

    def test_orphan_tool_message_removed(self) -> None:
        script = [[chunk(content="好"), usage_chunk(100, 10)]]
        agent = self.build(script)
        #  孤儿 tool 消息（对应的 assistant 被压缩/改写掉了）会让请求 400
        agent.messages.append({"role": "tool", "tool_call_id": "ghost", "content": "旧结果"})
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertNotIn(
            "ghost", [m.get("tool_call_id") for m in agent.messages if m.get("role") == "tool"]
        )


class TestTokenAnchor(AgentTestCase):
    """token 记账：服务端 usage 锚点 + 只估算其后新增。"""

    def test_no_anchor_falls_back_to_estimate(self) -> None:
        agent = self.build([])
        self.assertIsNone(agent._anchor)
        self.assertGreater(agent.context_tokens(), 0)
        self.assertIn("纯本地估算", agent.context_source())

    def test_anchor_set_from_usage_and_used(self) -> None:
        script = [[chunk(content="好的"), usage_chunk(5000, 20)]]
        agent = self.build(script)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertIsNotNone(agent._anchor)
        anchor_tokens, anchor_index = agent._anchor
        self.assertEqual(anchor_tokens, 5000)
        #  锚点覆盖发请求时的消息数（system + user）
        self.assertEqual(anchor_index, 2)
        #  context = 锚点 + 之后新增（assistant 回复）的估算，必然 >= 5000
        self.assertGreaterEqual(agent.context_tokens(), 5000)
        self.assertIn("锚点", agent.context_source())

    def test_history_rewrite_invalidates_anchor(self) -> None:
        script = [[chunk(content="好的"), usage_chunk(5000, 20)]]
        agent = self.build(script)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        agent.reset()
        self.assertIsNone(agent._anchor)


class PurposeParamAgentTest(AgentTestCase):
    """__tool_use_purpose 在 agent 侧的两件事：执行前剥离、审批前展示。"""

    def test_purpose_stripped_before_handler(self) -> None:
        #  handler 收到未知 kwarg 会 TypeError——目的参数必须在执行前剥掉
        first = [
            chunk(
                tool_calls=[
                    call_fragment(
                        0,
                        "c1",
                        "read_file",
                        '{"path": "calc.py", "__tool_use_purpose": "看看加法函数"}',
                    )
                ]
            )
        ]
        agent = self.build([first, [chunk(content="看完了")]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("看文件")
        self.assertTrue(agent.trace[0]["ok"], agent.trace[0]["output"])
        self.assertNotIn("__tool_use_purpose", agent.trace[0]["args"])

    def test_purpose_shown_before_approval(self) -> None:
        self.config.auto_approve = False
        first = [
            chunk(
                tool_calls=[
                    call_fragment(
                        0,
                        "c1",
                        "bash",
                        '{"command": "echo hi", "__tool_use_purpose": "验证回显是否正常"}',
                    )
                ]
            )
        ]
        agent = self.build([first, [chunk(content="好了")]], approver=lambda n, a: True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send("跑一下")
        self.assertIn("验证回显是否正常", buffer.getvalue())


class EmptyReplyGuardTest(AgentTestCase):
    """空回复护栏：content 空且无 tool_calls 不能静默结束整轮。

    真实会话里 deepseek 改完 8 处代码后返回 content=null，用户面前一片空白、
    等了 27 分钟才试探性追问——这轮体验崩坏必须由 agent 兜住。
    """

    def test_empty_completion_retried_in_place(self) -> None:
        """第一层防线：空补全原地重发，不入历史、没有 nudge。"""
        agent = self.build([[chunk(content=None)], [chunk(content="重发后的结论")]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("改一下代码")
        self.assertEqual(len(self.client.completions.calls), 2)
        user_texts = [
            str(m.get("content")) for m in agent.messages if m.get("role") == "user"
        ]
        self.assertFalse(any("回复是空的" in text for text in user_texts))
        #  空的 assistant 消息没有被记进历史
        empties = [
            m for m in agent.messages
            if m.get("role") == "assistant" and not m.get("content") and not m.get("tool_calls")
        ]
        self.assertEqual(empties, [])
        self.assertEqual(agent.last_assistant_text(), "重发后的结论")

    def test_empty_reply_gets_nudged_after_retries(self) -> None:
        """原地重试耗尽（3 次全空）才轮到 nudge 这层兜底。"""
        empty = [chunk(content=None)]
        agent = self.build([empty, empty, empty, [chunk(content="补上的结论")]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("改一下代码")
        self.assertEqual(len(self.client.completions.calls), 4)
        user_texts = [
            str(m.get("content")) for m in agent.messages if m.get("role") == "user"
        ]
        self.assertTrue(any("回复是空的" in text for text in user_texts))
        self.assertEqual(agent.last_assistant_text(), "补上的结论")

    def test_persistent_empty_reply_ends_turn_loudly(self) -> None:
        empty = [chunk(content=None)]
        #  两个 send 层回合 × 各 3 次原地重试全空 → 显式告警收场
        agent = self.build([empty] * 6)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send("改一下代码")
        self.assertEqual(len(self.client.completions.calls), 6)
        self.assertIn("空回复", buffer.getvalue())


class PlanClaimGuardTest(AgentTestCase):
    """「宣称完成」护栏：验证类步骤标 completed 但没执行过任何命令时要被追问。"""

    def _plan(self, agent, plan):
        with contextlib.redirect_stdout(io.StringIO()):
            return agent._update_plan(plan)

    def test_unverified_test_step_is_challenged(self) -> None:
        agent = self.build([])
        result = self._plan(
            agent,
            [
                {"step": "编写HTML", "status": "completed"},
                {"step": "测试验证", "status": "completed"},
            ],
        )
        self.assertIn("[注意]", result)
        self.assertIn("测试验证", result)

    def test_verified_step_passes_quietly(self) -> None:
        agent = self.build([])
        self._plan(agent, [{"step": "测试验证", "status": "in_progress"}])
        agent._exec_evidence = 1  # 模拟这期间跑过一次 bash
        result = self._plan(agent, [{"step": "测试验证", "status": "completed"}])
        self.assertNotIn("[注意]", result)

    def test_non_verify_steps_never_challenged(self) -> None:
        agent = self.build([])
        result = self._plan(agent, [{"step": "编写解析器", "status": "completed"}])
        self.assertNotIn("[注意]", result)

    def test_already_completed_step_not_rechallenged(self) -> None:
        agent = self.build([])
        self._plan(agent, [{"step": "测试验证", "status": "completed"}])
        #  第二次更新时该步骤已是 completed：不算"新标完成"，不重复追问
        result = self._plan(agent, [{"step": "测试验证", "status": "completed"}])
        self.assertNotIn("[注意]", result)


class SkillLoadPathTest(AgentTestCase):
    """skill 加载结果必须带基准目录：正文里的相对路径模型无从知道相对谁。"""

    def _agent_with_skill(self):
        from xiaoyu.skills import Skill

        skill_dir = self.root / "myskill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: myskill\ndescription: 测试技能\n---\n"
            "看 [参考](references/deep.md)。\n",
            encoding="utf-8",
        )
        agent = self.build([])
        agent.skills = [
            Skill(name="myskill", description="测试技能", path=skill_dir / "SKILL.md")
        ]
        return agent, skill_dir

    def test_skill_body_carries_base_dir(self) -> None:
        agent, skill_dir = self._agent_with_skill()
        result = agent._load_skill("myskill")
        self.assertIn("技能目录", result)
        self.assertIn(str(skill_dir), result)
        self.assertIn("references/deep.md", result)

    def test_repeat_load_is_flagged(self) -> None:
        agent, _ = self._agent_with_skill()
        agent._load_skill("myskill")
        second = agent._load_skill("myskill")
        self.assertIn("已加载过", second)
        #  重复加载仍返回完整正文：上下文可能已被压缩，拒发正文会把模型困死
        self.assertIn("references/deep.md", second)

    def test_unknown_skill_unchanged(self) -> None:
        agent, _ = self._agent_with_skill()
        self.assertTrue(agent._load_skill("nope").startswith("ERROR:"))


class VanishedToolsTest(unittest.TestCase):
    """resume 预警：历史里用过、现已不在注册表里的工具要点名。"""

    def test_reports_missing_tools(self) -> None:
        from xiaoyu.cli import vanished_tools

        loaded = [
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "1", "function": {"name": "bash", "arguments": "{}"}},
                    {"id": "2", "function": {"name": "feishu_send", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "2", "content": "ok"},
        ]
        self.assertEqual(vanished_tools(loaded, ["bash", "read_file"]), ["feishu_send"])
        self.assertEqual(vanished_tools(loaded, ["bash", "feishu_send"]), [])
        self.assertEqual(vanished_tools([], ["bash"]), [])


class TestPrefixReplaySummary(AgentTestCase):
    """前缀重放式摘要：主模型腿逐字重放会话前缀。"""

    PREFIX = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "任务"},
        {"role": "assistant", "content": "好的"},
    ]

    def test_main_model_leg_replays_prefix(self) -> None:
        #  便宜模型挂了 → 主模型腿用前缀重放
        agent = self.build([RuntimeError("502"), text_response(GOOD_SUMMARY)])
        with contextlib.redirect_stdout(io.StringIO()):
            summary = agent._summarize("一段历史", list(self.PREFIX))
        self.assertEqual(summary, GOOD_SUMMARY)
        replay = self.client.completions.calls[1]
        self.assertEqual(replay["model"], "main-model")
        #  逐字重放前缀 + 指令作为最后一条 user 消息
        self.assertEqual(replay["messages"][: len(self.PREFIX)], self.PREFIX)
        self.assertEqual(replay["messages"][-1]["role"], "user")
        self.assertIn("以上对话", replay["messages"][-1]["content"])
        #  带工具 schema 对齐缓存前缀，但禁止真的调用
        self.assertTrue(replay.get("tools"))
        self.assertEqual(replay.get("tool_choice"), "none")

    def test_cheap_leg_keeps_transcript_posture(self) -> None:
        """便宜摘要模型窗口小、也没有本会话缓存：维持渲染转写，不重放。"""
        agent = self.build([text_response(GOOD_SUMMARY)])
        with contextlib.redirect_stdout(io.StringIO()):
            agent._summarize("一段历史", list(self.PREFIX))
        call = self.client.completions.calls[0]
        self.assertEqual(call["model"], "cheap-model")
        self.assertEqual(len(call["messages"]), 1)
        self.assertIn("一段历史", call["messages"][0]["content"])
        self.assertNotIn("tools", call)

    def test_replay_failure_falls_back_to_transcript_on_same_route(self) -> None:
        """重放姿势失败（个别网关不认 tool_choice）退回转写姿势，不作废整条路由。"""
        agent = self.build(
            [
                RuntimeError("502"),
                RuntimeError("tool_choice 不识别"),
                text_response(GOOD_SUMMARY),
            ]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            summary = agent._summarize("一段历史", list(self.PREFIX))
        self.assertEqual(summary, GOOD_SUMMARY)
        used = [call["model"] for call in self.client.completions.calls]
        self.assertEqual(used, ["cheap-model", "main-model", "main-model"])

    def test_no_prefix_means_no_replay(self) -> None:
        """不带前缀（compact 降级阶梯等）时主模型腿也走转写姿势。"""
        agent = self.build([RuntimeError("502"), text_response(GOOD_SUMMARY)])
        with contextlib.redirect_stdout(io.StringIO()):
            agent._summarize("一段历史")
        main_call = self.client.completions.calls[1]
        self.assertEqual(len(main_call["messages"]), 1)


class TestRestoreRepair(AgentTestCase):
    """resume 崩溃恢复：未配对调用补"结果未知"。"""

    def test_unpaired_call_gets_unknown_outcome(self) -> None:
        agent = self.build([])
        history = [
            {"role": "user", "content": "跑一下测试"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    }
                ],
            },
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            agent.restore(history)
        last = agent.messages[-1]
        self.assertEqual(last["role"], "tool")
        self.assertEqual(last["tool_call_id"], "c1")
        #  措辞必须是"结果未知、先核实"，不是"已放弃"——工具可能已经执行过
        self.assertIn("结果未知", last["content"])
        self.assertIn("核实", last["content"])

    def test_clean_history_untouched(self) -> None:
        agent = self.build([])
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "好"},
        ]
        with contextlib.redirect_stdout(io.StringIO()):
            agent.restore(history)
        self.assertEqual(agent.messages[-1]["content"], "好")

    def test_orphan_compact_lock_is_reported(self) -> None:
        """来源文件死在压缩中途（孤儿 compact_start）：resume 时提示一句。"""
        path = self.root / "dead.jsonl"
        path.write_text(
            json.dumps({"event": "meta", "format": 2})
            + "\n"
            + json.dumps({"event": "compact_start", "trigger": "auto"})
            + "\n",
            encoding="utf-8",
        )
        agent = self.build([])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.restore([], source=str(path))
        self.assertIn("压缩进行中异常终止", buffer.getvalue())

    def test_compact_attempt_is_bracketed_by_lock_events(self) -> None:
        """maybe_compact 的尝试被 compact_start/compact_end 括号包住：
        锁最后释放，正常路径永远不留孤儿 start。"""
        from xiaoyu.session_log import SessionLog, has_orphan_compact

        log_path = self.root / "log.jsonl"
        agent = self.build([], session_log=SessionLog(log_path))
        with contextlib.redirect_stdout(io.StringIO()):
            agent.maybe_compact(force=True)
        text = log_path.read_text(encoding="utf-8")
        self.assertIn('"compact_start"', text)
        self.assertIn('"compact_end"', text)
        self.assertFalse(has_orphan_compact(log_path))

    def test_paired_compact_lock_is_silent(self) -> None:
        path = self.root / "ok.jsonl"
        path.write_text(
            json.dumps({"event": "compact_start"})
            + "\n"
            + json.dumps({"event": "compact_end", "ok": True})
            + "\n",
            encoding="utf-8",
        )
        agent = self.build([])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.restore([], source=str(path))
        self.assertNotIn("压缩进行中异常终止", buffer.getvalue())


class TestEscalationApproval(AgentTestCase):
    """沙箱升权必须人工批准：allow 规则与 auto_approve（--yolo）都不放行。"""

    def _bash_call_script(self, args: str) -> list:
        first = [
            chunk(
                tool_calls=[call_fragment(0, "e1", "bash", args)]
            )
        ]
        return [first, [chunk(content="收到")]]

    def test_escalated_bash_asks_even_under_yolo(self) -> None:
        asked: list[str] = []

        def approver(name: str, args: dict) -> bool:
            asked.append(name)
            return False

        script = self._bash_call_script(
            '{"command": "true", "sandbox_permissions": "danger-full-access",'
            ' "justification": "测试"}'
        )
        #  auto_approve=True（harness 默认）等价 --yolo：普通 bash 不会问
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            agent = self.build(script, approver=approver)
            with contextlib.redirect_stdout(io.StringIO()):
                agent.send("升权跑一下")
        self.assertEqual(asked, ["bash"], "升权调用必须走人工确认")
        self.assertIn("拒绝", agent.messages[-2]["content"])

    def test_plain_bash_stays_auto_approved(self) -> None:
        asked: list[str] = []

        def approver(name: str, args: dict) -> bool:
            asked.append(name)
            return True

        script = self._bash_call_script('{"command": "echo hi"}')
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            agent = self.build(script, approver=approver)
            with contextlib.redirect_stdout(io.StringIO()):
                agent.send("跑一下")
        self.assertEqual(asked, [], "--yolo 下普通 bash 不该弹确认")


if __name__ == "__main__":
    unittest.main()
