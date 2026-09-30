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
from xiaoyu import agents as agents_mod
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


class ForkSeedTest(AgentTestCase):
    """fork 继承：正在进行的这次委托，在子 agent 眼里不该是一次"已放弃"的调用。"""

    ABANDONED = "按已放弃处理"

    @staticmethod
    def call(call_id: str, name: str = "worker") -> dict:
        return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}

    def test_forked_child_never_sees_its_own_delegation_as_abandoned(self) -> None:
        spec = AgentSpec(
            name="worker", description="干活", system_prompt="接着干，工作区 {workspace}",
            tools=("read_file", "grep", "list_files"), inherit="fork",
        )
        store = RunStore()
        agent = self.build([
            [chunk(content="读到了 add 函数")],
            delegate_call("d1", "worker", "接着干"),
            [chunk(content="子 agent 结论")],
            [chunk(content="主收尾")],
        ])
        agent.toolbox.register(
            make_subagent_tool(
                spec, self.config, agent.registry, agent.usage, agent.sink,
                agent.approver, agent.permissions, runs=store,
                parent_history=lambda: agent.messages,
            )
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("第一件事")
            agent.send("委托一下")
        #  第三次请求是子 agent 发的：看它实际发出去的历史（假 client 记的是活的
        #  列表，之后的答复也会追加进去——截到委托任务那一条为止）
        sent = self.client.completions.calls[2]["messages"]
        task_at = [m.get("content") for m in sent].index("接着干")
        sent = sent[: task_at + 1]
        self.assertFalse([m for m in sent if self.ABANDONED in str(m.get("content"))])
        self.assertFalse([m for m in sent if m.get("tool_calls") or m.get("role") == "tool"])
        #  父会话的上下文照样逐字带着
        self.assertTrue(any(m.get("content") == "第一件事" for m in sent))
        self.assertTrue(any(m.get("content") == "委托一下" for m in sent))
        #  存档里也没有这句话
        (run,) = store.values()
        self.assertNotIn(self.ABANDONED, json.dumps(run.messages, ensure_ascii=False))
        #  父会话自己的历史不受影响：委托调用与它的结果都在
        self.assertTrue(any(m.get("tool_calls") for m in agent.messages))

    def test_answered_siblings_stay_and_pending_calls_go(self) -> None:
        history = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "干活"},
            {"role": "assistant", "content": "", "tool_calls": [
                self.call("c1", "read_file"), self.call("c2"), self.call("c3", "grep"),
            ]},
            {"role": "tool", "tool_call_id": "c1", "content": "文件内容"},
        ]
        seed = agents_mod.fork_seed(history)
        self.assertEqual([m["role"] for m in seed], ["user", "assistant", "tool"])
        self.assertEqual([c["id"] for c in seed[1]["tool_calls"]], ["c1"])
        #  动的是副本
        self.assertEqual(len(history[2]["tool_calls"]), 3)

    def test_assistant_with_text_keeps_the_text(self) -> None:
        history = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "干活"},
            {"role": "assistant", "content": "我交给 worker", "tool_calls": [self.call("c1")]},
        ]
        seed = agents_mod.fork_seed(history)
        self.assertEqual(seed[-1], {"role": "assistant", "content": "我交给 worker"})

    def test_earlier_settled_turns_are_untouched(self) -> None:
        history = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "干活"},
            {"role": "assistant", "content": "", "tool_calls": [self.call("c1", "read_file")]},
            {"role": "tool", "tool_call_id": "c1", "content": "文件内容"},
            {"role": "assistant", "content": "读完了"},
        ]
        self.assertEqual(agents_mod.fork_seed(history), history[1:])


def tool_call(call_id: str, name: str = "read_file") -> dict:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


def shape(messages: list[dict]) -> list[str]:
    """消息序列的骨架：role，tool 带上它应答的调用 id。"""
    return [
        f"tool:{m.get('tool_call_id')}" if m.get("role") == "tool" else str(m.get("role"))
        for m in messages
    ]


class FillerPlacementTest(AgentTestCase):
    """补位的工具结果：内存与会话日志里都紧跟它的调用，resume 后序列合法。"""

    def test_dangling_call_is_closed_before_the_next_user_message(self) -> None:
        from xiaoyu.session_log import SessionLog, load_messages

        log_path = self.root / "log.jsonl"
        log = SessionLog(log_path)
        self.addCleanup(log.release)

        def approver(name, args):
            raise RuntimeError("审批通道断了")

        self.config.auto_approve = False
        agent = self.build(
            [
                [chunk(tool_calls=[call_fragment(0, "c1", "bash", '{"command": "echo hi"}')])],
                [chunk(content="好的")],
            ],
            session_log=log, approver=approver,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            #  宿主的通用异常分支：不做补位，直接等用户的下一条
            with self.assertRaises(RuntimeError):
                agent.send("跑一下")
            agent.send("怎么了")
        expected = ["user", "assistant", "tool:c1", "user", "assistant"]
        self.assertEqual(shape(agent.messages[1:]), expected)
        #  日志里的顺序与内存一致——resume 重放出来的就是合法序列
        self.assertEqual(shape(load_messages(log_path)), expected)

    def restored(self, messages: list[dict]):
        agent = self.build([])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.restore(messages, copy=False)
            agent._repair_history()
        return agent.messages[1:]

    def test_log_written_out_of_order_heals_on_resume(self) -> None:
        healed = self.restored([
            {"role": "user", "content": "跑一下"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c1")]},
            {"role": "user", "content": "怎么了"},
            {"role": "tool", "tool_call_id": "c1", "content": "[按已放弃处理]"},
            {"role": "assistant", "content": "好的"},
        ])
        self.assertEqual(shape(healed), ["user", "assistant", "tool:c1", "user", "assistant"])

    def test_result_far_from_its_call_is_dropped(self) -> None:
        healed = self.restored([
            {"role": "user", "content": "跑一下"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "真结果"},
            {"role": "assistant", "content": "好的"},
            {"role": "user", "content": "再来"},
            {"role": "tool", "tool_call_id": "c1", "content": "游离的一条"},
            {"role": "tool", "tool_call_id": "zz", "content": "没有主的一条"},
        ])
        self.assertEqual(shape(healed), ["user", "assistant", "tool:c1", "assistant", "user"])
        self.assertEqual(healed[2]["content"], "真结果")

    def test_duplicate_result_for_one_call_keeps_the_first(self) -> None:
        healed = self.restored([
            {"role": "user", "content": "跑一下"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "第一条"},
            {"role": "tool", "tool_call_id": "c1", "content": "第二条"},
        ])
        self.assertEqual(shape(healed), ["user", "assistant", "tool:c1"])
        self.assertEqual(healed[2]["content"], "第一条")

    def test_real_result_behind_an_interleaved_note_is_moved_back_not_replaced(self) -> None:
        #  一批工具跑到一半时插进来的说明（切档的交代）：第二个调用的真结果排在它后面
        healed = self.restored([
            {"role": "user", "content": "跑一下"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c1"), tool_call("c2")]},
            {"role": "tool", "tool_call_id": "c1", "content": "一"},
            {"role": "user", "content": "[已进入 plan 档]", "_operator": True},
            {"role": "tool", "tool_call_id": "c2", "content": "二"},
        ])
        self.assertEqual(shape(healed), ["user", "assistant", "tool:c1", "tool:c2", "user"])
        self.assertEqual(healed[3]["content"], "二")

    def test_legitimate_history_is_left_alone(self) -> None:
        #  每轮都从同一个 id 起编的模型、不给 id 的服务端：位置都对，一条不能少
        history = [
            {"role": "user", "content": "跑一下"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "一"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "二"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call(""), tool_call("")]},
            {"role": "tool", "tool_call_id": "", "content": "三"},
            {"role": "tool", "tool_call_id": "", "content": "四"},
            {"role": "assistant", "content": "完"},
        ]
        healed = self.restored([dict(m) for m in history])
        self.assertEqual(healed, history)

    def test_reused_id_in_a_later_turn_is_not_stolen(self) -> None:
        #  前一轮的调用悬空，后一轮用了同一个 id：后一轮的结果不是前一轮的
        healed = self.restored([
            {"role": "user", "content": "跑一下"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c1")]},
            {"role": "user", "content": "再来"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c1")]},
            {"role": "tool", "tool_call_id": "c1", "content": "后一轮的结果"},
        ])
        self.assertEqual(
            shape(healed), ["user", "assistant", "tool:c1", "user", "assistant", "tool:c1"]
        )
        self.assertNotEqual(healed[2]["content"], "后一轮的结果")
        self.assertEqual(healed[5]["content"], "后一轮的结果")


class ResumedBackgroundTasksTest(AgentTestCase):
    """接回来的历史里提到的后台任务：编号不撞，且交代一句它们已经不在了。"""

    STARTED = (
        "后台任务已启动：task-3\n日志：/tmp/x/task-3.log\n"
        '完成时会自动通知你；中途要看输出用 task_output(task_ids=["task-3"])。'
    )

    def history(self, *extra: dict) -> list[dict]:
        return [
            {"role": "user", "content": "跑个构建"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c1", "bash")]},
            {"role": "tool", "tool_call_id": "c1", "content": self.STARTED},
            {"role": "assistant", "content": "已在后台启动，完成后通知"},
            *extra,
        ]

    def restored(self, messages: list[dict]):
        agent = self.build([])
        self.addCleanup(agent.toolbox.tasks.shutdown)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.restore(messages, copy=False)
        return agent

    def notes(self, agent) -> list[dict]:
        return [
            m for m in agent.messages
            if str(m.get("content")).startswith(agent.TASK_RECONCILE_MARK)
        ]

    def test_mentioned_tasks_are_declared_gone_once(self) -> None:
        agent = self.restored(self.history())
        (note,) = self.notes(agent)
        self.assertIs(agent.messages[-1], note)
        self.assertTrue(note.get("_operator"))
        self.assertIn("task-3", note["content"])
        self.assertIn("not_found", note["content"])
        #  再接回一次（历史里已有这条说明）：不重复交代
        again = self.restored([dict(m) for m in agent.messages[1:]])
        self.assertEqual(len(self.notes(again)), 1)

    @unittest.skipUnless(POSIX, "用例依赖 POSIX shell")
    def test_new_tasks_are_numbered_after_the_ones_in_history(self) -> None:
        agent = self.restored(self.history())
        output = agent.toolbox.run("bash", {"command": "true", "run_in_background": True})
        self.assertIn("后台任务已启动：task-4", output)
        #  旧 id 查不到，与说明里的说法一致
        self.assertIn("task-3：not_found", agent.toolbox.run("task_output", {"task_ids": ["task-3"]}))

    def test_tasks_started_after_the_last_note_get_their_own_note(self) -> None:
        first = self.restored(self.history())
        later = [
            *[dict(m) for m in first.messages[1:]],
            {"role": "user", "content": "[monitor \"task-5\" 已结束（exit 0）。]", "_injected": True},
        ]
        second = self.restored(later)
        notes = self.notes(second)
        self.assertEqual(len(notes), 2)
        self.assertIn("task-5", notes[-1]["content"])
        self.assertNotIn("task-3", notes[-1]["content"])

    def test_session_that_never_used_background_tasks_gets_nothing(self) -> None:
        history = [
            {"role": "user", "content": "看看 task-12 这张工单"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c1", "grep")]},
            {"role": "tool", "tool_call_id": "c1", "content": "docs/plan.md:3: task-12 待排期"},
            {"role": "assistant", "content": "找到了"},
        ]
        agent = self.restored([dict(m) for m in history])
        self.assertEqual(agent.messages[1:], history)
        self.assertEqual(agent.toolbox.tasks._counter, 0)

    def test_note_lands_after_the_crash_filler(self) -> None:
        agent = self.restored([
            *self.history(),
            {"role": "user", "content": "再跑一个"},
            {"role": "assistant", "content": "", "tool_calls": [tool_call("c2", "bash")]},
        ])
        self.assertEqual(shape(agent.messages[-3:]), ["assistant", "tool:c2", "user"])
        self.assertEqual(len(self.notes(agent)), 1)


if __name__ == "__main__":
    unittest.main()
