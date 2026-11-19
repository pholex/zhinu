"""验收用例（题面里不出现）：检索子 agent 的逐步记录进父会话日志，正文不进，中断也收尾。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu.agent import Agent
from xiaoyu.session_inspect import inspect_session

from .test_agent_paths import AgentTestCase, chunk, usage_chunk
from .test_subagent_knobs import text_turn, tool_turn


class FakeLog:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def event(self, kind: str, **fields) -> None:
        self.records.append({"event": kind, **fields})

    def of(self, kind: str) -> list[dict]:
        return [r for r in self.records if r["event"] == kind]


class BrokenLog:
    def event(self, kind: str, **fields) -> None:
        raise OSError("磁盘满了")


class ExploreTraceTest(AgentTestCase):
    def explore(self, script, log, question="add 定义在哪"):
        agent = self.build(script)
        #  建完再挂：journal 是现取的，/clear、resume 换掉日志后也跟得上
        agent.session_log = log
        with contextlib.redirect_stdout(io.StringIO()):
            return agent.toolbox.get("explore").handler(question=question)

    def test_each_step_is_journaled_without_tool_output(self):
        log = FakeLog()
        result = self.explore(
            [
                tool_turn("a", "read_file", {"path": "calc.py"}) + [usage_chunk(1200, 40)],
                tool_turn("b", "read_file", {"path": "没有这个文件.py"}) + [usage_chunk(1500, 30)],
                [chunk(content="calc.py:1 def add(a, b):"), usage_chunk(1700, 20)],
            ],
            log,
        )
        self.assertIn("calc.py:1", result)

        (start,) = log.of("explore_start")
        self.assertEqual(start["question"], "add 定义在哪")

        requests = log.of("explore_request")
        self.assertEqual([r["step"] for r in requests], [1, 2, 3])
        self.assertEqual([r["prompt_tokens"] for r in requests], [1200, 1500, 1700])

        tools = log.of("explore_tool")
        self.assertEqual([(t["step"], t["tool"], t["ok"]) for t in tools],
                         [(1, "read_file", True), (2, "read_file", False)])
        #  正文不进父会话日志：留下的只有字数
        self.assertNotIn("return a + b", json.dumps(log.records, ensure_ascii=False))

        (end,) = log.of("explore_end")
        self.assertEqual(end["outcome"], "done")
        self.assertEqual((end["requests"], end["tools"], end["failed_tools"]), (3, 2, 1))
        #  同一次检索的记录共用一个 run，并发/连续多次检索才分得开
        self.assertEqual(len({r["run"] for r in log.records}), 1)

    def test_interrupt_still_writes_the_end_record(self):
        """Ctrl-C 一路抛给父级：检索没有结论，但日志里要有它停在哪的收尾记录。"""
        log = FakeLog()
        agent = self.build([])
        agent.session_log = log
        with mock.patch.object(Agent, "send", side_effect=KeyboardInterrupt), \
                contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            agent.toolbox.get("explore").handler(question="查 add")
        (end,) = log.of("explore_end")
        self.assertEqual(end["outcome"], "interrupted")
        self.assertEqual((end["requests"], end["tools"]), (0, 0))

    def test_sub_agent_failure_is_reported_and_journaled(self):
        log = FakeLog()
        agent = self.build([])
        agent.session_log = log
        with mock.patch.object(Agent, "send", side_effect=RuntimeError("上游挂了")), \
                contextlib.redirect_stdout(io.StringIO()):
            result = agent.toolbox.get("explore").handler(question="查 add")
        self.assertTrue(result.startswith("ERROR: 检索子 agent 失败"))
        self.assertEqual(log.of("explore_end")[0]["outcome"], "error")

    def test_no_session_log_means_no_trace_and_no_error(self):
        result = self.explore([text_turn("没找到")], None)
        self.assertIn("没找到", result)

    def test_journal_failure_does_not_break_the_search(self):
        result = self.explore(
            [tool_turn("a", "read_file", {"path": "calc.py"}), text_turn("calc.py:1 def add(a, b):")],
            BrokenLog(),
        )
        self.assertIn("calc.py:1", result)


class InspectTest(unittest.TestCase):
    def test_inspect_flags_failed_steps_as_errors(self):
        records = [
            {"event": "explore_start", "run": "r1", "question": "q"},
            {"event": "explore_request", "run": "r1", "step": 1, "ms": 900},
            {"event": "explore_tool", "run": "r1", "step": 1, "tool": "grep", "ok": True},
            {"event": "explore_tool", "run": "r1", "step": 1, "tool": "read_file", "ok": False},
            {"event": "explore_request", "run": "r1", "step": 2, "ms": 10, "error": "rate_limit"},
            {"event": "explore_end", "run": "r1", "outcome": "interrupted"},
            {"event": "explore_end", "run": "r2", "outcome": "done"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "s.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            self.assertEqual(inspect_session(path, kinds=("explore",))["matched"], 7)
            failed = inspect_session(path, errors_only=True)["rows"]
            self.assertEqual([row["line"] for row in failed], [4, 5, 6])
            #  检索子 agent 的请求不计入父会话的请求序号
            self.assertEqual(inspect_session(path)["requests"], 0)
