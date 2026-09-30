"""操作进行到一半失败时，不丢东西、不说假话。

每一组对应一条"半路出事"的路径：后台任务超时、一次性模式退出、委托被打断、
fork 继承、补位的工具结果、resume 后的后台任务、前台命令被叫停。
真实子进程一律有超时，且不产生无界输出。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest

from xiaoyu import background as bg

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


if __name__ == "__main__":
    unittest.main()
