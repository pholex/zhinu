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


class WaitStallTest(StallDetectionTest):
    """先 sleep 再查、结果却和等待前一样：每次 sleep 秒数不同、退出码又是 0，
    打转检测和无进展检测都看不见。"""

    CLOSED = "exit_status: 0\nstdout:\nConnection closed by 203.0.113.7 port 22"
    HEALTHY = 'exit_status: 0\nstdout:\n[\n    {\n        "State": "running"\n    }\n]'
    SSH = "ssh -o ConnectTimeout=8 host 'uptime' 2>&1 | tail -5"

    def build_with_fake_bash(self, outputs: list[str]):
        agent = super().build_with_fake_bash(outputs)
        queue = list(outputs)
        #  真 bash 还收 timeout / run_in_background：假的照单全收
        agent.toolbox.get("bash").handler = lambda command, **_: queue.pop(0)
        return agent

    def test_replay_of_the_reboot_session(self) -> None:
        """实测会话的命令序列：重启后查到健康，又自作主张去连 SSH，越等越久。"""
        verbose = "exit_status: 0\nstdout:\ndebug1: Connecting\nConnection closed by 203.0.113.7 port 22"
        listed = "exit_status: 0\nstdout:\n-rw------- 1 u staff 1679 key.pem\nConnection closed by 203.0.113.7 port 22"
        agent = self.build_with_fake_bash([self.HEALTHY, self.CLOSED, self.CLOSED, verbose, listed, self.CLOSED])
        outputs = [
            self._run(agent, "bash", {"command": "sleep 45; ops reboot i-1 --status 2>&1 | tail -20", "timeout": 120}),
            self._run(agent, "bash", {"command": self.SSH, "timeout": 40}),
            self._run(agent, "bash", {"command": f"sleep 40; {self.SSH}", "timeout": 90}),
            self._run(agent, "bash", {"command": f"awk 1 ~/.ssh/config; ssh -vv host uptime 2>&1 | tail -25"}),
            self._run(agent, "bash", {"command": f"ls -l key.pem; sleep 60; {self.SSH}", "timeout": 120}),
            self._run(agent, "bash", {"command": f"sleep 90; for i in 1 2; do {self.SSH}; sleep 10; done"}),
        ]
        #  第一次等完没变不提示（刚重启，再等一次说得过去）；第二次起提示
        self.assertTrue(all("[提示]" not in text for text in outputs[:4]), outputs[:4])
        self.assertIn("2 次先 sleep 再查（共等了约 100 秒）", outputs[4])
        self.assertIn("如实告诉用户", outputs[4])
        self.assertIn("二选一", outputs[5])

    def test_trailing_exit_echo_and_loops_do_not_hide_the_unchanged_result(self) -> None:
        """替身环境实测到的两种写法：命令尾巴接 `echo "exit=$?"`，以及把几次
        重试写进一条 for 循环——字面末行都不是那句结论。"""
        closed = "Connection closed by 203.0.113.7 port 22"
        once = f"exit_status: 0\nstdout:\n{closed}\nssh_exit=255"
        looped = "exit_status: 0\nstdout:\n" + "".join(
            f"--- attempt {i} ---\n{closed}\nexit=255\n" for i in (1, 2, 3))
        agent = self.build_with_fake_bash([once, once, looped])
        outputs = [
            self._run(agent, "bash", {"command": 'ssh h uptime; echo "ssh_exit=$?"'}),
            self._run(agent, "bash", {"command": 'sleep 20; ssh h uptime; echo "ssh_exit=$?"'}),
            self._run(agent, "bash", {"command": 'for i in 1 2 3; do sleep 25; ssh h uptime; echo "exit=$?"; done'}),
        ]
        self.assertNotIn("[提示]", outputs[1])
        self.assertIn("2 次先 sleep 再查", outputs[2])

    def test_waiting_that_changes_the_result_is_silent(self) -> None:
        pending = "exit_status: 0\nstdout:\ndeployment is still rolling out"
        agent = self.build_with_fake_bash([pending, "exit_status: 0\nstdout:\ndeployment finished ok"])
        outputs = [self._run(agent, "bash", {"command": f"sleep {n}; kubectl rollout status"}) for n in (20, 30)]
        self.assertTrue(all("[提示]" not in text for text in outputs), outputs)

    def test_short_pauses_background_and_bare_closers_do_not_count(self) -> None:
        agent = self.build_with_fake_bash([self.CLOSED] * 4 + [self.HEALTHY] * 3)
        outputs = [self._run(agent, "bash", {"command": f"sleep 2; probe {i}"}) for i in range(4)]
        #  后台任务里的 sleep 不占着这一轮，结果相同也不算空等
        outputs += [self._run(agent, "bash", {"command": f"sleep 30; status {i}", "run_in_background": True})
                    for i in range(3)]
        self.assertTrue(all("[提示]" not in text for text in outputs), outputs)
        self.assertEqual(agent._wait_unchanged, 0)

    def test_tail_only_match_needs_substance(self) -> None:
        first = 'exit_status: 0\nstdout:\n[\n  {"State": "pending"}\n]'
        second = 'exit_status: 0\nstdout:\n[\n  {"State": "running"}\n]'
        agent = self.build_with_fake_bash([first, second, second])
        outputs = [self._run(agent, "bash", {"command": f"sleep 30; status #{i}"}) for i in range(3)]
        #  第二次：整段不同，最后一行有实质内容的行（State）也不同；第三次才算没变
        self.assertEqual(agent._wait_unchanged, 1)
        self.assertTrue(all("[提示]" not in text for text in outputs), outputs)

    def test_counters_reset_each_turn(self) -> None:
        agent = self.build_with_fake_bash([self.CLOSED] * 3)
        for n in (0, 40, 60):
            self._run(agent, "bash", {"command": (f"sleep {n}; " if n else "") + self.SSH})
        self.assertEqual(agent._wait_unchanged, 2)
        self.client.completions.script.append([__import__("tests.test_agent_paths", fromlist=["chunk"]).chunk(content="好")])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("下一件事")
        self.assertEqual((agent._wait_unchanged, agent._wait_seen), (0, set()))

    def test_sleep_seconds_parsing(self) -> None:
        from xiaoyu.agent import Agent

        cases = {
            "sleep 45; ops status": 45, "ls; sleep 60 && ssh h": 60, "sleep 1m; x": 60,
            "sleep 90; for i in 1 2; do ssh h; sleep 10; done": 100, "sleep 0.5": 0.5,
            "echo sleep 30": 0, "nosleep 30": 0, "sleep $N; x": 0, "(sleep 20; x)": 20,
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertEqual(Agent._slept_seconds(command), expected)


class SleepInsensitiveRepeatTest(ProgressGuardTestCase):
    def test_only_changing_the_leading_sleep_is_the_same_call(self) -> None:
        agent = self.build([])
        keys = {
            agent._repeat_args("bash", json.dumps(args)) for args in (
                {"command": "ssh h uptime"},
                {"command": "sleep 40; ssh h uptime", "timeout": 90},
                {"command": "sleep 90 && sleep 5; ssh h uptime", "timeout": 180},
            )
        }
        self.assertEqual(len(keys), 1)
        self.assertEqual([agent._track_repeat("bash", json.dumps({"command": f"sleep {n}; ssh h uptime"}))
                          for n in (10, 20, 30)], [1, 2, 3])

    def test_other_differences_still_count_as_new_calls(self) -> None:
        agent = self.build([])
        different = [
            {"command": "sleep 40; ssh h uptime"},
            {"command": "sleep 40; ssh -v h uptime"},
            {"command": "sleep 40; ssh h uptime", "run_in_background": True},
            {"command": "ls; sleep 40; ssh h uptime"},
        ]
        self.assertEqual(len({agent._repeat_args("bash", json.dumps(a)) for a in different}), 4)
        #  别的工具、解析不了的参数原样比较
        self.assertEqual(agent._repeat_args("read_file", '{"path": "a"}'), '{"path": "a"}')
        self.assertEqual(agent._repeat_args("bash", "不是 JSON"), "不是 JSON")


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
