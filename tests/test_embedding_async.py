"""异步化 + interrupt 原语的测试（「库层嵌入 SDK 化」第 1 块）。

不打网络，假 client 注入（同 test_agent_paths.py 的模式）。三件事：

1. TestSyncInterrupt   —— Agent.interrupt() 是线程安全的标志位，在下一个 chunk
   边界被 _consume_stream 发现，走跟 Ctrl-C 完全一样的收尾路径（半截话入历史、
   残缺 tool_calls 丢弃），且不会"没打中东西却污染下一轮"
2. TestAsyncAgentSend  —— AsyncAgent.send() 用 asyncio.to_thread 跑，不堵事件循环
   ——循环里另一个协程能在 send() 跑的同时继续推进
3. TestAsyncAgentInterrupt —— 端到端：事件循环里调 async_agent.interrupt()，
   能打断正跑在工作线程里的 send()；打断以 result.interrupted=True 报告，
   不以异常弹回宿主（同步 Agent.send() 仍抛 Interrupted，语义只在异步层收拢）
4. TestRunResult      —— send() 返回本轮元数据：正文、usage 增量（不是会话
   累计）、耗时、上下文水位
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from xiaoyu.agent import Agent, Interrupted
from xiaoyu.config import Config
from xiaoyu.embedding import AsyncAgent
from xiaoyu.providers import Registry
from xiaoyu.tools import Toolbox

from tests.test_agent_paths import FakeClient, chunk


class EmbeddingTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.config = Config(
            base_url="http://unused",
            model="main-model",
            summary_model="cheap-model",
            explore_model="cheap-model",
            workspace=self.root,
            auto_approve=True,
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, script: list, **kwargs) -> Agent:
        client = FakeClient(script)
        return Agent(self.config, Toolbox(self.config), registry=Registry.for_client(client), **kwargs)


def paced_stream(ready: threading.Event, proceed: threading.Event):
    """先吐一段正文，通知测试"可以打断了"，然后等测试放行才吐第二段——
    给测试一个确定的窗口在"已经说了一半"和"继续说"之间调用 interrupt()。
    """
    yield chunk(content="我先看看代码")
    ready.set()
    proceed.wait(timeout=2)
    yield chunk(content="——这段不该出现在历史里")


class TestSyncInterrupt(EmbeddingTestCase):
    def test_interrupt_mid_stream_preserves_partial_text(self) -> None:
        ready = threading.Event()
        proceed = threading.Event()
        agent = self.build([paced_stream(ready, proceed)])

        def caller() -> None:
            ready.wait(timeout=2)
            agent.interrupt()
            proceed.set()

        threading.Thread(target=caller).start()

        with self.assertRaises(Interrupted):
            agent.send("看一下这段代码")

        last = agent.messages[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertIn("我先看看代码", last["content"])
        self.assertIn("被用户中断", last["content"])
        self.assertNotIn("这段不该出现在历史里", last["content"])

    def test_stray_interrupt_does_not_leak_into_next_turn(self) -> None:
        """没打中任何东西的 interrupt()（比如宿主手抖调了两次）不能把下一轮
        无关的 send() 也误伤。"""
        agent = self.build([[chunk(content="第一轮正常回复")], [chunk(content="第二轮也该正常")]])

        agent.send("第一句")
        self.assertEqual(agent.last_assistant_text(), "第一轮正常回复")

        #  这次 interrupt() 没有对应正在跑的请求——纯粹的陈旧调用
        agent.interrupt()

        agent.send("第二句")
        self.assertEqual(agent.last_assistant_text(), "第二轮也该正常")


class TestAsyncAgentSend(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.config = Config(
            base_url="http://unused",
            model="main-model",
            summary_model="cheap-model",
            explore_model="cheap-model",
            workspace=self.root,
            auto_approve=True,
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_send_does_not_block_event_loop(self) -> None:
        """send() 跑在工作线程里，事件循环上的另一个协程要能同时继续推进。"""
        ready = threading.Event()
        proceed = threading.Event()
        client = FakeClient([paced_stream(ready, proceed)])
        agent = Agent(self.config, Toolbox(self.config), registry=Registry.for_client(client))
        async_agent = AsyncAgent(agent)

        ticks = 0

        async def ticker() -> None:
            nonlocal ticks
            while not proceed.is_set():
                ticks += 1
                await asyncio.sleep(0.01)

        send_task = asyncio.create_task(async_agent.send("跑起来"))
        tick_task = asyncio.create_task(ticker())

        await asyncio.to_thread(ready.wait, 2)
        #  send() 已经卡在 paced_stream 的 proceed.wait() 里了，
        #  但 ticker 这段时间应该照样在推进——证明事件循环没被堵住
        self.assertGreater(ticks, 0)

        proceed.set()
        await send_task
        await tick_task


class TestAsyncAgentInterrupt(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.config = Config(
            base_url="http://unused",
            model="main-model",
            summary_model="cheap-model",
            explore_model="cheap-model",
            workspace=self.root,
            auto_approve=True,
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_interrupt_from_event_loop_stops_worker_thread_send(self) -> None:
        ready = threading.Event()
        proceed = threading.Event()
        client = FakeClient([paced_stream(ready, proceed)])
        agent = Agent(self.config, Toolbox(self.config), registry=Registry.for_client(client))
        async_agent = AsyncAgent(agent)

        send_task = asyncio.create_task(async_agent.send("看一下"))

        await asyncio.to_thread(ready.wait, 2)
        async_agent.interrupt()  # 从事件循环线程调用，非阻塞
        proceed.set()

        #  打断是宿主自己的主动动作，不以异常弹回——以 result.interrupted 报告
        result = await send_task
        self.assertTrue(result.interrupted)
        self.assertIn("我先看看代码", result.text)

        last = agent.messages[-1]
        self.assertIn("我先看看代码", last["content"])
        self.assertIn("被用户中断", last["content"])


class TestRunResult(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.config = Config(
            base_url="http://unused",
            model="main-model",
            summary_model="cheap-model",
            explore_model="cheap-model",
            workspace=self.root,
            auto_approve=True,
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    async def test_send_returns_per_run_usage_delta(self) -> None:
        from tests.test_agent_paths import usage_chunk

        client = FakeClient(
            [
                [chunk(content="第一轮回复"), usage_chunk(100, 20)],
                [chunk(content="第二轮回复"), usage_chunk(150, 30)],
            ]
        )
        agent = Agent(self.config, Toolbox(self.config), registry=Registry.for_client(client))
        async_agent = AsyncAgent(agent)

        first = await async_agent.send("第一句")
        self.assertEqual(first.text, "第一轮回复")
        self.assertFalse(first.interrupted)
        self.assertGreaterEqual(first.duration_seconds, 0)
        self.assertEqual(first.usage["turns"], 1)
        self.assertEqual(first.usage["prompt_tokens"], 100)
        self.assertEqual(first.usage["completion_tokens"], 20)
        self.assertGreater(first.context_tokens, 0)

        #  第二轮拿到的是**本轮增量**，不是会话累计——宿主对账全靠这一点
        second = await async_agent.send("第二句")
        self.assertEqual(second.text, "第二轮回复")
        self.assertEqual(second.usage["turns"], 1)
        self.assertEqual(second.usage["prompt_tokens"], 150)
        self.assertEqual(second.usage["completion_tokens"], 30)
        by_model = second.usage["by_model"]
        self.assertEqual(len(by_model), 1)
        (entry,) = by_model.values()
        self.assertEqual(entry["calls"], 1)


if __name__ == "__main__":
    unittest.main()
