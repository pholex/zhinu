"""无进展检测与压缩后重读提示。"""

from __future__ import annotations

import contextlib
import io
import json

from xiaoyu.tools import Tool

from .test_agent_paths import AgentTestCase


class ProgressGuardTestCase(AgentTestCase):
    def _run(self, agent, name: str, args: dict, call_id: str = "c") -> str:
        call = {"id": call_id, "function": {"name": name, "arguments": json.dumps(args)}}
        with contextlib.redirect_stdout(io.StringIO()):
            return agent._execute(call)["content"]


class StallDetectionTest(ProgressGuardTestCase):
    def build_with_fake_bash(self, outputs: list[str]):
        agent = self.build([])
        queue = list(outputs)
        agent.toolbox.register(
            Tool(
                name="bash",
                description="假 bash",
                parameters={"type": "object", "properties": {"command": {"type": "string"}}},
                handler=lambda command: queue.pop(0),
                requires_approval=False,
            )
        )
        return agent

    def failure(self, stamp: int) -> str:
        return f"exit_status: 1\nstderr:\n2026-09-14 10:{stamp:02d}:01 FAIL test_x ({stamp}.{stamp}ms) pid {4000 + stamp}"

    def test_same_failure_modulo_numbers_is_flagged(self) -> None:
        agent = self.build_with_fake_bash([self.failure(i) for i in range(5)])
        outputs = [self._run(agent, "bash", {"command": f"pytest -k x -v{'v' * i}"}) for i in range(5)]
        self.assertNotIn("[提示]", outputs[1])
        self.assertIn("除数字、时间戳外", outputs[2])
        self.assertNotIn("[提示]", outputs[3])
        self.assertIn("三选一", outputs[4])

    def test_success_or_different_failure_resets(self) -> None:
        agent = self.build_with_fake_bash(
            [self.failure(1), self.failure(2), "exit_status: 0\nstdout:\nok",
             self.failure(3), "exit_status: 1\nstderr:\n全新的错误", self.failure(4)]
        )
        outputs = [self._run(agent, "bash", {"command": f"cmd {i}"}) for i in range(6)]
        self.assertTrue(all("[提示]" not in text for text in outputs), outputs)

    def test_other_tools_in_between_do_not_reset(self) -> None:
        """改一处再重跑、结果不变——中间的编辑恰恰不该清零。"""
        agent = self.build_with_fake_bash([self.failure(i) for i in range(3)])
        outputs = []
        for index in range(3):
            outputs.append(self._run(agent, "bash", {"command": f"make test #{index}"}))
            self._run(agent, "list_files", {"pattern": f"*{index}.py"})
        self.assertIn("除数字、时间戳外", outputs[2])


class RereadAfterCompactionTest(ProgressGuardTestCase):
    def test_identical_reread_after_compaction_is_noted(self) -> None:
        agent = self.build([])
        first = self._run(agent, "read_file", {"path": "calc.py"})
        self.assertNotIn("[提示]", first)
        agent._mark_compaction()
        again = self._run(agent, "read_file", {"path": "calc.py"})
        self.assertIn("压缩前的一次调用", again)

    def test_changed_content_or_window_elapsed_is_silent(self) -> None:
        agent = self.build([])
        self._run(agent, "read_file", {"path": "calc.py"})
        agent._mark_compaction()
        (self.root / "calc.py").write_text("def add(a, b):\n    return b + a\n", encoding="utf-8")
        changed = self._run(agent, "read_file", {"path": "calc.py"})
        self.assertNotIn("压缩前的一次调用", changed)
        for index in range(3):
            self._run(agent, "list_files", {"pattern": f"*{index}"})
        late = self._run(agent, "read_file", {"path": "calc.py"})
        self.assertNotIn("压缩前的一次调用", late)
