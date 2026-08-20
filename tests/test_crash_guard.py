"""崩溃面包屑测试。不打网络。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import crash_guard


class CrashGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.log = Path(self._tmp.name) / "crash.log"
        #  每个用例独立安装：重置模块状态，收尾还原 excepthook
        crash_guard._installed = False
        crash_guard._log_path = None
        self._orig_hook = sys.excepthook
        self.addCleanup(self._restore)
        self.addCleanup(self._tmp.cleanup)

    def _restore(self) -> None:
        sys.excepthook = self._orig_hook
        crash_guard._installed = False
        crash_guard._log_path = None

    def test_install_is_idempotent(self) -> None:
        crash_guard.install(self.log)
        hook = sys.excepthook
        crash_guard.install(self.log)  # 第二次不应再换 hook
        self.assertIs(sys.excepthook, hook)

    def test_uncaught_exception_written_and_chained(self) -> None:
        chained = []
        with mock.patch.object(sys, "excepthook", lambda *a: chained.append(a)):
            crash_guard.install(self.log)
            try:
                raise ValueError("boom-xyz")
            except ValueError as exc:
                sys.excepthook(type(exc), exc, exc.__traceback__)
        #  写盘：记录里有异常信息
        self.assertTrue(self.log.exists())
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("boom-xyz", text)
        self.assertIn("未捕获异常", text)
        #  链回原 hook：终端仍会打 traceback
        self.assertEqual(len(chained), 1)

    def test_keyboard_interrupt_not_recorded(self) -> None:
        with mock.patch.object(sys, "excepthook", lambda *a: None):
            crash_guard.install(self.log)
            exc = KeyboardInterrupt()
            sys.excepthook(KeyboardInterrupt, exc, None)
        #  Ctrl-C 是正常中断，不该留崩溃记录（文件可不存在或不含该条）
        if self.log.exists():
            self.assertNotIn("未捕获异常", self.log.read_text(encoding="utf-8"))

    def test_write_never_raises_on_bad_path(self) -> None:
        #  日志路径不可写时 _write 必须静默——崩溃日志不能让退出再崩一次
        crash_guard.install(Path("/proc/nonexistent/crash.log"))
        try:
            raise RuntimeError("x")
        except RuntimeError as exc:
            #  不抛即通过
            crash_guard._write("测试", exc)

    def test_log_truncated_to_tail_when_oversized(self) -> None:
        crash_guard.install(self.log)
        with mock.patch.object(crash_guard, "_MAX_BYTES", 500):
            for i in range(50):
                try:
                    raise ValueError(f"err-{i}")
                except ValueError as exc:
                    crash_guard._write("批量", exc)
        text = self.log.read_text(encoding="utf-8")
        self.assertLessEqual(len(text), 500 + 100)  # 截断标记的余量
        self.assertIn("截断", text)
        #  保留的是尾部（最近的崩溃）
        self.assertIn("err-49", text)
        self.assertNotIn("err-0\n", text)


if __name__ == "__main__":
    unittest.main()
