"""async approver 生产化的测试（「库层嵌入 SDK 化」第 2 块）。

不打网络，假 client 注入。`AsyncApprover` 把宿主的 async 审批函数桥接成
`Agent` 认识的同步 `Approver`——协程实际跑在宿主的事件循环上（用
`asyncio.run_coroutine_threadsafe`），`AsyncAgent.send()` 的工作线程只是
阻塞等结果，不影响事件循环上别的协程。四件事：

1. TestApprove         —— 协程真的在事件循环上跑（await 了依赖那个循环的东西），
   返回 True 时工具正常执行
2. TestDenyWithReason  —— 协程返回非空字符串时工具被拒绝，理由原文回灌模型
3. TestTimeout         —— 协程超时不响应时自动拒绝（fail closed），不会挂起工作线程
4. TestException       —— 协程抛异常时自动拒绝（fail closed），不会掀翻整轮对话
"""

from __future__ import annotations

import asyncio
import tempfile
import types
import unittest
from pathlib import Path

from xiaoyu.agent import Agent
from xiaoyu.config import Config
from xiaoyu.embedding import AsyncAgent, AsyncApprover
from xiaoyu.providers import Registry
from xiaoyu.tools import Toolbox

from tests.test_agent_paths import FakeClient, chunk
from tests.test_embedding_smoke import _chunk_with_tool_call


class AsyncApproverTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.config = Config(
            base_url="http://unused",
            model="main-model",
            summary_model="cheap-model",
            explore_model="cheap-model",
            workspace=self.root,
            auto_approve=False,
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build_with_bash_call(self, approver, follow_up: str = "done") -> Agent:
        tool_call = types.SimpleNamespace(
            index=0,
            id="call_1",
            function=types.SimpleNamespace(name="bash", arguments='{"command": "ls"}'),
        )
        script = [[_chunk_with_tool_call(tool_call)], [chunk(content=follow_up)]]
        client = FakeClient(script)
        return Agent(
            self.config,
            Toolbox(self.config),
            registry=Registry.for_client(client),
            approver=approver,
        )


class TestApprove(AsyncApproverTestCase):
    async def test_coroutine_runs_on_event_loop_and_approves(self) -> None:
        seen = []

        async def approve(name: str, args: dict) -> bool:
            #  真的 await 一个依赖事件循环的东西——如果这段代码被误当成
            #  普通函数在别的线程/循环上跑，asyncio.sleep 这里就会出问题
            await asyncio.sleep(0.01)
            seen.append((name, dict(args)))
            return True

        loop = asyncio.get_running_loop()
        agent = self.build_with_bash_call(AsyncApprover(approve, loop))
        async_agent = AsyncAgent(agent)

        await async_agent.send("跑一下 ls")

        self.assertEqual(seen, [("bash", {"command": "ls"})])
        #  批准了就该真的执行，不是拒绝
        tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertTrue(tool_messages)
        self.assertNotIn("拒绝", tool_messages[-1]["content"])


class TestDenyWithReason(AsyncApproverTestCase):
    async def test_non_empty_string_denies_with_reason(self) -> None:
        async def approve(name: str, args: dict) -> str:
            await asyncio.sleep(0)
            return "宿主策略：飞书渠道不放行 bash"

        loop = asyncio.get_running_loop()
        agent = self.build_with_bash_call(AsyncApprover(approve, loop))
        async_agent = AsyncAgent(agent)

        await async_agent.send("跑一下 ls")

        tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertTrue(tool_messages)
        self.assertIn("宿主策略", tool_messages[-1]["content"])


class TestTimeout(AsyncApproverTestCase):
    async def test_slow_coroutine_auto_denies_without_hanging(self) -> None:
        async def approve(name: str, args: dict) -> bool:
            await asyncio.sleep(10)  # 远超测试超时——不该真的等完
            return True

        loop = asyncio.get_running_loop()
        agent = self.build_with_bash_call(AsyncApprover(approve, loop, timeout=0.05))
        async_agent = AsyncAgent(agent)

        await asyncio.wait_for(async_agent.send("跑一下 ls"), timeout=2)

        tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertTrue(tool_messages)
        self.assertIn("超时", tool_messages[-1]["content"])


class TestException(AsyncApproverTestCase):
    async def test_coroutine_error_auto_denies(self) -> None:
        async def approve(name: str, args: dict) -> bool:
            await asyncio.sleep(0)
            raise RuntimeError("飞书 API 挂了")

        loop = asyncio.get_running_loop()
        agent = self.build_with_bash_call(AsyncApprover(approve, loop))
        async_agent = AsyncAgent(agent)

        #  故障不能掀翻整轮对话——send() 正常跑完，只是这次工具被拒绝
        await async_agent.send("跑一下 ls")

        tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertTrue(tool_messages)
        self.assertIn("出错", tool_messages[-1]["content"])
        self.assertIn("飞书 API 挂了", tool_messages[-1]["content"])


if __name__ == "__main__":
    unittest.main()
