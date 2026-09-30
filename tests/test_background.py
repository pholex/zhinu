"""后台任务三件套的测试：任务表、完成通知、task_output/kill_task/monitor、限流。

真实起子进程（echo / sleep 这类瞬时命令），不打网络。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_admission_cap_refuses_until_a_slot_frees(self):
        with mock.patch.object(bg, "MAX_RUNNING_TASKS", 2):
            first = self.start("sleep 30")
            self.start("sleep 30")
            refused = self.manager.start(["/bin/sh", "-c", "sleep 30"], command="sleep 30")
            self.assertIsInstance(refused, str)
            self.assertTrue(refused.startswith("ERROR"), refused)
            self.assertIn(first.task_id, refused)
            self.assertIn("kill_task", refused)
            self.manager.kill(first.task_id)
            #  终止中的任务仍占名额，进程真正退出后才释放
            self.assertTrue(wait_until(first.done.is_set))
            self.start("true")

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

    def test_shutdown_reaps_what_it_kills(self):
        #  收场的那一刻就得收干净，不能指望 watcher 线程事后去做：
        #  进程退出时它没有运行机会，子 agent 收工时调用方紧接着就往下走了
        tasks = [self.start("sleep 30") for _ in range(3)]
        with mock.patch.object(bg.TaskManager, "_watch", lambda *args, **kwargs: None):
            late = self.start("sleep 30")  # 这一个压根没有 watcher
        started = time.monotonic()
        self.manager.shutdown()
        self.assertLess(time.monotonic() - started, bg.TaskManager.SHUTDOWN_REAP_SECONDS + 2)
        for task in [*tasks, late]:
            self.assertIsNotNone(task.proc.returncode, f"{task.task_id} 被杀了却没被收掉")

    def test_shutdown_gives_up_on_a_process_that_will_not_die(self):
        task = self.start("sleep 30")
        with mock.patch.object(bg, "kill_tree", lambda proc: None), \
                mock.patch.object(bg.TaskManager, "SHUTDOWN_REAP_SECONDS", 0.3):
            started = time.monotonic()
            self.manager.shutdown()
            self.assertLess(time.monotonic() - started, 2.0)  # 等不到就放手，不卡住收场
        bg.kill_tree(task.proc)
        task.proc.wait(timeout=5)

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

    def test_polling_script_wakes_the_model_once_per_change(self):
        task = self.start(
            "for i in 1 2 3 4 5; do echo waiting; sleep 0.3; done; echo DONE",
            kind="monitor", description="轮询",
        )
        self.assertTrue(wait_until(task.done.is_set, timeout=15))
        self.assertTrue(
            wait_until(lambda: any("DONE" in t for t in self.notify.texts()))
        )
        events = [t for t in self.notify.texts() if "monitor-event" in t]
        waiting = sum(text.count("\nwaiting") for text in events)
        self.assertEqual(waiting, 1, events)
        self.assertTrue(any("又原样出现了 4 次" in text for text in events), events)

    def test_still_running_line(self):
        self.assertEqual(self.manager.still_running_line(), "")
        task = self.start("sleep 30")
        monitor = self.start("sleep 30", kind="monitor", description="watch")
        line = self.manager.still_running_line()
        self.assertIn("1 个后台任务", line)
        self.assertIn("1 个 monitor", line)
        self.manager.kill(task.task_id)
        self.manager.kill(monitor.task_id)


@unittest.skipUnless(POSIX, "用例依赖 POSIX shell")
class BoundedLogTest(unittest.TestCase):
    """前台命令的输出有界，后台这条路也得有。"""

    def setUp(self):
        self.manager = bg.TaskManager()
        self.notify = Collector()
        self.manager.notify = self.notify
        self.addCleanup(self.manager.shutdown)

    def start(self, command: str, **kwargs):
        task = self.manager.start(["/bin/sh", "-c", command], command=command, **kwargs)
        self.assertNotIsInstance(task, str, task)
        return task

    def test_large_log_is_read_as_head_and_tail(self):
        task = self.start("printf 'HEAD-MARK\\n'; yes filler | head -c 300000; printf 'TAIL-MARK\\n'")
        self.assertTrue(task.done.wait(10))
        with mock.patch.object(bg, "OUTPUT_READ_BYTES", 4096):
            text = self.manager.output_of(task)
        self.assertLess(len(text), 6000)
        self.assertIn("HEAD-MARK", text)
        self.assertIn("TAIL-MARK", text)
        self.assertIn("中间省略", text)
        self.assertIn(str(task.log_path), text)

    def test_small_log_is_returned_whole(self):
        task = self.start("echo one; echo two")
        self.assertTrue(task.done.wait(10))
        self.assertEqual(self.manager.output_of(task).split(), ["one", "two"])

    def test_runaway_output_gets_the_task_stopped(self):
        #  输出必须有界：用无限输出的命令的话，终止一旦没杀到它，它就一直写到把
        #  磁盘写满（sh 把命令当子进程跑的系统上，只杀 sh 杀不到它）。这里写够
        #  越线的量就停下睡着——杀没杀到都不会出事，而"被终止"仍然测得出来。
        #  进程组与生产路径一致：生产上后台任务都是独立进程组，终止按组杀
        with mock.patch.object(bg, "MAX_LOG_BYTES", 50_000), \
                mock.patch.object(bg, "LOG_CHECK_INTERVAL", 0.1):
            task = self.start(
                "yes 失控打印 | head -c 400000; sleep 60",
                popen_extra={"start_new_session": True},
            )
            self.assertTrue(task.done.wait(15), "日志超限后任务应被终止")
        self.assertLess(task.elapsed(), 30)
        self.assertIn("上限", task.stopped_for)
        self.assertTrue(wait_until(lambda: any("被终止" in t for t in self.notify.texts())))
        (text,) = [t for t in self.notify.texts() if task.task_id in t]
        self.assertIn("日志超过", text)

    def test_timeout_still_stops_the_task(self):
        with mock.patch.object(bg, "LOG_CHECK_INTERVAL", 0.1):
            task = self.start("sleep 30", timeout=0.5)
            self.assertTrue(task.done.wait(15))
        self.assertIn("超时上限", task.stopped_for)
        self.assertNotEqual(task.exit_code, 0)

    def test_quiet_task_is_left_alone(self):
        with mock.patch.object(bg, "LOG_CHECK_INTERVAL", 0.05):
            task = self.start("sleep 0.4; echo done")
            self.assertTrue(task.done.wait(10))
        self.assertEqual(task.exit_code, 0)
        self.assertEqual(task.stopped_for, "")


class RepeatFilterTest(unittest.TestCase):
    """原样重复的行不是新事件：不为它单独唤醒模型，但出现过几次要交代。"""

    def test_consecutive_repeats_are_folded_and_counted(self):
        repeats = bg._RepeatFilter()
        self.assertEqual(repeats.filter(["still running"]), ["still running"])
        #  之后的轮询一直是同一行：什么都不发
        self.assertEqual(repeats.filter(["still running"]), [])
        self.assertEqual(repeats.filter(["still running", "still running"]), [])
        #  换了内容：先交代上一行又出现了几次
        self.assertEqual(
            repeats.filter(["DONE"]), ["（上一行又原样出现了 3 次）", "DONE"]
        )

    def test_only_consecutive_repeats_count(self):
        repeats = bg._RepeatFilter()
        self.assertEqual(repeats.filter(["A", "B", "A"]), ["A", "B", "A"])

    def test_pending_count_is_flushed_when_the_monitor_ends(self):
        repeats = bg._RepeatFilter()
        repeats.filter(["waiting", "waiting"])
        self.assertEqual(repeats.filter([], flush=True), ["（上一行又原样出现了 1 次）"])
        self.assertEqual(repeats.filter([], flush=True), [])


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


class ReadNewTest(unittest.TestCase):
    """日志按字节偏移增量读：多字节字符横跨读块边界不能被切成 �。"""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "log.txt"

    def tearDown(self):
        self.tmp.cleanup()

    def _drain(self, chunk: int, flush_last: bool = False) -> str:
        """按小块反复读到文件尾，拼出全部文本。"""
        offset, out = 0, ""
        size = self.path.stat().st_size
        for _ in range(size * 2 + 4):
            text, offset = bg.TaskManager._read_new(self.path, offset, chunk=chunk)
            out += text
            if offset >= size:
                break
        return out

    def test_multibyte_char_across_read_boundary(self):
        # 汉字 3 字节；块长 4 让"文""输""出"都被切在块中间
        content = "a中文输出b\n"
        self.path.write_bytes(content.encode("utf-8"))
        out = self._drain(chunk=4)
        self.assertNotIn("�", out)
        self.assertEqual(out, content)

    def test_partial_tail_waits_for_next_poll(self):
        data = "中".encode("utf-8")
        self.path.write_bytes(data[:2])
        text, offset = bg.TaskManager._read_new(self.path, 0)
        #  进程还在跑：残缺尾巴留到下次，偏移不越过它
        self.assertEqual((text, offset), ("", 0))
        self.path.write_bytes(data + b"\n")
        text, offset = bg.TaskManager._read_new(self.path, offset)
        self.assertEqual((text, offset), ("中\n", 4))

    def test_flush_decodes_everything(self):
        data = "中".encode("utf-8")
        self.path.write_bytes(data[:2])
        text, offset = bg.TaskManager._read_new(self.path, 0, flush=True)
        #  进程已结束的最后一读：残缺字节再也等不来后半截，照有损解码交出去
        self.assertEqual(offset, 2)
        self.assertEqual(text, "�")

    def test_ascii_and_garbage_unchanged(self):
        self.path.write_bytes(b"plain ascii\n")
        self.assertEqual(
            bg.TaskManager._read_new(self.path, 0), ("plain ascii\n", 12)
        )
        #  尾部三个孤立续字节不是"未完成序列"，照常推进，绝不卡在原地
        self.path.write_bytes(b"x\x80\x80\x80")
        text, offset = bg.TaskManager._read_new(self.path, 0)
        self.assertEqual(offset, 4)
        self.assertTrue(text.startswith("x"))


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
                model="deepseek-flash",
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
