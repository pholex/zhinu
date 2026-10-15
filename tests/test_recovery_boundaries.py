"""截断、压缩、崩溃续写的组合回归：以落盘历史与实际副作用为准。"""

from __future__ import annotations

import contextlib
import copy
import io
import json
from unittest import mock

from xiaoyu.agent import Agent
from xiaoyu.session_log import SessionLog, load_messages
from xiaoyu.tools import Tool

from .test_agent_paths import AgentTestCase, chunk
from .test_errors import length_chunk


class RecoveryBoundariesTest(AgentTestCase):
    def assert_tool_pairs(self, messages):
        calls = [call["id"] for m in messages for call in m.get("tool_calls", [])]
        results = [m["tool_call_id"] for m in messages if m.get("role") == "tool"]
        self.assertEqual(calls, results)
        self.assertEqual(len(results), len(set(results)))

    def log(self, name="session.jsonl"):
        log = SessionLog(self.root / name)
        self.addCleanup(log.release)
        return log

    def test_truncation_then_compaction_survives_restart_and_another_turn(self):
        log = self.log()
        agent = self.build([[chunk(content="最近的半截回答"), length_chunk()]], session_log=log)
        self.config.keep_recent = agent.compactor.keep_recent = 4
        agent._record({"role": "user", "content": "原始任务"})
        for index in range(8):
            agent._record({"role": "user", "content": f"旧问题{index}"})
            agent._record({"role": "assistant", "content": "应被摘要替代的旧正文" * 500})
        agent.compactor.summarizer = lambda *_: "摘要：旧步骤已完成，保留原始任务继续。"
        compact = agent.maybe_compact
        expected = []

        class RestartNow(BaseException):
            pass

        def compact_after_truncation(*args, **kwargs):
            if not agent._length_truncated:
                return None
            compact(force=True)
            expected.extend(copy.deepcopy(agent.messages[1:]))
            raise RestartNow()

        with mock.patch.object(agent, "maybe_compact", compact_after_truncation):
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RestartNow):
                agent.send("请继续说明")
        log.release()
        restored = load_messages(log.path)
        self.assertEqual(restored, expected)
        self.assertEqual(restored.corrupt_lines, [])
        self.assertIn("摘要：旧步骤已完成", str(restored))
        self.assertIn("最近的半截回答", str(restored))
        self.assertLess(str(restored).count("应被摘要替代的旧正文"), 8 * 500)
        resumed_log = self.log()
        resumed = self.build([[chunk(content="本轮已结束。")]], session_log=resumed_log)
        with contextlib.redirect_stdout(io.StringIO()):
            resumed.restore(restored, source=str(log.path), copy=False)
            resumed.send("接着说")
        self.assertEqual(len(self.client.completions.calls), 1)
        sent = self.client.completions.calls[0]["messages"]
        self.assertIn("最近的半截回答", str(sent))
        self.assertEqual(str(sent).count("摘要：旧步骤已完成"), 1)
        self.assert_tool_pairs(sent)
        self.assertEqual(load_messages(log.path), resumed.messages[1:])

    def test_side_effect_before_torn_result_is_not_reexecuted_on_resume(self):
        log = self.log()
        marker = self.root / "side-effect.txt"
        def action():
            marker.write_text(marker.read_text(encoding="utf-8", errors="replace") + "once\n"
                              if marker.exists() else "once\n", encoding="utf-8")
            return "已执行"
        tool = Tool("probe", "本机副作用夹具", {"type": "object", "properties": {}}, action)
        agent = self.build([], session_log=log)
        agent.toolbox.register(tool)
        call = {"id": "c1", "type": "function", "function": {"name": "probe", "arguments": "{}"}}
        agent._record({"role": "user", "content": "执行一次"})
        agent._record({"role": "assistant", "content": "", "tool_calls": [call]})
        with contextlib.redirect_stdout(io.StringIO()):
            result = agent._execute(call)
        # 动作已经完成，模拟结果记录只写了半行就断电；不伪造一个完整 tool 结果。
        with log.path.open("ab") as handle:
            handle.write(json.dumps(result, ensure_ascii=False).encode("utf-8")[:20])
        log.release()
        resumed_log = self.log()
        resumed = self.build([[chunk(content="需要先核实上次的执行结果。")]], session_log=resumed_log)
        resumed.toolbox.register(tool)
        with contextlib.redirect_stdout(io.StringIO()):
            resumed.restore(load_messages(log.path), source=str(log.path), copy=False)
            resumed.send("继续")
        self.assertEqual(marker.read_text(encoding="utf-8", errors="replace"), "once\n")
        results = [m for m in resumed.messages if m.get("tool_call_id") == "c1"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["content"], Agent.UNKNOWN_TOOL_OUTCOME)
        self.assert_tool_pairs(self.client.completions.calls[0]["messages"])
        self.assertEqual(load_messages(log.path), resumed.messages[1:])
        self.assertEqual(load_messages(log.path).corrupt_lines, [])
        resumed_log.release()
        # 再恢复一次仍只有一个结果占位，修复记录不会每次重复追加。
        again_log = self.log()
        again = self.build([], session_log=again_log)
        with contextlib.redirect_stdout(io.StringIO()):
            again.restore(load_messages(log.path), source=str(log.path), copy=False)
        self.assertEqual(load_messages(log.path), again.messages[1:])
        self.assertEqual(len([m for m in again.messages if m.get("tool_call_id") == "c1"]), 1)
