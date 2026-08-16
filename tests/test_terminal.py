"""终端能力探测的测试。

真终端拿不到，用 pty 造一个：这不是为了好看——写这些测试时正是 pty 用例
逮到 `tty.setraw` 默认的 TCSAFLUSH 会在 macOS 上**永久挂死**（启动时卡住，
比探测不到背景色严重得多），纯解析层的单测永远发现不了这个。
"""

from __future__ import annotations

import os
import sys
import unittest
import unittest.mock

from xiaoyu import terminal, theme

try:
    import pty
    import termios

    HAS_PTY = True
except ImportError:  # Windows
    HAS_PTY = False


class TestParsing(unittest.TestCase):
    def test_reads_both_terminators(self) -> None:
        """ST（ESC \\）和 BEL 两种收尾都得认，不同终端各用各的。"""
        self.assertEqual(terminal.parse_background(b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\"), 1.0)
        self.assertEqual(terminal.parse_background(b"\x1b]11;rgb:0000/0000/0000\x07"), 0.0)

    def test_handles_short_hex_components(self) -> None:
        """分量可以是 1~4 位十六进制，按位数归一化，不能当成固定 4 位。"""
        self.assertEqual(terminal.parse_background(b"\x1b]11;rgb:ff/ff/ff\x1b\\"), 1.0)
        self.assertEqual(terminal.parse_background(b"\x1b]11;rgb:f/f/f\x07"), 1.0)

    def test_blue_background_is_not_light(self) -> None:
        """按人眼亮度加权，不能简单取平均：纯蓝底会被平均法误判成浅色。"""
        blue = terminal.parse_background(b"\x1b]11;rgb:0000/0000/ffff\x1b\\")
        self.assertIsNotNone(blue)
        self.assertLess(blue, terminal._LIGHT_ABOVE)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(terminal.parse_background(b"not an answer"))
        self.assertIsNone(terminal.parse_background(b""))

    def test_not_a_tty_gives_up_quietly(self) -> None:
        #  单测环境的 stdin 不是终端，探测应直接放弃而不是报错
        self.assertIsNone(terminal.query_background())


@unittest.skipUnless(HAS_PTY, "本平台没有 pty")
class TestQueryOverPty(unittest.TestCase):
    """在真 pty 上跑完整流程：切 raw → 发查询 → 读回答 → 复原。"""

    class _Std:
        def __init__(self, fd: int) -> None:
            self._fd = fd

        def fileno(self) -> int:
            return self._fd

        def isatty(self) -> bool:
            return True

        def write(self, text: str) -> None:
            os.write(self._fd, text.encode())

        def flush(self) -> None:
            pass

    #  探测本身最多 150ms；给它 5 秒还没回来就是卡住了
    _DEADLINE = 5.0

    def run_query(self, answer: bytes):
        import threading

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        if answer:
            #  回答先躺进输入缓冲，省掉线程时序（真终端是异步答，效果一样）
            os.write(master, answer)
        before = termios.tcgetattr(slave)

        #  放线程里跑并设截止时间，兜住 select/read 卡住的情况（它们会释放 GIL）。
        #  注意兜不住 termios 系统调用本身卡死——那类调用不释放 GIL，整个解释器
        #  会冻结，看门狗线程根本得不到调度。模式切换参数由下面那条白盒用例锁死。
        box: list = []
        saved_in, saved_out = sys.stdin, sys.stdout
        sys.stdin = sys.stdout = self._Std(slave)
        worker = threading.Thread(target=lambda: box.append(terminal.query_background()))
        worker.daemon = True
        worker.start()
        worker.join(self._DEADLINE)
        sys.stdin, sys.stdout = saved_in, saved_out
        if worker.is_alive():
            self.fail(f"探测超过 {self._DEADLINE}s 没有返回——终端模式切换卡住了")
        return box[0], before, termios.tcgetattr(slave)

    def test_setraw_must_use_tcsanow(self) -> None:
        """锁死模式切换参数。tty.setraw 默认的 TCSAFLUSH 有两宗罪：
        丢弃待读输入（吃掉用户抢跑打的字），以及在 macOS pty 上永久挂死。

        为什么用白盒断言而不是"跑一遍看会不会卡"：termios 系统调用不释放 GIL，
        真卡住时整个解释器冻结，任何超时看门狗都不会被调度到——实测变异版本
        是被外部 timeout 杀掉的，测试框架连一行失败信息都来不及打。
        """
        import tty

        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        saved_in, saved_out = sys.stdin, sys.stdout
        sys.stdin = sys.stdout = self._Std(slave)
        try:
            with unittest.mock.patch.object(tty, "setraw") as setraw:
                terminal.query_background()
        finally:
            sys.stdin, sys.stdout = saved_in, saved_out
        setraw.assert_called_once()
        self.assertEqual(
            setraw.call_args.args[1:] or (setraw.call_args.kwargs.get("when"),),
            (termios.TCSANOW,),
            "必须显式传 TCSANOW，默认的 TCSAFLUSH 会丢输入并可能挂死",
        )

    def test_reads_the_answer(self) -> None:
        value, _before, _after = self.run_query(b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")
        self.assertEqual(value, 1.0)

    def test_silent_terminal_times_out_and_returns_none(self) -> None:
        value, _before, _after = self.run_query(b"")
        self.assertIsNone(value)

    def test_restores_terminal_modes(self) -> None:
        """探测完必须把终端交还原样，否则用户的 shell 就废在 raw 模式里了。"""
        _value, before, after = self.run_query(b"\x1b]11;rgb:1e1e/1e1e/1e1e\x07")
        names = ("iflag", "oflag", "cflag", "lflag", "ispeed", "ospeed", "cc")
        for name, old, new in zip(names, before, after):
            if name == "lflag":
                #  PENDIN 是内核维护的"有待重显输入"状态位，不是我们设的模式
                old, new = old & ~0x20000000, new & ~0x20000000
            self.assertEqual(old, new, f"{name} 没有复原")


class TestAutodetect(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(theme.set_mode, theme.mode())
        self.addCleanup(os.environ.pop, "XIAOYU_THEME", None)

    def test_explicit_theme_skips_detection(self) -> None:
        """用户显式指定了就别再问终端——他说了算。"""
        os.environ["XIAOYU_THEME"] = "light"
        with unittest.mock.patch.object(terminal, "detect_theme_mode") as detect:
            self.assertIsNone(terminal.autodetect())
        detect.assert_not_called()

    def test_detection_applies_the_mode(self) -> None:
        os.environ["XIAOYU_THEME"] = "auto"
        with unittest.mock.patch.object(terminal, "detect_theme_mode", return_value="light"):
            self.assertEqual(terminal.autodetect(), "light")
        self.assertEqual(theme.mode(), "light")

    def test_failed_detection_leaves_the_mode_alone(self) -> None:
        os.environ["XIAOYU_THEME"] = "auto"
        theme.set_mode("dark")
        with unittest.mock.patch.object(terminal, "detect_theme_mode", return_value=None):
            self.assertIsNone(terminal.autodetect())
        self.assertEqual(theme.mode(), "dark")


if __name__ == "__main__":
    unittest.main()
