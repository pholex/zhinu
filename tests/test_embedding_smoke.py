"""库层嵌入可行性验证。

背景：常驻宿主（把 agent 当执行引擎嵌进去的长进程）需要的形态是——一个 client 对象
跨多轮复用、注入实时 approver 回调、消费流式事件，这也是嵌入式 agent SDK
的标准用法。xiaoyu 此前只有 CLI 用法被验证过——一次性模式
每次冷启动一个新进程只发一条 `send()`；REPL 模式虽是长进程，但从没有"宿主进程 import
`Agent`、自己驱动多轮、自己注入 approver/sink"这个用法被跑通过一次。

这份测试就是把这个假设的架构钉死：证明 `Agent` 单实例可以被当作库对象反复驱动，
不必每轮重建、不必经过 CLI 子进程。不打网络，用假 client 注入（同 test_agent_paths.py
的模式）。三件事分别对应差距清单里的三项：

1. TestMultiTurnReuse   —— 同一个 Agent 实例反复 send()，上下文原样累积（对应
   "常驻会话入口未被验证过"）
2. TestLiveApprover     —— approver 是宿主可注入的活回调，能看到实时参数、能拒绝、
   能附言（对应"实时可编程 approver"，这里先验证同步桥接可行，异步桥接另见
   test_embedding_approver.py）
3. TestSinkAsEventFeed  —— sink 可被宿主换成"收集事件到列表"而非打印，验证事件流
   可以喂给宿主自己的消费逻辑（对应"Sink → 宿主进度心跳"这条）
4. TestRecycle          —— reset() 清空对话但 Agent 对象/registry/config 都不重建，
   验证"recycle 语义"已经存在，只是没对外正式承诺这个名字/协议
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xiaoyu.agent import Agent
from xiaoyu.config import Config
from xiaoyu.events import ToolDenied, UIEvent
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
            auto_approve=False,
            enable_skills=False,
            enable_agents=False,
            enable_hooks=False,
            enable_plugins=False,
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def build(self, script: list, **kwargs) -> tuple[Agent, FakeClient]:
        client = FakeClient(script)
        agent = Agent(self.config, Toolbox(self.config), registry=Registry.for_client(client), **kwargs)
        return agent, client


class TestMultiTurnReuse(EmbeddingTestCase):
    def test_same_instance_survives_multiple_host_driven_turns(self) -> None:
        """一个 Agent 对象，宿主分两次调用 send()（模拟飞书两条独立消息），
        第二轮请求体里必须能看到第一轮的问答——证明不必每轮重建 Agent。
        """
        agent, client = self.build(
            [[chunk(content="收到，1 号工单")], [chunk(content="1 号工单还没处理")]]
        )

        agent.send("记一下：1 号工单")
        first_len = len(agent.messages)
        self.assertEqual(agent.last_assistant_text(), "收到，1 号工单")

        #  宿主这里不会重建 Agent、不会重传历史——直接调第二次 send()
        agent.send("1 号工单处理了吗")

        self.assertGreater(len(agent.messages), first_len)
        self.assertEqual(agent.last_assistant_text(), "1 号工单还没处理")
        #  第二轮实际发给"模型"的请求里带着第一轮的问答，不是只有这一句
        second_request_messages = client.completions.calls[1]["messages"]
        joined = " ".join(m.get("content") or "" for m in second_request_messages if isinstance(m.get("content"), str))
        self.assertIn("1 号工单", joined)


class TestLiveApprover(EmbeddingTestCase):
    def test_host_supplied_callback_sees_real_args_and_can_deny(self) -> None:
        """approver 不是写死的策略，是宿主传进来的活函数——能拿到真实的
        (工具名, 参数)，返回值能拒绝并附言，且拒绝原因要能回到模型上下文里。
        """
        import types

        seen: list[tuple[str, dict]] = []

        def host_approver(name: str, args: dict) -> str | bool:
            seen.append((name, dict(args)))
            if name == "bash":
                #  约定：非空字符串 = 拒绝并附理由（不是 (False, "...") 元组——
                #  元组形状只用于"批准并附言"，这是踩过一次才摸清的真实契约）
                return "宿主策略：飞书渠道不放行 bash"
            return True

        tool_call = types.SimpleNamespace(
            index=0,
            id="call_1",
            function=types.SimpleNamespace(name="bash", arguments='{"command": "rm -rf /"}'),
        )
        first = [_chunk_with_tool_call(tool_call)]
        second = [chunk(content="已拒绝执行")]
        agent, _ = self.build([first, second], approver=host_approver)

        agent.send("跑一下这个命令")

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "bash")
        self.assertIn("command", seen[0][1])
        #  拒绝要作为工具结果回灌进对话，模型才能看到理由继续对话
        tool_messages = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertTrue(tool_messages)
        self.assertIn("宿主策略", tool_messages[-1]["content"])


class TestSinkAsEventFeed(EmbeddingTestCase):
    def test_sink_can_be_swapped_for_a_collector_instead_of_printing(self) -> None:
        """sink 只是个 Protocol（emit(event)），宿主可以换成"收集到列表"而非
        打印到终端——这是"事件流喂给宿主自己的消费逻辑"（比如飞书心跳更新）的前提。
        """

        events: list[UIEvent] = []

        class CollectingSink:
            def emit(self, event: UIEvent) -> None:
                events.append(event)

        import types

        tool_call = types.SimpleNamespace(
            index=0,
            id="call_1",
            function=types.SimpleNamespace(name="bash", arguments="{}"),
        )
        agent, _ = self.build(
            [[_chunk_with_tool_call(tool_call)], [chunk(content="done")]],
            approver=lambda name, args: False,
            sink=CollectingSink(),
        )

        agent.send("随便跑点什么")

        kinds = {e.kind for e in events}
        #  至少要能看到工具被拒绝这类事件，宿主才能据此更新飞书卡片状态
        self.assertIn(ToolDenied.kind, kinds)
        self.assertTrue(any(isinstance(e, ToolDenied) for e in events))


class TestRecycle(EmbeddingTestCase):
    def test_reset_clears_conversation_without_rebuilding_agent(self) -> None:
        """Agent 层的 reset()：清对话、留对象。宿主侧的正名是
        AsyncAgent.recycle()（见 TestRecycleAlias），这里锁 Agent 层语义。"""
        agent, _ = self.build([[chunk(content="你好")]])
        agent.send("你好")
        self.assertGreater(len(agent.messages), 1)

        registry_before = agent.registry
        config_before = agent.config

        agent.reset()

        self.assertEqual(len(agent.messages), 1)  # 只剩 system prompt
        #  对象本身、registry、config 都没有被重建——这才是"recycle"而不是"重启进程"
        self.assertIs(agent.registry, registry_before)
        self.assertIs(agent.config, config_before)


class TestRecycleAlias(EmbeddingTestCase):
    def test_async_agent_recycle_is_the_canonical_name(self) -> None:
        """recycle() 已是 AsyncAgent 的正名（对齐常驻宿主侧的惯用词），
        reset() 留作别名——hasattr(agent, "recycle") 这类宿主探测直接成立。"""
        from xiaoyu.embedding import AsyncAgent

        agent, _ = self.build([[chunk(content="你好")]])
        async_agent = AsyncAgent(agent)
        agent.send("你好")
        self.assertGreater(len(agent.messages), 1)

        async_agent.recycle()
        self.assertEqual(len(agent.messages), 1)


class TestRestore(EmbeddingTestCase):
    def test_restore_replays_history_and_copies_into_new_session_file(self) -> None:
        """嵌入宿主的 resume 路径：SessionLog 落盘 → load_messages 读出 →
        restore 接回新 Agent。与 CLI `xiaoyu resume` 共用同一条公开路径，
        新会话文件自包含（历史被复制进去，可再次 resume）。"""
        import json

        from xiaoyu.session_log import SessionLog, load_messages

        old_path = self.root / "old-session.jsonl"
        agent1, _ = self.build([[chunk(content="收到，1 号工单")]], session_log=SessionLog(old_path))
        agent1.send("记一下：1 号工单")

        loaded = load_messages(old_path)
        self.assertEqual([m["role"] for m in loaded], ["user", "assistant"])

        new_path = self.root / "new-session.jsonl"
        agent2, _ = self.build([[chunk(content="还没处理")]], session_log=SessionLog(new_path))
        agent2.restore(loaded, source=str(old_path))

        #  上下文接上：system prompt 之后跟着完整历史
        self.assertEqual(len(agent2.messages), 1 + len(loaded))
        self.assertEqual(agent2.last_assistant_text(), "收到，1 号工单")
        #  新文件自包含：历史被复制进去，并记了 resumed_from 来源
        records = [json.loads(line) for line in new_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(r.get("event") == "resumed_from" and str(old_path) in r.get("source", "") for r in records))
        self.assertTrue(any(r.get("role") == "user" for r in records))

        #  接回之后继续对话没有断层
        agent2.send("1 号工单处理了吗")
        self.assertEqual(agent2.last_assistant_text(), "还没处理")


def _chunk_with_tool_call(tool_call):
    import types

    delta = types.SimpleNamespace(content=None, tool_calls=[tool_call])
    choice = types.SimpleNamespace(delta=delta)
    usage = types.SimpleNamespace(prompt_tokens=50, completion_tokens=10)
    return types.SimpleNamespace(choices=[choice], usage=usage)


if __name__ == "__main__":
    unittest.main()
