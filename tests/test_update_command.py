"""xiaoyu update 自升级命令的测试。

锁住这些事：
1. pip 不存在（pipx/uv tool 环境）时不硬跑，给出替代命令并返回 1；
2. 未装 TUI 可选依赖 → 升级 spec 自动带上 [tui]；已装 → 只升本体；
   serve 方向相反：装了 fastapi+uvicorn 才带 [serve]（跟上新 pin），没装不塞；
3. spec 必须作为独立 argv 传给 sys.executable -m pip（不经 shell，无引号问题）；
4. pip 失败向上返回 1，成功后用新解释器读版本号播报；
5. 从 Windows 启动器 exe 跑起来时，pip **不在本进程里执行**——它锁着自己，
   pip 卸旧版必撞 WinError 32（改名挪开也不行，真机实测过），整段交给脱离的
   子进程；别的情形一律照旧在本进程里跑。
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from xiaoyu import cli


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = cli.update_command(argv)
    return code, out.getvalue()


@contextlib.contextmanager
def _extras(tui: bool = True, serve: bool = False):
    """把两个可选依赖探测都钉住——测试环境里装没装 fastapi 不该影响断言。"""
    with mock.patch.object(cli, "_tui_available", return_value=tui), \
            mock.patch.object(cli, "_serve_available", return_value=serve):
        yield


@contextlib.contextmanager
def _fake_windows_launcher(argv0: str | None = None):
    """伪造"本次是从 Scripts\\xiaoyu.exe 跑起来的 Windows 环境"。

    默认按**真实形态**给 argv[0]：不带 .exe。pip 塞进 exe 的 __main__.py 会先
    `re.sub(r'(-script\\.pyw|\\.exe)?$', '', sys.argv[0])`，我们看到的就是
    `...\\Scripts\\xiaoyu`。v0.30.1/v0.30.2 判据要求 argv[0] 以 .exe 结尾，
    因此在真机上永远为假、整套处理从没执行过——这个默认值就是那次的回归防线。

    patch os.name 期间一律用字符串路径：Path() 会按 os.name 分派成 WindowsPath，
    在 POSIX 上一构造就抛 UnsupportedOperation（被测代码同理，见 cli 里的注释）。
    """
    with tempfile.TemporaryDirectory() as tmp:
        launcher = os.path.join(os.path.realpath(tmp), "xiaoyu.exe")
        with open(launcher, "wb") as handle:
            handle.write(b"launcher")
        with mock.patch.object(cli.os, "name", "nt"), \
                mock.patch.object(
                    cli.sys, "argv", [argv0 or launcher[:-4], "update"]
                ):
            yield launcher


def _probe_ok(argv, **kwargs):
    """子进程入口探针（`-m xiaoyu._winpip` 无参数）固定返回 2。"""
    if argv[-1] == "xiaoyu._winpip":
        return SimpleNamespace(returncode=2, stdout="")
    return SimpleNamespace(returncode=0, stdout="9.9.9\n")


class UpdateCommandTest(unittest.TestCase):
    def test_no_pip_hints_pipx_and_uv(self):
        with mock.patch.object(
            cli.subprocess, "run", return_value=SimpleNamespace(returncode=1)
        ) as run:
            code, output = _run([])
        self.assertEqual(code, 1)
        self.assertEqual(run.call_count, 1)  # 探测失败后不再碰 pip
        self.assertIn("pipx upgrade xiaoyu-agent", output)
        self.assertIn("uv tool upgrade xiaoyu-agent", output)

    def test_installs_tui_extra_when_missing(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="9.9.9\n")

        with mock.patch.object(cli.subprocess, "run", side_effect=fake_run), \
                _extras(tui=False):
            code, output = _run([])
        self.assertEqual(code, 0)
        #  spec 是独立 argv 元素、带 [tui]、由本解释器的 pip 执行
        self.assertIn(
            [sys.executable, "-m", "pip", "install", "--upgrade", "xiaoyu-agent[tui]"],
            calls,
        )
        self.assertIn("一并安装", output)

    def test_plain_upgrade_when_tui_present(self):
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="9.9.9\n")

        with mock.patch.object(cli.subprocess, "run", side_effect=fake_run), \
                _extras():
            code, output = _run([])
        self.assertEqual(code, 0)
        self.assertIn(
            [sys.executable, "-m", "pip", "install", "--upgrade", "xiaoyu-agent"],
            calls,
        )
        self.assertIn("9.9.9", output)  # 新版本号来自新解释器的读数

    def test_serve_extra_follows_when_installed(self):
        """已装 fastapi+uvicorn 的环境升级要带 [serve]——不带的话锁版本升了
        本体也跟不上（0.31.6 动过 serve 的 pin，老用户会无声留在旧版）。"""
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return SimpleNamespace(returncode=0, stdout="9.9.9\n")

        with mock.patch.object(cli.subprocess, "run", side_effect=fake_run), \
                _extras(serve=True):
            code, output = _run([])
        self.assertEqual(code, 0)
        self.assertIn(
            [sys.executable, "-m", "pip", "install", "--upgrade", "xiaoyu-agent[serve]"],
            calls,
        )
        self.assertIn("serve", output)

    def test_tui_and_serve_extras_combine(self):
        def fake_run(argv, **kwargs):
            return SimpleNamespace(returncode=0, stdout="9.9.9\n")

        with mock.patch.object(
            cli.subprocess, "run", side_effect=fake_run
        ) as run, _extras(tui=False, serve=True):
            code, _ = _run([])
        self.assertEqual(code, 0)
        specs = [c.args[0][-1] for c in run.call_args_list if "install" in c.args[0]]
        self.assertEqual(specs, ["xiaoyu-agent[tui,serve]"])

    def test_pip_failure_returns_one(self):
        def fake_run(argv, **kwargs):
            returncode = 1 if "install" in argv else 0
            return SimpleNamespace(returncode=returncode, stdout="")

        with mock.patch.object(cli.subprocess, "run", side_effect=fake_run), \
                _extras():
            code, output = _run([])
        self.assertEqual(code, 1)
        self.assertIn("升级失败", output)

    def test_pip_deferred_to_detached_child_from_launcher(self):
        """从 xiaoyu.exe 跑起来时，pip 一次都不能在本进程里执行。"""
        with _fake_windows_launcher():
            with mock.patch.object(
                cli.subprocess, "run", side_effect=_probe_ok
            ) as run, mock.patch.object(cli.subprocess, "Popen") as popen, \
                    _extras():
                code, output = _run([])

        self.assertEqual(code, 0)
        #  本进程只允许探 pip 在不在、子进程起不起得来，绝不能真去 install
        self.assertTrue(all("install" not in call.args[0] for call in run.call_args_list))
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:4], [sys.executable, "-P", "-m", "xiaoyu._winpip"])
        #  pid/ppid 都要带上：python.exe 之上还有启动器 stub
        self.assertEqual(argv[4:], [
            str(os.getpid()), str(os.getppid()), "update", "xiaoyu-agent", cli.__version__,
        ])
        self.assertIn("本进程退出后继续", output)

    def test_launcher_detected_although_argv0_lost_its_suffix(self):
        """真机上 argv[0] 是 `...\\Scripts\\xiaoyu`——判据不能要求 .exe 结尾。

        v0.30.1/v0.30.2 的线上失效就是这一条：distlib 的 SCRIPT_TEMPLATE 在进
        main() 之前把后缀 re.sub 掉了，两版的处理因此从没被执行过。
        """
        with _fake_windows_launcher() as launcher:
            self.assertFalse(cli.sys.argv[0].endswith(".exe"))  # 先确认伪造的是真形态
            self.assertEqual(cli._running_launcher(), launcher)

    def test_launcher_detected_when_argv0_keeps_the_suffix(self):
        """万一哪天 argv[0] 又带上 .exe，也得照样认出来。"""
        with _fake_windows_launcher(argv0=None) as launcher:
            with mock.patch.object(cli.sys, "argv", [launcher, "update"]):
                self.assertEqual(cli._running_launcher(), launcher)

    def test_detached_child_keeps_the_console(self):
        """加了 DETACHED_PROCESS 用户就看不见 pip 输出了——只许要新进程组。"""
        with _fake_windows_launcher():
            with mock.patch.object(cli.subprocess, "run", side_effect=_probe_ok), \
                    mock.patch.object(cli.subprocess, "Popen") as popen, \
                    _extras():
                _run([])
        flags = popen.call_args.kwargs["creationflags"]
        self.assertEqual(flags, getattr(cli.subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        self.assertNotIn("stdout", popen.call_args.kwargs)  # 继承控制台

    def test_unusable_child_entry_point_keeps_pip_inline(self):
        """子进程入口跑不起来就别交出去——不然用户等到的是"什么都没发生"。"""
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if argv[-1] == "xiaoyu._winpip":
                return SimpleNamespace(returncode=1, stdout="")  # 探针不通
            return SimpleNamespace(returncode=0, stdout="9.9.9\n")

        with _fake_windows_launcher():
            with mock.patch.object(cli.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(cli.subprocess, "Popen") as popen, \
                    _extras():
                code, _ = _run([])
        popen.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn(
            [sys.executable, "-m", "pip", "install", "--upgrade", "xiaoyu-agent"], calls
        )

    def test_spawn_failure_falls_back_to_inline_pip(self):
        """子进程拉不起来也不能罢工：退回本进程里硬跑（不比以前差）。"""
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            return _probe_ok(argv)

        with _fake_windows_launcher():
            with mock.patch.object(cli.subprocess, "run", side_effect=fake_run), \
                    mock.patch.object(
                        cli.subprocess, "Popen", side_effect=OSError("nope")
                    ), _extras():
                code, output = _run([])
        self.assertEqual(code, 0)
        self.assertIn(
            [sys.executable, "-m", "pip", "install", "--upgrade", "xiaoyu-agent"], calls
        )
        self.assertIn("改在本进程里执行", output)

    def test_no_deferring_off_windows(self):
        """别的平台随便覆盖正在运行的文件，不该平白多起一个进程。"""
        with tempfile.TemporaryDirectory() as tmp:
            launcher = os.path.join(tmp, "xiaoyu.exe")
            with open(launcher, "wb") as handle:
                handle.write(b"launcher")
            with mock.patch.object(cli.os, "name", "posix"), \
                    mock.patch.object(cli.sys, "argv", [launcher, "update"]), \
                    mock.patch.object(cli.subprocess, "Popen") as popen:
                self.assertFalse(cli._defer_pip_to_detached("update", "xiaoyu-agent"))
            popen.assert_not_called()

    def test_no_deferring_when_not_launched_from_exe(self):
        """`python -m xiaoyu update` 没有自锁问题，照旧在本进程里升。"""
        with mock.patch.object(cli.os, "name", "nt"), \
                mock.patch.object(cli.sys, "argv", ["-m", "update"]), \
                mock.patch.object(cli.subprocess, "Popen") as popen:
            self.assertFalse(cli._defer_pip_to_detached("update", "xiaoyu-agent"))
        popen.assert_not_called()

    def test_windows_failure_hint_survives_inline_path(self):
        """本进程里硬跑失败时，那条"另开终端"的兜底命令必须还在。"""
        def fake_run(argv, **kwargs):
            returncode = 1 if "install" in argv else 0
            return SimpleNamespace(returncode=returncode, stdout="")

        with mock.patch.object(cli.os, "name", "nt"), \
                mock.patch.object(cli.sys, "argv", ["-m", "update"]), \
                mock.patch.object(cli.subprocess, "run", side_effect=fake_run), \
                _extras():
            code, output = _run([])
        self.assertEqual(code, 1)
        self.assertIn(cli._WINDOWS_MANUAL_UPGRADE, output)

    def test_main_dispatches_update(self):
        with mock.patch.object(cli, "update_command", return_value=0) as command:
            code = cli.main(["update"])
        self.assertEqual(code, 0)
        command.assert_called_once_with([])


if __name__ == "__main__":
    unittest.main()
