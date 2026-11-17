"""会话空闲时，后台事件对宿主可见。

后台任务是在别的线程里完成的，那时候会话多半已经空闲。完成通知是给模型看的，
要等下一轮才送得到——宿主不知道"有事件在等"，就只能干等，或者定时发消息来问
（每问一次都是一次模型调用）。

这里锁的是三层的约定：内核在通知入队时告知宿主；serve 把它变成事件流里的一条
与 /status 里的一项；嵌入宿主的回调落在它自己的事件循环里。三层都**只告知、
不开新的一轮**。
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import threading
import time
import unittest
from typing import Any

from tests.test_serve import HAS_FASTAPI, ServeCase
from xiaoyu.embedding import AsyncAgent

from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk

DONE = 'usage: {"prompt_tokens": 10, "completion_tokens": 5}\ntext: 好了\n'


def wait_until(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class KernelTest(AgentTestCase):
    def watched(self, script: list | None = None):
        agent = self.build(script or [])
        seen: list[dict[str, Any]] = []
        agent.on_notification = seen.append
        return agent, seen

    def test_host_is_told_when_a_notification_is_queued(self) -> None:
        agent, seen = self.watched()
        agent.notify("  后台任务 bg-1 已完成  ", key=" task-done-bg-1 ")
        agent.notify("MCP server 上线了", wake=False)
        self.assertEqual(seen, [
            {"key": "task-done-bg-1", "text": "后台任务 bg-1 已完成", "wake": True},
            {"key": "", "text": "MCP server 上线了", "wake": False},
        ])
        self.assertEqual(agent.pending_notifications(), seen)

    def test_same_key_is_announced_once(self) -> None:
        agent, seen = self.watched()
        for _ in range(3):
            agent.notify("后台任务 bg-1 已完成", key="task-done-bg-1")
        self.assertEqual(len(seen), 1)
        self.assertEqual(len(agent.pending_notifications()), 1)

    def test_blank_text_is_not_a_notification(self) -> None:
        agent, seen = self.watched()
        agent.notify("   ", key="k")
        self.assertEqual((seen, agent.pending_notifications()), ([], []))

    def test_without_a_host_callback_nothing_changes(self) -> None:
        agent = self.build([])
        agent.notify("后台任务 bg-1 已完成", key="task-done-bg-1")
        self.assertEqual(len(agent.pending_notifications()), 1)

    def test_broken_host_callback_does_not_lose_the_notification(self) -> None:
        agent = self.build([])

        def broken(item: dict[str, Any]) -> None:
            raise RuntimeError("宿主的回调坏了")

        agent.on_notification = broken
        agent.notify("后台任务 bg-1 已完成", key="task-done-bg-1")  # 不许抛到投递方头上
        self.assertEqual(len(agent.pending_notifications()), 1)

    def test_being_told_does_not_start_a_turn(self) -> None:
        agent, seen = self.watched([])
        before = list(agent.messages)
        agent.notify("后台任务 bg-1 已完成", key="task-done-bg-1")
        self.assertEqual(len(seen), 1)
        self.assertEqual(agent.messages, before)  # 历史没动
        self.assertEqual(self.client.completions.calls, [])  # 模型没被调用

    def test_pending_is_empty_once_the_model_has_been_told(self) -> None:
        agent, seen = self.watched([
            [chunk(tool_calls=[call_fragment(0, "c1", "read_file", '{"path": "calc.py"}')])],
            [chunk(content="看过了"), usage_chunk(10, 2)],
        ])
        agent.notify("后台任务 bg-1 已完成", key="task-done-bg-1")
        self.assertEqual(len(agent.pending_notifications()), 1)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("看一下 calc.py")
        self.assertEqual(agent.pending_notifications(), [])
        self.assertTrue(any("bg-1 已完成" in str(m.get("content")) for m in agent.messages))
        #  送达之后同一个 key 再投递：不重复送达，也不重复告知
        agent.notify("后台任务 bg-1 已完成", key="task-done-bg-1")
        self.assertEqual(len(seen), 1)
        self.assertEqual(agent.pending_notifications(), [])

    def test_callable_from_any_thread(self) -> None:
        agent, seen = self.watched()
        threads = [
            threading.Thread(target=agent.notify, args=(f"事件 {n}", f"k{n}")) for n in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(sorted(item["key"] for item in seen), sorted(f"k{n}" for n in range(20)))
        self.assertEqual(len(agent.pending_notifications()), 20)

    @unittest.skipIf(os.name == "nt", "用例依赖 POSIX shell")
    def test_real_background_task_finishing_while_idle(self) -> None:
        self.config.sandbox = False
        agent, seen = self.watched()
        out = agent.toolbox.run("bash", {"command": "echo 跑完了", "run_in_background": True})
        self.assertNotIn("ERROR", out)
        #  这时没有任何一轮在跑：完成通知只能靠回调让宿主知道
        self.assertTrue(wait_until(lambda: bool(seen)), "后台任务完成了，宿主却没被告知")
        (item,) = seen
        self.assertTrue(item["key"].startswith("task-done-"))
        self.assertTrue(item["wake"])
        self.assertIn("已完成", item["text"])
        self.assertEqual(agent.pending_notifications(), [item])
        self.assertEqual(self.client.completions.calls, [])


class EmbeddingTest(AgentTestCase, unittest.IsolatedAsyncioTestCase):
    async def test_callback_runs_on_the_hosts_event_loop(self) -> None:
        agent = self.build([])
        host = AsyncAgent(agent)
        loop_thread = threading.get_ident()
        seen: list[tuple[int, dict[str, Any]]] = []
        arrived = asyncio.Event()

        def on_notification(item: dict[str, Any]) -> None:
            seen.append((threading.get_ident(), item))
            arrived.set()

        host.watch_notifications(on_notification)
        #  后台任务的 watcher 线程就是这样投递的：不在事件循环线程上
        await asyncio.to_thread(agent.notify, "后台任务 bg-1 已完成", "task-done-bg-1")
        await asyncio.wait_for(arrived.wait(), 5)
        ((thread, item),) = seen
        self.assertEqual(thread, loop_thread, "回调该落在宿主的事件循环里")
        self.assertEqual(item["key"], "task-done-bg-1")
        self.assertEqual(host.pending_notifications(), [item])

    async def test_unwatch(self) -> None:
        agent = self.build([])
        host = AsyncAgent(agent)
        seen: list[dict[str, Any]] = []
        host.watch_notifications(seen.append)
        host.watch_notifications(None)
        await asyncio.to_thread(agent.notify, "后台任务 bg-1 已完成", "task-done-bg-1")
        await asyncio.sleep(0.05)
        self.assertEqual(seen, [])
        self.assertEqual(len(host.pending_notifications()), 1)  # 通知本身照常在队里


@unittest.skipUnless(HAS_FASTAPI, "需要可选额外 [serve]（fastapi + uvicorn）")
class ServeTest(ServeCase):
    def agent_of(self, session_id: str):
        return self.client.app.state.sessions[session_id].agent

    def notifications(self, session_id: str) -> list[dict[str, Any]]:
        return [e for e in self.events(session_id, limit=2000) if e["kind"] == "notification.pending"]

    def notify_from_another_thread(self, session_id: str, text: str, key: str, **kwargs) -> None:
        worker = threading.Thread(target=self.agent_of(session_id).notify, args=(text, key), kwargs=kwargs)
        worker.start()
        worker.join(5)

    def test_idle_session_reports_the_event_and_lists_it_in_status(self) -> None:
        self.start(DONE)
        session_id = self.new_session()
        self.assertEqual(self.status(session_id)["pending_notifications"], [])
        self.notify_from_another_thread(session_id, "后台任务 bg-1 已完成", "task-done-bg-1")
        self.assertTrue(wait_until(lambda: bool(self.notifications(session_id))))
        (event,) = self.notifications(session_id)
        self.assertEqual(
            {k: event[k] for k in ("key", "text", "wake", "idle")},
            {"key": "task-done-bg-1", "text": "后台任务 bg-1 已完成", "wake": True, "idle": True},
        )
        state = self.status(session_id)
        self.assertEqual(
            state["pending_notifications"],
            [{"key": "task-done-bg-1", "text": "后台任务 bg-1 已完成", "wake": True}],
        )
        #  只是告知：会话还是空闲的，没有自己开一轮
        self.assertEqual((state["status"], state["busy"], state["turns"]), ("idle", False, 0))
        self.assertNotIn("run.started", self.kinds(session_id))

    def test_next_turn_delivers_it_and_the_list_empties(self) -> None:
        script = (
            'tool_call: {"name": "read_file", "arguments": {"path": "README.md"}}\n---\n' + DONE
        )
        self.start(script)
        (self.root / "README.md").write_text("说明", encoding="utf-8")
        session_id = self.new_session()
        self.notify_from_another_thread(session_id, "后台任务 bg-1 已完成", "task-done-bg-1")
        self.assertTrue(wait_until(lambda: bool(self.status(session_id)["pending_notifications"])))
        done = self.client.post(
            f"/session/{session_id}/prompt", json={"text": "接着干"}, headers=self.headers()
        ).json()
        self.assertEqual(done["detail"], "finished")
        self.assertEqual(done["pending_notifications"], [])
        history = self.agent_of(session_id).messages
        self.assertTrue(any("bg-1 已完成" in str(m.get("content")) for m in history))

    def test_event_says_whether_the_session_was_busy(self) -> None:
        script = (
            'tool_call: {"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}\n---\n'
            + DONE
        )
        self.start(script)
        session_id = self.new_session()
        self.client.post(
            f"/session/{session_id}/prompt_async", json={"text": "干活"}, headers=self.headers()
        )
        waiting = self._wait_for(session_id, "waiting_for_approval")  # 这一轮确定还在跑
        self.notify_from_another_thread(session_id, "monitor 有新输出", "mon-1")
        self.assertTrue(wait_until(lambda: bool(self.notifications(session_id))))
        (event,) = self.notifications(session_id)
        self.assertFalse(event["idle"])  # 在跑：搭下一条工具结果就送到了，编排方不必做什么
        self.client.post(
            f"/session/{session_id}/permissions",
            json={"request_id": waiting["pending_approvals"][0]["request_id"], "decision": "allow"},
            headers=self.headers(),
        )
        self._wait_for(session_id, "finished")
        self.assertEqual(self.status(session_id)["pending_notifications"], [])

    def test_long_text_is_clipped_in_status(self) -> None:
        self.start(DONE, max_field=40)
        session_id = self.new_session()
        self.notify_from_another_thread(session_id, "很长的输出" * 100, "mon-1")
        self.assertTrue(wait_until(lambda: bool(self.status(session_id)["pending_notifications"])))
        (item,) = self.status(session_id)["pending_notifications"]
        self.assertLess(len(item["text"]), 200)
        (event,) = self.notifications(session_id)
        self.assertTrue(event.get("text_truncated"))

    def test_sessions_do_not_see_each_others_notifications(self) -> None:
        self.start(DONE)
        first, second = self.new_session(), self.new_session()
        self.notify_from_another_thread(first, "后台任务 bg-1 已完成", "task-done-bg-1")
        self.assertTrue(wait_until(lambda: bool(self.notifications(first))))
        self.assertEqual(self.notifications(second), [])
        self.assertEqual(self.status(second)["pending_notifications"], [])


if __name__ == "__main__":
    unittest.main()
