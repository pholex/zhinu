"""操作进行到一半失败时，不丢东西、不说假话。

每一组对应一条"半路出事"的路径：后台任务超时、一次性模式退出、委托被打断、
fork 继承、补位的工具结果、resume 后的后台任务、前台命令被叫停。
真实子进程一律有超时，且不产生无界输出。
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from xiaoyu import background as bg
from xiaoyu import worktree as worktree_mod
from xiaoyu.agent import Usage
from xiaoyu.agents import AgentSpec, RunStore, make_subagent_tool
from xiaoyu.cli import run_once

from .test_agent_paths import AgentTestCase, call_fragment, chunk

HAS_GIT = shutil.which("git") is not None

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
class BackgroundTimeoutNoticeTest(unittest.TestCase):
    """超时被杀的后台任务，通知里要说它是被杀的。"""

    def setUp(self):
        self.manager = bg.TaskManager()
        self.notify = Collector()
        self.manager.notify = self.notify
        self.addCleanup(self.manager.shutdown)

    def start(self, command: str, **kwargs):
        task = self.manager.start(["/bin/sh", "-c", command], command=command, **kwargs)
        self.assertNotIsInstance(task, str, task)
        return task

    def test_timed_out_task_is_not_announced_as_completed(self):
        task = self.start("sleep 30", timeout=0.3)
        self.assertTrue(task.done.wait(15))
        self.assertTrue(wait_until(lambda: bool(self.notify.items)))
        (text,) = self.notify.texts()
        self.assertIn("被终止", text)
        self.assertIn("超时上限", text)
        self.assertIn("0.3s", text)
        self.assertNotIn("已完成", text)

    def test_monitor_reaching_its_lifetime_says_so(self):
        task = self.start("sleep 30", kind="monitor", description="看着", timeout=0.3)
        self.assertTrue(task.done.wait(15))
        self.assertTrue(wait_until(lambda: any("被终止" in t for t in self.notify.texts())))
        (text,) = [t for t in self.notify.texts() if "被终止" in t]
        self.assertIn("最长观察时间", text)

    def test_task_finishing_inside_its_timeout_is_a_plain_completion(self):
        task = self.start("echo ok", timeout=20)
        self.assertTrue(task.done.wait(15))
        self.assertTrue(wait_until(lambda: bool(self.notify.items)))
        self.assertEqual(task.stopped_for, "")
        self.assertIn("已完成", self.notify.texts()[0])


class _OneShotAgent:
    """run_once 碰得到的那一小块 agent；send 时按剧本起后台任务。"""

    def __init__(self, commands: list[tuple[str, str]]) -> None:
        self.commands = commands
        self.usage = Usage()
        self.config = SimpleNamespace(model="m")
        self.session_log = None
        self.structured_output = None
        self.toolbox = SimpleNamespace(tasks=bg.TaskManager())
        self.started: list[bg.BackgroundTask] = []

    def send(self, prompt) -> None:
        for kind, command in self.commands:
            task = self.toolbox.tasks.start(
                ["/bin/sh", "-c", command], command=command, kind=kind,
                popen_extra={"start_new_session": True},
            )
            assert not isinstance(task, str), task
            self.started.append(task)

    def last_assistant_text(self) -> str:
        return "已在后台启动，完成后通知"


@unittest.skipUnless(POSIX, "用例依赖 POSIX shell")
class OneShotBackgroundReportTest(unittest.TestCase):
    """一次性模式退出时带走的后台任务，要在输出里看得见。"""

    def run_capture(self, agent: _OneShotAgent, output_format: str) -> tuple[int, str, str]:
        self.addCleanup(agent.toolbox.tasks.shutdown)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_once(agent, "干活", output_format)
        return code, out.getvalue(), err.getvalue()

    def test_json_result_lists_terminated_commands(self):
        agent = _OneShotAgent([("command", "sleep 30;  echo\n late")])
        code, out, _ = self.run_capture(agent, "json")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        (item,) = payload["background_tasks_terminated"]
        self.assertEqual(item["task_id"], "task-1")
        self.assertEqual(item["command"], "sleep 30; echo late")
        self.assertIsInstance(item["elapsed_seconds"], float)
        #  报出来的就是真的停了：不留到进程退出才杀
        self.assertIsNotNone(agent.started[0].proc.returncode)

    def test_stream_json_final_record_carries_the_field(self):
        agent = _OneShotAgent([("command", "sleep 30")])
        _, out, _ = self.run_capture(agent, "stream-json")
        payload = json.loads(out.splitlines()[-1])
        self.assertEqual(payload["kind"], "result")
        self.assertEqual(len(payload["background_tasks_terminated"]), 1)

    def test_text_mode_warns_on_stderr_only(self):
        agent = _OneShotAgent([("command", "sleep 30"), ("command", "sleep 31")])
        code, out, err = self.run_capture(agent, "text")
        self.assertEqual(code, 0)
        self.assertIn("2 个后台任务在退出时被终止", err)
        self.assertIn("sleep 30", err)
        self.assertIn("sleep 31", err)
        self.assertNotIn("被终止", out)

    def test_nothing_to_report_adds_nothing(self):
        #  跑完了的任务、monitor 都不算"被终止的后台任务"
        agent = _OneShotAgent([("command", "true"), ("monitor", "sleep 30")])
        original_send = agent.send

        def send(prompt):
            original_send(prompt)
            assert agent.started[0].done.wait(10)

        agent.send = send
        code, out, err = self.run_capture(agent, "json")
        self.assertEqual(code, 0)
        self.assertNotIn("background_tasks_terminated", json.loads(out))
        self.assertEqual(err, "")

    def test_agent_without_a_task_table_is_fine(self):
        agent = _OneShotAgent([])
        del agent.toolbox
        code, out, _ = self.run_capture_plain(agent)
        self.assertEqual(code, 0)
        self.assertNotIn("background_tasks_terminated", json.loads(out))

    def run_capture_plain(self, agent) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = run_once(agent, "干活", "json")
        return code, out.getvalue(), err.getvalue()


def delegate_call(call_id: str, name: str, task: str) -> list:
    return [chunk(tool_calls=[call_fragment(0, call_id, name, json.dumps({"task": task}))])]


def cut_off_stream(spoken: str, exc: BaseException):
    """说到一半被掐断的流（脚本项给生成器，假 client 原样交出去）。"""
    yield chunk(content=spoken)
    raise exc


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class InterruptedDelegationTest(AgentTestCase):
    """单发委托跑到一半被 Ctrl-C：打断照常上抛，但存档与有改动的 worktree 留得住。"""

    SPEC = AgentSpec(
        name="builder", description="d", system_prompt="工作区 {workspace}",
        tools=("read_file", "write_file"), isolation="worktree",
    )

    def setUp(self) -> None:
        super().setUp()
        for args in (
            ["init", "-q"],
            ["config", "user.email", "t@t"],
            ["config", "user.name", "t"],
            ["add", "-A"],
            ["commit", "-qm", "init"],
        ):
            subprocess.run(["git", "-C", str(self.root), *args], check=True, capture_output=True)
        #  worktree 基目录落在临时区，不碰真机配置
        patcher = mock.patch.object(worktree_mod, "user_config_dir", lambda: self.root / "cfg")
        patcher.start()
        self.addCleanup(patcher.stop)

    def delegate(self, sub_script: list, exc_type: type[BaseException]):
        store = RunStore()
        agent = self.build([delegate_call("d1", "builder", "写个文件"), *sub_script])
        agent.toolbox.register(
            make_subagent_tool(
                self.SPEC, self.config, agent.registry, agent.usage, agent.sink,
                agent.approver, agent.permissions, runs=store,
            )
        )
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(exc_type):
            agent.send("委托一下")
        return agent, store

    def test_ctrl_c_keeps_the_archive_and_the_dirty_worktree(self) -> None:
        write = json.dumps({"path": "new.txt", "content": "hi"})
        agent, store = self.delegate(
            [
                [chunk(tool_calls=[call_fragment(0, "w1", "write_file", write)])],
                cut_off_stream("写了一半", KeyboardInterrupt()),
            ],
            KeyboardInterrupt,
        )
        (run,) = store.values()
        self.assertIsNotNone(run.worktree)
        self.assertTrue((run.worktree / "new.txt").is_file())
        self.assertTrue(run.isolated)
        #  存档里是子 agent 真正走到的地方：写文件那一步在
        self.assertTrue(any(m.get("role") == "tool" for m in run.messages))
        #  句柄与路径作为这次委托调用的结果进了父会话历史
        (result,) = [m for m in agent.messages if m["role"] == "tool"]
        self.assertEqual(result["tool_call_id"], "d1")
        self.assertIn("没有做完", result["content"])
        self.assertIn(f"resume_from: {run.id}", result["content"])
        self.assertIn(str(run.worktree), result["content"])
        self.assertIn("写了一半", result["content"])

    def test_interrupted_run_with_nothing_written_leaves_no_worktree(self) -> None:
        agent, store = self.delegate(
            [cut_off_stream("还没动手", KeyboardInterrupt())], KeyboardInterrupt
        )
        (run,) = store.values()
        self.assertIsNone(run.worktree)
        base = self.root / "cfg" / "worktrees"
        self.assertEqual([p for p in base.rglob("*") if p.is_file()] if base.is_dir() else [], [])
        (result,) = [m for m in agent.messages if m["role"] == "tool"]
        self.assertIn(f"resume_from: {run.id}", result["content"])
        self.assertNotIn("worktree", result["content"])

    def test_exit_request_is_never_turned_into_a_failed_delegation(self) -> None:
        _, store = self.delegate([cut_off_stream("收到", SystemExit(143))], SystemExit)
        self.assertEqual(len(store), 1)


if __name__ == "__main__":
    unittest.main()
