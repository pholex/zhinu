"""interrupt() 的覆盖面：不只是流的 chunk 边界。

宿主（serve 的 /abort、ACP 的 session/cancel、预算硬闸）叫停一轮时，agent 可能
正卡在退避等待里、正等一条前台命令、正要跑同一批里的下一个工具、或者正等着
子 agent。这些地方都得停得下来。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import threading
import time
import unittest

from xiaoyu.agent import Interrupted
from xiaoyu.agents import AgentSpec, make_subagent_tool
from xiaoyu.agent import Usage
from xiaoyu.fanout import Attempt, run_attempts
from xiaoyu.providers import Registry
from xiaoyu.render import PlainSink

from .test_agent_paths import AgentTestCase, FakeClient, call_fragment, chunk


def tool_turn(call_id: str, name: str, args: dict) -> list:
    return [chunk(tool_calls=[call_fragment(0, call_id, name, json.dumps(args))])]


def later(seconds: float, action) -> threading.Timer:
    timer = threading.Timer(seconds, action)
    timer.daemon = True
    timer.start()
    return timer


class SameClassTest(unittest.TestCase):
    def test_interrupted_is_one_class_wherever_it_is_imported_from(self) -> None:
        from xiaoyu import errors, tools

        self.assertIs(Interrupted, errors.Interrupted)
        self.assertIs(tools.Interrupted, errors.Interrupted)
        #  刻意不是 KeyboardInterrupt：asyncio 对它有特殊处理，会捅穿宿主的事件循环
        self.assertFalse(issubclass(Interrupted, KeyboardInterrupt))


class BackoffTest(AgentTestCase):
    def test_interrupt_wakes_a_backoff_wait(self) -> None:
        agent = self.build([])
        later(0.2, agent.interrupt)
        started = time.monotonic()
        with self.assertRaises(Interrupted):
            agent._sleep(30)  # noqa: SLF001
        self.assertLess(time.monotonic() - started, 3)

    def test_uninterrupted_wait_runs_its_course(self) -> None:
        agent = self.build([])
        started = time.monotonic()
        agent._sleep(0.2)  # noqa: SLF001
        self.assertGreaterEqual(time.monotonic() - started, 0.2)


@unittest.skipIf(os.name == "nt", "用 sleep 当长命令")
class ForegroundCommandTest(AgentTestCase):
    def test_interrupt_stops_a_running_command_and_kills_it(self) -> None:
        self.config.sandbox = False
        script = [tool_turn("b1", "bash", {"command": "echo $$ > pid.txt; sleep 60"})]
        agent = self.build(script)
        later(0.8, agent.interrupt)
        started = time.monotonic()
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Interrupted):
            agent.send("跑个长命令")
        self.assertLess(time.monotonic() - started, 10)
        pid = int((self.root / "pid.txt").read_text(encoding="utf-8").strip())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            os.kill(pid, 9)
            self.fail("命令在打断之后还活着")
        #  悬空的调用补得上，下一轮发得出去
        agent.close_open_tool_calls("本轮被打断。")
        self.assertEqual(agent.messages[-1]["role"], "tool")


class BatchTest(AgentTestCase):
    def test_remaining_calls_in_the_batch_do_not_run(self) -> None:
        batch = [
            chunk(
                tool_calls=[
                    call_fragment(0, "w1", "write_file", json.dumps({"path": "one.txt", "content": "1"})),
                    call_fragment(1, "w2", "write_file", json.dumps({"path": "two.txt", "content": "2"})),
                ]
            )
        ]
        agent = self.build([batch])
        real_execute = agent._execute  # noqa: SLF001

        def execute_then_interrupt(call):
            message = real_execute(call)
            agent.interrupt()
            return message

        agent._execute = execute_then_interrupt  # noqa: SLF001
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(Interrupted):
            agent.send("写两个文件")
        self.assertTrue((self.root / "one.txt").is_file())
        self.assertFalse((self.root / "two.txt").exists(), "打断之后同批的下一个调用还是跑了")


class SubagentTest(AgentTestCase):
    SPEC = AgentSpec(
        name="digger", description="d", system_prompt="工作区 {workspace}",
        tools=("read_file",),
    )

    def test_child_stops_when_the_parent_is_interrupted(self) -> None:
        stopped = threading.Event()
        #  子 agent 每一步都要调工具：没人叫停就会一直跑到轮数上限
        script = [tool_turn(f"r{n}", "read_file", {"path": "calc.py"}) for n in range(40)]
        client = FakeClient(script)
        tool = make_subagent_tool(
            self.SPEC, self.config, Registry.for_client(client), Usage(),
            PlainSink(indent="", verbose=False), lambda name, args: True, None,
            stop_requested=stopped.is_set,
        )
        real_create = client.completions.create

        def create(**kwargs):
            if len(client.completions.calls) >= 3:
                stopped.set()
            return real_create(**kwargs)

        client.completions.create = create
        with contextlib.redirect_stdout(io.StringIO()):
            result = tool.handler(task="一直读")
        self.assertIn("Interrupted", result)
        self.assertLess(len(client.completions.calls), 8, "父级被打断后子 agent 还在跑")
        #  存档还在：打断不白跑
        self.assertRegex(result, r"resume_from: [0-9a-f]{8}")


class FanoutTest(unittest.TestCase):
    def test_parent_interrupt_stops_the_whole_batch(self) -> None:
        stopped = threading.Event()
        interrupted: list[int] = []

        class FakeAgent:
            def __init__(self, index: int) -> None:
                self.index = index
                self.halt = threading.Event()

            def interrupt(self) -> None:
                interrupted.append(self.index)
                self.halt.set()

        def make(index: int):
            def primary(register):
                agent = FakeAgent(index)
                register(agent)
                if not agent.halt.wait(30):
                    raise AssertionError("没人叫停")
                raise Interrupted("宿主请求打断")

            return primary

        attempts = [Attempt(index=n, primary=make(n)) for n in range(3)]
        later(0.5, stopped.set)
        started = time.monotonic()
        with self.assertRaises(Interrupted):
            run_attempts(attempts, concurrency=3, stop_requested=stopped.is_set)
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(sorted(interrupted), [0, 1, 2])


if __name__ == "__main__":
    unittest.main()
