"""上下文窗口两件套：get_context_remaining 查询、new_context 翻篇。

不打网络：复用 test_agent_paths 的假 client。
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from xiaoyu import tokens
from xiaoyu.agent import PLAN_MODE_TOOLS, Agent
from xiaoyu.tools import Toolbox

from tests.test_agent_paths import AgentTestCase, Registry, call_fragment, chunk, usage_chunk
from tests.test_context import assert_valid_sequence


class RecordingLog:
    """最小会话日志桩：只记事件与消息。"""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self.messages: list[dict] = []

    def event(self, kind: str, **fields) -> None:
        self.events.append((kind, fields))

    def append(self, message: dict) -> None:
        self.messages.append(message)


class GetContextRemainingTest(AgentTestCase):
    def test_numbers_match_estimator(self) -> None:
        agent = self.build([])
        agent.messages.append({"role": "user", "content": "一段占位文本 " * 50})
        payload = json.loads(agent._get_context_remaining())  # noqa: SLF001
        expected_used = tokens.estimate_messages(agent.messages) + tokens.estimate_tools(
            agent.toolbox.schemas()
        )
        self.assertEqual(payload["used"], expected_used)
        self.assertEqual(payload["context_window"], self.config.context_limit)
        self.assertEqual(payload["tokens_left"], self.config.context_limit - expected_used)
        self.assertEqual(payload["window"], 1)

    def test_registered_readonly_and_in_plan_mode(self) -> None:
        agent = self.build([])
        tool = agent.toolbox.get("get_context_remaining")
        self.assertIsNotNone(tool)
        self.assertFalse(tool.requires_approval)
        self.assertIn("get_context_remaining", PLAN_MODE_TOOLS)
        self.assertIn("ERROR", agent._get_context_remaining(foo=1))  # noqa: SLF001


class NewContextTest(AgentTestCase):
    def test_subagent_has_no_new_context(self) -> None:
        from tests.test_agent_paths import FakeClient

        child = Agent(
            self.config,
            Toolbox(self.config),
            registry=Registry.for_client(FakeClient([])),
            allow_explore=False,
        )
        self.assertIsNone(child.toolbox.get("new_context"))
        self.assertIsNotNone(child.toolbox.get("get_context_remaining"))

    def test_handler_validates_notes(self) -> None:
        agent = self.build([])
        self.assertIn("ERROR", agent._new_context(notes=""))  # noqa: SLF001
        self.assertIn("ERROR", agent._new_context(notes="x", extra=1))  # noqa: SLF001
        self.assertIsNone(agent._new_context_notes)  # noqa: SLF001
        reply = agent._new_context(notes="目标：修 calc.py")  # noqa: SLF001
        self.assertIn("第 2 个", reply)
        self.assertEqual(agent._new_context_notes, "目标：修 calc.py")  # noqa: SLF001

    def test_turn_resets_history_after_tool_result(self) -> None:
        """完整一轮：读文件 → new_context → 新窗口里继续 → 收尾。

        断言：翻篇发生在 tool 结果入历史之后（没有孤儿）、历史只剩
        system + 交接笔记、窗口号递增、后续消息接在新窗口里、日志协议成对。
        """
        first = [
            chunk(tool_calls=[call_fragment(0, "r1", "read_file", '{"path": "calc.py"}')]),
            usage_chunk(300, 20),
        ]
        notes = "目标：给 calc.py 补除零保护。已读文件，函数 add 存在。下一步：改 div。"
        second = [
            chunk(
                tool_calls=[
                    call_fragment(0, "n1", "new_context", json.dumps({"notes": notes}, ensure_ascii=False))
                ]
            ),
            usage_chunk(900, 20),
        ]
        third = [chunk(content="新窗口里继续，已按笔记接上"), usage_chunk(200, 10)]
        log = RecordingLog()
        agent = self.build([first, second, third], session_log=log)

        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("把 calc.py 的除零补上")

        self.assertEqual([entry["tool"] for entry in agent.trace], ["read_file", "new_context"])
        self.assertTrue(all(entry["ok"] for entry in agent.trace))
        self.assertEqual(agent._context_window, 2)  # noqa: SLF001
        self.assertIsNone(agent._new_context_notes)  # noqa: SLF001

        roles = [m["role"] for m in agent.messages]
        self.assertEqual(roles, ["system", "user", "assistant"])
        note = agent.messages[1]["content"]
        self.assertIn("第 2 个", note)
        self.assertIn(notes, note)
        self.assertNotIn("calc.py 的除零补上", note)
        assert_valid_sequence(self, agent.messages)

        #  第三次请求发出去的就是新窗口：system + 笔记，没有旧历史
        #  （假 client 存的是同一个 list 引用，收尾的 assistant 事后也会出现在里面）
        sent = self.client.completions.calls[2]["messages"]
        self.assertEqual([m["role"] for m in sent[:2]], ["system", "user"])
        self.assertNotIn("tool", [m["role"] for m in sent])
        self.assertIn(notes, sent[1]["content"])

        kinds = [kind for kind, _ in log.events]
        self.assertIn("compact_start", kinds)
        self.assertIn("compact_end", kinds)
        compact = next(fields for kind, fields in log.events if kind == "compact")
        self.assertEqual(compact["window"], 2)
        self.assertEqual([m["role"] for m in compact["replacement"]], ["user"])
        start = next(fields for kind, fields in log.events if kind == "compact_start")
        self.assertEqual(start["trigger"], "new_context")

    def test_notes_are_capped(self) -> None:
        agent = self.build([])
        agent._new_context(notes="x" * 10_000)  # noqa: SLF001
        self.assertLess(len(agent._new_context_notes), 4100)  # noqa: SLF001
        self.assertIn("已截断", agent._new_context_notes)  # noqa: SLF001


if __name__ == "__main__":
    unittest.main()


class HistoryVersionTest(AgentTestCase):
    """history_version：追加不计，任何改写（翻篇 / 回滚 / 重置 / 修复 / 装入）都 +1。"""

    def test_append_does_not_bump_but_rewrites_do(self) -> None:
        agent = self.build([[chunk(content="好"), usage_chunk(50, 5)]])
        self.assertEqual(agent.history_version, 0)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("你好")
        self.assertEqual(agent.history_version, 0)
        before = len(agent.messages)

        agent.messages.append({"role": "assistant", "content": "追加"})
        self.assertEqual(agent.history_version, 0)

        agent._new_context_notes = "笔记"  # noqa: SLF001
        agent._start_new_context_window()  # noqa: SLF001
        self.assertEqual(agent.history_version, 1)
        self.assertLess(len(agent.messages), before + 1)

        agent.reset()
        self.assertEqual(agent.history_version, 2)

        agent.restore([{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"}])
        self.assertEqual(agent.history_version, 3)
        agent.restore([])
        self.assertEqual(agent.history_version, 3)

    def test_repair_bumps_only_when_changed(self) -> None:
        agent = self.build([])
        agent._repair_history()  # noqa: SLF001
        self.assertEqual(agent.history_version, 0)
        agent.messages.append({"role": "tool", "tool_call_id": "orphan", "content": "孤儿"})
        agent._repair_history()  # noqa: SLF001
        self.assertEqual(agent.history_version, 1)
