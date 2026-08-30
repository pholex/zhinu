"""嵌入层加固修复的测试（Fable 审查后的五处修复）。

不打网络，假 client 注入。对应五个修复：

1. TestSequentialAfterEarlyExit —— stream() 提前退出后立刻开下一轮（stream 或
   send）：入口先等上一轮工作线程真正收尾，sink 物归原主、消息历史不交错——
   修"sink 永久指向死队列，事件静默丢失"
2. TestDenyRecheckOnRewrite —— Allow(updated_args=…) 改写后的参数重新过 deny
   规则——修"审批改写可绕过 bypass-immune 的 deny"
3. TestApproverLoopGuard —— AsyncApprover 在自己事件循环线程被同步调用时
   fail closed 返回配置错误，不死锁
4. TestResetClearsResidentState —— reset() 清 trace/_loaded_skills/打转计数/
   验证证据——修常驻 daemon 的内存泄漏与 recycle 后技能存根错乱
5. TestRunTextScopedToRun —— 本轮以空回复收场时 RunResult.text 是空串，
   不会把上一轮的交付文本错记到本轮
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import types
import unittest
from contextlib import aclosing
from pathlib import Path

from xiaoyu.agent import Agent, Allow
from xiaoyu.config import Config
from xiaoyu.embedding import AsyncAgent, AsyncApprover, RunCompleted, measured_send
from xiaoyu.events import TextDelta
from xiaoyu.permissions import Permissions, parse_rule
from xiaoyu.providers import Registry
from xiaoyu.tools import Toolbox

from tests.test_agent_paths import FakeClient, chunk
from tests.test_embedding_async import paced_stream
from tests.test_embedding_smoke import _chunk_with_tool_call


def make_config(root: Path, **overrides) -> Config:
    fields = dict(
        base_url="http://unused",
        model="main-model",
        summary_model="cheap-model",
        explore_model="cheap-model",
        workspace=root,
        auto_approve=True,
        #  钉确认档：关掉 auto_approve 的用例要真的走审批，不能被出厂 auto 档放行
        mode="default",
        enable_skills=False,
        enable_agents=False,
        enable_hooks=False,
        enable_plugins=False,
    )
    fields.update(overrides)
    return Config(**fields)


class HardeningTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()


class TestSequentialAfterEarlyExit(HardeningTestCase):
    async def test_stream_after_early_exit_gets_clean_run_and_original_sink(self) -> None:
        ready = threading.Event()
        proceed = threading.Event()
        config = make_config(self.root)
        client = FakeClient([paced_stream(ready, proceed), [chunk(content="B 轮回复")]])
        agent = Agent(config, Toolbox(config), registry=Registry.for_client(client))
        async_agent = AsyncAgent(agent)
        original_sink = agent.sink

        async with aclosing(async_agent.stream("A 轮")) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    break  # 提前退出
        proceed.set()

        #  宿主视角的"顺序下一轮"：不等待、直接开 B 轮
        collected = [event async for event in async_agent.stream("B 轮")]

        #  B 轮事件必须完整走进 B 的迭代器（不是掉进 A 留下的死队列）
        deltas = [event.text for event in collected if isinstance(event, TextDelta)]
        self.assertEqual(deltas, ["B 轮回复"])
        self.assertIsInstance(collected[-1], RunCompleted)
        self.assertEqual(collected[-1].result.text, "B 轮回复")
        #  收尾后 sink 物归原主，不是任何一轮的 bridge
        self.assertIs(agent.sink, original_sink)
        #  历史不交错：A 的半截话带中断标记，B 的回复在其后
        contents = [str(m.get("content")) for m in agent.messages if m.get("role") == "assistant"]
        self.assertTrue(any("被用户中断" in c for c in contents))
        self.assertEqual(agent.last_assistant_text(), "B 轮回复")
        self.assertNotIn("这段不该出现在历史里", " ".join(contents))

    async def test_send_after_early_exit_waits_for_previous_worker(self) -> None:
        ready = threading.Event()
        proceed = threading.Event()
        config = make_config(self.root)
        client = FakeClient([paced_stream(ready, proceed), [chunk(content="B 轮回复")]])
        agent = Agent(config, Toolbox(config), registry=Registry.for_client(client))
        async_agent = AsyncAgent(agent)
        original_sink = agent.sink

        async with aclosing(async_agent.stream("A 轮")) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    break
        proceed.set()

        result = await async_agent.send("B 轮")

        self.assertEqual(result.text, "B 轮回复")
        self.assertFalse(result.interrupted)
        #  send() 入口的 _settle_previous 已把 sink 物归原主，B 轮事件走的是宿主原 sink
        self.assertIs(agent.sink, original_sink)


class TestDenyRecheckOnRewrite(HardeningTestCase):
    async def test_rewritten_args_cannot_bypass_deny_rules(self) -> None:
        config = make_config(self.root, auto_approve=False)
        tool_call = types.SimpleNamespace(
            index=0,
            id="call_1",
            function=types.SimpleNamespace(name="bash", arguments='{"command": "echo hi"}'),
        )
        client = FakeClient([[_chunk_with_tool_call(tool_call)], [chunk(content="done")]])
        agent = Agent(
            config,
            Toolbox(config),
            registry=Registry.for_client(client),
            permissions=Permissions(self.root, [parse_rule("deny bash(rm *)")]),
            #  审批方（可能有 bug 的宿主逻辑）把无害命令改写成了 deny 名单里的
            approver=lambda name, args: Allow(updated_args={"command": "rm -rf sub"}),
        )

        agent.send("跑一下")

        tool_messages = [m["content"] for m in agent.messages if m.get("role") == "tool"]
        self.assertTrue(tool_messages)
        self.assertIn("deny 权限规则", tool_messages[-1])
        #  trace 里是规则拦截，不是执行成功
        bash_traces = [t for t in agent.trace if t["tool"] == "bash"]
        self.assertEqual(bash_traces[-1]["output"], "DENIED_BY_RULE")


class TestApproverLoopGuard(HardeningTestCase):
    async def test_sync_call_on_own_loop_fails_closed_instead_of_deadlocking(self) -> None:
        async def approve(name: str, args: dict) -> bool:
            return True

        approver = AsyncApprover(approve, asyncio.get_running_loop(), timeout=None)

        #  在事件循环线程上直接同步调用（宿主误用形态）。timeout=None 时若无
        #  护栏这里会永久挂死——护栏必须立刻返回拒绝，而不是等待
        verdict = approver("bash", {"command": "ls"})

        self.assertIsInstance(verdict, str)
        self.assertIn("配置错误", verdict)

    async def test_normal_worker_thread_call_still_works(self) -> None:
        async def approve(name: str, args: dict) -> bool:
            await asyncio.sleep(0)
            return True

        approver = AsyncApprover(approve, asyncio.get_running_loop(), timeout=2)
        verdict = await asyncio.to_thread(approver, "bash", {"command": "ls"})
        self.assertIs(verdict, True)


class TestResetClearsResidentState(unittest.TestCase):
    def test_reset_clears_all_per_conversation_state(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        config = make_config(root)
        agent = Agent(config, Toolbox(config), registry=Registry.for_client(FakeClient([])))

        agent.trace.append({"tool": "bash", "args": {}, "ok": True, "output": "x" * 1000})
        agent._loaded_skills.add("some-skill")
        agent._last_call_key = ("bash", "{}")
        agent._call_repeats = 2
        agent._exec_evidence = 3

        agent.reset()

        self.assertEqual(agent.trace, [])
        self.assertEqual(agent._loaded_skills, set())
        self.assertIsNone(agent._last_call_key)
        self.assertEqual(agent._call_repeats, 0)
        self.assertEqual(agent._exec_evidence, 0)


class TestRunTextScopedToRun(unittest.TestCase):
    def test_empty_reply_run_reports_empty_text_not_previous_delivery(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        config = make_config(root)
        #  第一轮正常交付；第二轮持续空回复——先被流层原地重试吃掉 3 次，
        #  再经 nudge 层 3 次，共 6 次全空后护栏收场
        client = FakeClient([[chunk(content="第一轮交付")], *([[chunk()]] * 6)])
        agent = Agent(config, Toolbox(config), registry=Registry.for_client(client))

        first = measured_send(agent, "第一句")
        self.assertEqual(first.text, "第一轮交付")

        second = measured_send(agent, "第二句")
        #  本轮没有任何交付——绝不能把第一轮的话当成本轮结果
        self.assertEqual(second.text, "")


if __name__ == "__main__":
    unittest.main()
