"""xiaoyu._winpip：Windows 上等调用方退出后再跑 pip 的落地执行器。

锁住五件事：
1. 动 pip 之前必须先等调用方的 pid 与 ppid 都退掉——早一步就还锁着；
2. update / uninstall 各自拼对 pip 参数，且一律走 sys.executable -m pip；
3. pip 失败要重试（启动器 stub 比 python.exe 晚一步退），重试完仍败则返回非 0；
4. update 成功后用新解释器读版本号播报，uninstall 成功后报告卸载完成；
5. 参数个数/模式不对就返回 2，不去猜调用方想干什么。
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from xiaoyu import _winpip


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = _winpip.main(argv)
    return code, out.getvalue()


class WinPipTest(unittest.TestCase):
    def test_waits_for_both_pids_before_touching_pip(self):
        order = []

        def fake_wait(pid, seconds):
            order.append(("wait", pid))

        def fake_run(argv, **kwargs):
            order.append(("run", argv[3] if len(argv) > 3 else ""))
            return SimpleNamespace(returncode=0, stdout="9.9.9\n")

        with mock.patch.object(_winpip, "_wait_for_exit", side_effect=fake_wait), \
                mock.patch.object(_winpip.subprocess, "run", side_effect=fake_run):
            code, _ = _run(["111", "222", "update", "xiaoyu-agent", "0.30.1"])

        self.assertEqual(code, 0)
        #  两个 pid 都等完，才允许出现第一次 run
        self.assertEqual(order[:2], [("wait", 111), ("wait", 222)])
        self.assertEqual(order[2][0], "run")

    def test_parent_wait_is_capped_short(self):
        """ppid 可能是 cmd.exe（要等用户关窗口），不能给它长上限。"""
        waits = {}

        with mock.patch.object(
            _winpip, "_wait_for_exit", side_effect=lambda pid, s: waits.setdefault(pid, s)
        ), mock.patch.object(
            _winpip.subprocess, "run",
            return_value=SimpleNamespace(returncode=0, stdout="9.9.9\n"),
        ):
            _run(["111", "222", "update", "xiaoyu-agent", "0.30.1"])

        self.assertEqual(waits[111], _winpip._WAIT_SECONDS)
        self.assertEqual(waits[222], _winpip._PARENT_WAIT_SECONDS)
        self.assertLess(_winpip._PARENT_WAIT_SECONDS, _winpip._WAIT_SECONDS)

    def test_update_reports_new_version(self):
        def fake_run(argv, **kwargs):
            if "-c" in argv:
                return SimpleNamespace(returncode=0, stdout="0.31.0\n")
            return SimpleNamespace(returncode=0, stdout="")

        with mock.patch.object(_winpip, "_wait_for_exit"), \
                mock.patch.object(_winpip.subprocess, "run", side_effect=fake_run) as run:
            code, output = _run(["1", "2", "update", "xiaoyu-agent[tui]", "0.30.1"])

        self.assertEqual(code, 0)
        self.assertIn(
            [sys.executable, "-m", "pip", "install", "--upgrade", "xiaoyu-agent[tui]"],
            [call.args[0] for call in run.call_args_list],
        )
        self.assertIn("0.30.1 → 0.31.0", output)

    def test_uninstall_uses_yes_flag(self):
        with mock.patch.object(_winpip, "_wait_for_exit"), \
                mock.patch.object(
                    _winpip.subprocess, "run",
                    return_value=SimpleNamespace(returncode=0, stdout=""),
                ) as run:
            code, output = _run(["1", "2", "uninstall", "xiaoyu-agent", "0.30.1"])

        self.assertEqual(code, 0)
        self.assertEqual(
            run.call_args_list[0].args[0],
            [sys.executable, "-m", "pip", "uninstall", "-y", "xiaoyu-agent"],
        )
        self.assertIn("已卸载", output)

    def test_retries_then_gives_up(self):
        """stub 晚一步退是常态，值得重试；但不能无限重试挂在那儿。"""
        with mock.patch.object(_winpip, "_wait_for_exit"), \
                mock.patch.object(_winpip.time, "sleep") as sleep, \
                mock.patch.object(
                    _winpip.subprocess, "run",
                    return_value=SimpleNamespace(returncode=1, stdout=""),
                ) as run:
            code, output = _run(["1", "2", "update", "xiaoyu-agent", "0.30.1"])

        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, _winpip._ATTEMPTS)
        self.assertEqual(sleep.call_count, _winpip._ATTEMPTS - 1)  # 最后一次不再等
        self.assertIn("失败", output)

    def test_retry_succeeds_and_stops_early(self):
        attempts = []

        def fake_run(argv, **kwargs):
            if "-c" in argv:
                return SimpleNamespace(returncode=0, stdout="0.31.0\n")
            attempts.append(argv)
            return SimpleNamespace(returncode=1 if len(attempts) == 1 else 0, stdout="")

        with mock.patch.object(_winpip, "_wait_for_exit"), \
                mock.patch.object(_winpip.time, "sleep"), \
                mock.patch.object(_winpip.subprocess, "run", side_effect=fake_run):
            code, output = _run(["1", "2", "update", "xiaoyu-agent", "0.30.1"])

        self.assertEqual(code, 0)
        self.assertEqual(len(attempts), 2)  # 第二次就成了，不再往下重试
        self.assertIn("已升级", output)

    def test_always_tells_the_user_to_press_enter(self):
        """cmd 的提示符早打完了、也不会重画——成败都得补这句，否则用户干等。"""
        for returncode in (0, 1):
            with self.subTest(returncode=returncode):
                with mock.patch.object(_winpip, "_wait_for_exit"), \
                        mock.patch.object(_winpip.time, "sleep"), \
                        mock.patch.object(
                            _winpip.subprocess, "run",
                            return_value=SimpleNamespace(returncode=returncode, stdout=""),
                        ):
                    code, output = _run(["1", "2", "update", "xiaoyu-agent", "0.30.1"])
                self.assertEqual(code, returncode)
                self.assertIn("按一次 Enter 回到命令提示符", output)

    def test_bad_arguments_rejected(self):
        with mock.patch.object(_winpip, "_wait_for_exit"), \
                mock.patch.object(_winpip.subprocess, "run") as run:
            self.assertEqual(_run(["1", "2", "update"])[0], 2)
            self.assertEqual(_run(["1", "2", "reinstall", "x", "0.1"])[0], 2)
        run.assert_not_called()

    def test_wait_is_a_noop_off_windows(self):
        """非 Windows 不该去 import ctypes、更不该阻塞——这条在 CI 上真跑。"""
        with mock.patch.object(_winpip.os, "name", "posix"):
            _winpip._wait_for_exit(1, 999.0)  # 不抛、不卡住即通过


if __name__ == "__main__":
    unittest.main()
