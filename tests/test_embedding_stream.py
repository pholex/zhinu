"""AsyncAgent.stream() 事件流迭代器的测试（嵌入 SDK 化第二批）。

不打网络，假 client 注入。四件事：

1. TestStreamEvents    —— 事件按产生顺序逐个产出，最后一个是 RunCompleted
   且携带与 send() 一致的 RunResult；迭代结束后 sink 恢复原样
2. TestStreamToolFlow  —— 工具生命周期事件（pending → running → completed）
   完整走进迭代器，宿主能据此画状态
3. TestStreamEarlyExit —— 消费方提前退出（aclosing + break，异步生成器
   提前退出的标准姿势——裸 break 的 finally 要等 GC 终结器，时机不确定）：
   自动 interrupt()，半截话入历史，线程收尾后 sink 恢复，不污染宿主原 sink
4. TestStreamError     —— 工作线程抛真错误（非打断）时原样传给宿主的
   async for 循环，不被吞掉
"""

from __future__ import annotations

import asyncio
import tempfile
import threading
import types
import unittest
from contextlib import aclosing
from pathlib import Path

from xiaoyu.agent import Agent
from xiaoyu.config import Config
from xiaoyu.embedding import AsyncAgent, RunCompleted
from xiaoyu.events import TextDelta, ToolCompleted, ToolPending, ToolRunning
from xiaoyu.providers import Registry
from xiaoyu.tools import Toolbox

from tests.test_agent_paths import FakeClient, chunk, usage_chunk
from tests.test_embedding_async import paced_stream
from tests.test_embedding_smoke import _chunk_with_tool_call


class StreamTestCase(unittest.IsolatedAsyncioTestCase):
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

    def build(self, script: list) -> tuple[Agent, AsyncAgent]:
        client = FakeClient(script)
        agent = Agent(self.config, Toolbox(self.config), registry=Registry.for_client(client))
        return agent, AsyncAgent(agent)


class TestStreamEvents(StreamTestCase):
    async def test_events_then_run_completed_and_sink_restored(self) -> None:
        agent, async_agent = self.build(
            [[chunk(content="你好，"), chunk(content="世界"), usage_chunk(60, 12)]]
        )
        original_sink = agent.sink

        events = [event async for event in async_agent.stream("打个招呼")]

        deltas = [event.text for event in events if isinstance(event, TextDelta)]
        self.assertEqual(deltas, ["你好，", "世界"])
        self.assertIsInstance(events[-1], RunCompleted)
        result = events[-1].result
        self.assertEqual(result.text, "你好，世界")
        self.assertFalse(result.interrupted)
        self.assertEqual(result.usage["turns"], 1)
        self.assertEqual(result.usage["prompt_tokens"], 60)
        #  迭代器就是本轮唯一的事件出口；结束后必须物归原主
        self.assertIs(agent.sink, original_sink)

    async def test_run_completed_serializes(self) -> None:
        """RunCompleted 走 to_dict() 可直接 JSON 化——SSE/落盘不用再写转换层。"""
        agent, async_agent = self.build([[chunk(content="好")]])
        events = [event async for event in async_agent.stream("嗯")]
        payload = events[-1].to_dict()
        self.assertEqual(payload["kind"], "run.completed")
        self.assertEqual(payload["result"]["text"], "好")


class TestStreamToolFlow(StreamTestCase):
    async def test_tool_lifecycle_events_flow_through(self) -> None:
        tool_call = types.SimpleNamespace(
            index=0,
            id="call_1",
            function=types.SimpleNamespace(name="bash", arguments='{"command": "echo hi"}'),
        )
        agent, async_agent = self.build(
            [[_chunk_with_tool_call(tool_call)], [chunk(content="done")]]
        )

        kinds = [event.kind async for event in async_agent.stream("跑一下")]

        for expected in ("tool.pending", "tool.running", "tool.completed"):
            self.assertIn(expected, kinds)
        self.assertLess(kinds.index("tool.pending"), kinds.index("tool.running"))
        self.assertLess(kinds.index("tool.running"), kinds.index("tool.completed"))
        self.assertEqual(kinds[-1], "run.completed")

    async def test_tool_completed_carries_output(self) -> None:
        tool_call = types.SimpleNamespace(
            index=0,
            id="call_1",
            function=types.SimpleNamespace(name="bash", arguments='{"command": "echo hi"}'),
        )
        agent, async_agent = self.build(
            [[_chunk_with_tool_call(tool_call)], [chunk(content="done")]]
        )
        outputs = [
            event.output
            async for event in async_agent.stream("跑一下")
            if isinstance(event, ToolCompleted)
        ]
        self.assertEqual(len(outputs), 1)
        self.assertIn("hi", outputs[0])


class TestStreamEarlyExit(StreamTestCase):
    async def test_break_interrupts_and_restores_sink_after_worker_finishes(self) -> None:
        ready = threading.Event()
        proceed = threading.Event()
        agent, async_agent = self.build([paced_stream(ready, proceed)])
        original_sink = agent.sink

        async with aclosing(async_agent.stream("看一下")) as events:
            async for event in events:
                if isinstance(event, TextDelta):
                    break  # 宿主看了一眼就走人；aclosing 保证退出即 interrupt

        proceed.set()  # 放行假流的第二段，工作线程在下一个 chunk 边界发现打断

        #  等工作线程收尾：半截话入历史、sink 物归原主
        for _ in range(200):
            if agent.sink is original_sink:
                break
            await asyncio.sleep(0.01)
        self.assertIs(agent.sink, original_sink)
        last = agent.messages[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertIn("我先看看代码", last["content"])
        self.assertIn("被用户中断", last["content"])
        self.assertNotIn("这段不该出现在历史里", last["content"])


class TestStreamError(StreamTestCase):
    async def test_worker_error_propagates_to_consumer(self) -> None:
        agent, async_agent = self.build([RuntimeError("网关 502")])

        with self.assertRaises(Exception) as caught:
            async for _ in async_agent.stream("跑一下"):
                pass
        self.assertIn("502", str(caught.exception))
        #  错误路径也不能漏恢复 sink
        self.assertNotEqual(type(agent.sink).__name__, "_QueueSink")


if __name__ == "__main__":
    unittest.main()
