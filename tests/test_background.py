"""后台任务三件套的测试：任务表、完成通知、task_output/kill_task/monitor、限流。

真实起子进程（echo / sleep 这类瞬时命令），不打网络。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

from xiaoyu import background as bg
from xiaoyu.config import Config
from xiaoyu.tools import Toolbox

#  Windows 上这些用 sh 语法的用例没意义；本仓测试机是 macOS/Linux
POSIX = sys.platform != "win32"


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


class Collector:
    """线程安全的 notify 收集器。"""

    def __init__(self) -> None:
        self.items: list[tuple[str, str]] = []
        self._lock = threading.Lock()

    def __call__(self, text: str, key: str) -> None:
        with self._lock:
            self.items.append((text, key))

    def texts(self) -> list[str]:
        with self._lock:
            return [text for text, _ in self.items]


@unittest.skipUnless(POSIX, "用例依赖 POSIX shell")
class TaskManagerTest(unittest.TestCase):
    def setUp(self):
        self.manager = bg.TaskManager()
        self.notify = Collector()
        self.manager.notify = self.notify
        self.addCleanup(self.manager.shutdown)

    def start(self, command: str, **kwargs):
        task = self.manager.start(["/bin/sh", "-c", command], command=command, **kwargs)
        self.assertNotIsInstance(task, str, task)
        return task

    def test_completion_notifies_with_dedup_key(self):
        task = self.start("echo hello")
        self.assertTrue(wait_until(task.done.is_set))
        self.assertTrue(wait_until(lambda: bool(self.notify.items)))
        text, key = self.notify.items[0]
        self.assertIn("已完成", text)
        self.assertIn(task.task_id, text)
        self.assertIn("task_output", text)
        self.assertEqual(key, f"task-done-{task.task_id}")
        self.assertEqual(task.status, "completed")
        self.assertIn("hello", self.manager.output_of(task))

    def test_failed_exit_code_and_fast_exit_hint(self):
        task = self.start("exit 3")
        self.assertTrue(wait_until(task.done.is_set))
        self.assertTrue(wait_until(lambda: bool(self.notify.items)))
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.exit_code, 3)
        self.assertIn("不到 1 秒", self.notify.texts()[0])

    def test_kill_suppresses_completion_notice(self):
        task = self.start("sleep 30")
        result = self.manager.kill(task.task_id)
        self.assertIn("已终止", result)
        self.assertTrue(wait_until(task.done.is_set))
        self.assertEqual(task.status, "cancelled")
        time.sleep(0.2)
        self.assertEqual(self.notify.items, [])

    def test_kill_unknown_and_finished(self):
        self.assertIn("没有名为", self.manager.kill("task-99"))
        task = self.start("true")
        self.assertTrue(wait_until(task.done.is_set))
        self.assertIn("早已结束", self.manager.kill(task.task_id))

    def test_timeout_kills_task(self):
        task = self.start("sleep 30", timeout=0.3)
        self.assertTrue(wait_until(task.done.is_set, timeout=15))
        self.assertNotEqual(task.exit_code, 0)

    def test_monitor_events_arrive_and_completion(self):
        task = self.start(
            "echo DONE; echo TAIL", kind="monitor", description="盯着测试",
        )
        self.assertTrue(wait_until(task.done.is_set))
        self.assertTrue(
            wait_until(lambda: any("monitor-event" in t for t in self.notify.texts()))
        )
        event = next(t for t in self.notify.texts() if "monitor-event" in t)
        self.assertIn("DONE", event)
        self.assertIn('description="盯着测试"', event)
        self.assertIn(task.task_id, event)
        #  自然结束也有一条收尾通知
        self.assertTrue(
            wait_until(lambda: any("已结束" in t for t in self.notify.texts()))
        )

    def test_still_running_line(self):
        self.assertEqual(self.manager.still_running_line(), "")
        task = self.start("sleep 30")
        monitor = self.start("sleep 30", kind="monitor", description="watch")
        line = self.manager.still_running_line()
        self.assertIn("1 个后台任务", line)
        self.assertIn("1 个 monitor", line)
        self.manager.kill(task.task_id)
        self.manager.kill(monitor.task_id)


class RateLimiterTest(unittest.TestCase):
    def test_bucket_suppresses_then_reports(self):
        limiter = bg._RateLimiter()
        allowed = sum(1 for _ in range(20) if limiter.allow()[0])
        self.assertEqual(allowed, bg._BUCKET_CAPACITY)
        self.assertGreater(limiter.suppressed, 0)
        #  手动补满令牌模拟时间流逝：恢复的第一条要带"被丢弃 N 条"的说明
        limiter.tokens = 1.0
        ok, note = limiter.allow()
        self.assertTrue(ok)
        self.assertIn("被丢弃", note)

    def test_auto_kill_after_sustained_suppression(self):
        limiter = bg._RateLimiter()
        limiter.tokens = 0.0
        limiter.last_refill = time.monotonic()
        self.assertFalse(limiter.allow()[0])
        self.assertFalse(limiter.should_auto_kill())
        limiter.suppressing_since = time.monotonic() - bg._AUTO_KILL_SECONDS - 1
        self.assertTrue(limiter.should_auto_kill())


class SplitLinesTest(unittest.TestCase):
    def test_partial_line_buffered_until_flush(self):
        lines, rest = bg.TaskManager._split_lines("a\nb\nhalf", flush=False)
        self.assertEqual(lines, ["a", "b"])
        self.assertEqual(rest, "half")
        lines, rest = bg.TaskManager._split_lines("half", flush=True)
        self.assertEqual(lines, ["half"])
        self.assertEqual(rest, "")

    def test_long_line_truncated(self):
        lines, _ = bg.TaskManager._split_lines("x" * 1000 + "\n", flush=False)
        self.assertIn("单行过长已截断", lines[0])


@unittest.skipUnless(POSIX, "用例依赖 POSIX shell")
class ToolboxBackgroundTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = Config(
            base_url="x",
            model="x",
            workspace=Path(self.tmp.name).resolve(),
            enable_plugins=False,
            enable_mcp=False,
            sandbox=False,
        )
        self.toolbox = Toolbox(config)
        self.notify = Collector()
        self.toolbox.tasks.notify = self.notify
        self.addCleanup(self.toolbox.tasks.shutdown)

    def test_run_in_background_returns_task_id(self):
        output = self.toolbox.run("bash", {"command": "echo bg", "run_in_background": True})
        self.assertIn("后台任务已启动：task-1", output)
        self.assertIn("task_output", output)
        task = self.toolbox.tasks.get("task-1")
        self.assertIsNotNone(task)
        self.assertTrue(wait_until(task.done.is_set))

    def test_trailing_ampersand_rejected(self):
        output = self.toolbox.run(
            "bash", {"command": "sleep 5 &", "run_in_background": True}
        )
        self.assertIn("不要在命令末尾写 &", output)

    def test_hardline_applies_to_background(self):
        output = self.toolbox.run(
            "bash", {"command": "rm -rf /", "run_in_background": True}
        )
        self.assertIn("硬性拦截", output)

    def test_second_task_warns_about_running_ones(self):
        first = self.toolbox.run(
            "bash", {"command": "sleep 30", "run_in_background": True}
        )
        self.assertIn("task-1", first)
        second = self.toolbox.run("bash", {"command": "echo hi", "run_in_background": True})
        self.assertIn("还有 1 个后台任务在跑", second)
        self.toolbox.tasks.kill("task-1")

    def test_task_output_snapshot_wait_and_not_found(self):
        self.toolbox.run("bash", {"command": "echo done-marker", "run_in_background": True})
        output = self.toolbox.run(
            "task_output", {"task_ids": ["task-1"], "timeout": 10}
        )
        self.assertIn("task-1：completed", output)
        self.assertIn("done-marker", output)
        missing = self.toolbox.run("task_output", {"task_ids": ["task-9"]})
        self.assertIn("not_found", missing)
        #  宽进：裸字符串也收
        again = self.toolbox.run("task_output", {"task_ids": "task-1"})
        self.assertIn("completed", again)

    def test_task_tools_hidden_until_first_task(self):
        names = [schema["function"]["name"] for schema in self.toolbox.schemas()]
        self.assertNotIn("task_output", names)
        self.assertNotIn("kill_task", names)
        self.assertIn("monitor", names)
        self.toolbox.run("bash", {"command": "true", "run_in_background": True})
        names = [schema["function"]["name"] for schema in self.toolbox.schemas()]
        self.assertIn("task_output", names)
        self.assertIn("kill_task", names)

    def test_monitor_tool_starts_and_reports(self):
        output = self.toolbox.run(
            "monitor", {"command": "echo DONE", "description": "test-watch"}
        )
        self.assertIn("monitor 已启动", output)
        self.assertIn("不要轮询", output)
        task = self.toolbox.tasks.get("task-1")
        self.assertEqual(task.kind, "monitor")
        self.assertTrue(wait_until(task.done.is_set))


class AgentWiringTest(unittest.TestCase):
    def test_agent_injects_notify_into_task_manager(self):
        #  不跑真模型：只验证构造后 toolbox.tasks.notify 就是 agent.notify
        import tempfile

        from xiaoyu.agent import Agent
        from xiaoyu.providers import Registry, Provider

        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                base_url="http://localhost:9",
                model="deepseek-v4-pro",
                workspace=Path(tmp).resolve(),
                enable_plugins=False,
                enable_mcp=False,
                enable_skills=False,
                enable_explore=False,
                enable_web_search=False,
                enable_agents=False,
                enable_hooks=False,
            )
            registry = Registry([Provider(name="gateway", base_url="http://localhost:9", api_key="k")])
            agent = Agent(config, registry=registry)
            self.assertEqual(agent.toolbox.tasks.notify, agent.notify)


if __name__ == "__main__":
    unittest.main()
