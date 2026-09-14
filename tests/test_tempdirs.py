"""临时目录生命周期的测试：谁创建谁清理 + 启动期清扫的安全边界。

清扫一律对着一次性的假 root 跑，绝不碰真实 $TMPDIR。
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import background as bg
from xiaoyu import tempdirs
from xiaoyu.config import Config
from xiaoyu.tools import Toolbox

POSIX = sys.platform != "win32"
DAY = 86400.0


def _dead_pid() -> int:
    """拿一个刚退出、已被回收的 pid（毫秒内被复用的概率可忽略）。"""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _isolate_owned(case: unittest.TestCase) -> None:
    """登记表换成用例私有的一份：cleanup_owned 是进程级的，不隔离会顺手删掉
    （或在 rmtree 被 mock 时直接丢掉登记）同进程里其它用例建的目录。"""
    patcher = mock.patch.object(tempdirs, "_owned", set())
    patcher.start()
    case.addCleanup(patcher.stop)


def _age(path: Path, days: float) -> None:
    stamp = time.time() - days * DAY
    os.utime(path, (stamp, stamp), follow_symlinks=False)


class OwnedDirsTest(unittest.TestCase):
    def setUp(self) -> None:
        _isolate_owned(self)

    def test_make_dir_embeds_pid_and_cleanup_removes_with_content(self) -> None:
        path = tempdirs.make_dir("xiaoyu-spill-")
        self.addCleanup(tempdirs.discard, path)
        self.assertTrue(path.name.startswith(f"xiaoyu-spill-{os.getpid()}-"))
        (path / "001-bash.txt").write_text("x", encoding="utf-8")
        tempdirs.cleanup_owned()
        self.assertFalse(path.exists())

    def test_discard_is_idempotent_and_tolerates_missing(self) -> None:
        path = tempdirs.make_dir("xiaoyu-bg-")
        tempdirs.discard(path)
        tempdirs.discard(path)
        self.assertFalse(path.exists())

    def test_cleanup_swallows_removal_failures(self) -> None:
        """Windows 上文件被占用删不掉：吞掉，留给下次启动清扫。"""
        path = tempdirs.make_dir("xiaoyu-bg-")
        self.addCleanup(tempdirs.discard, path)
        with mock.patch.object(tempdirs.shutil, "rmtree", side_effect=PermissionError("占用")):
            tempdirs.cleanup_owned()
        self.assertTrue(path.exists())


@unittest.skipUnless(POSIX, "用例依赖 POSIX shell")
class OwnersCleanUpTest(unittest.TestCase):
    def setUp(self) -> None:
        _isolate_owned(self)

    def test_task_manager_shutdown_removes_log_dir(self) -> None:
        manager = bg.TaskManager()
        self.addCleanup(manager.shutdown)
        task = manager.start(["/bin/sh", "-c", "echo hi; sleep 30"], command="x")
        self.assertNotIsInstance(task, str, task)
        log_dir = task.log_path.parent
        self.assertTrue(log_dir.is_dir())
        manager.shutdown()
        self.assertFalse(log_dir.exists())

    def test_spill_dir_is_owned_and_removed_on_cleanup(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = Config(
            base_url="http://unused", model="unused",
            workspace=Path(tmp.name).resolve(), enable_plugins=False,
        )
        config.max_tool_output = 50
        box = Toolbox(config)
        box.run("bash", {"command": "python3 -c \"print('x' * 500)\""})
        spill_dir = box._spill_dir  # noqa: SLF001
        self.assertIsNotNone(spill_dir)
        self.addCleanup(tempdirs.discard, spill_dir)
        self.assertTrue(spill_dir.name.startswith("xiaoyu-spill-"))
        tempdirs.cleanup_owned()
        self.assertFalse(spill_dir.exists())


class SweepTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def mkdir(self, name: str, days: float, *, files: int = 1) -> Path:
        path = self.root / name
        path.mkdir()
        for index in range(files):
            child = path / f"{index}.txt"
            child.write_text("x", encoding="utf-8")
            _age(child, days)
        _age(path, days)
        return path

    def sweep(self) -> list[Path]:
        return tempdirs.sweep_stale(self.root)

    def test_stale_dir_of_dead_process_is_removed(self) -> None:
        path = self.mkdir(f"xiaoyu-spill-{_dead_pid()}-abc123", days=8)
        self.sweep()
        self.assertFalse(path.exists())

    def test_live_process_dir_is_kept_however_old(self) -> None:
        """长会话：pid 还活着就绝不动，mtime 再老也一样。"""
        path = self.mkdir(f"xiaoyu-bg-{os.getpid()}-abc123", days=30)
        self.sweep()
        self.assertTrue(path.exists())

    def test_fresh_dir_is_kept_even_if_owner_dead(self) -> None:
        """pid 判不准（别的 pid 命名空间共享 /tmp）时，年龄门槛仍然兜着。"""
        path = self.mkdir(f"xiaoyu-spill-{_dead_pid()}-abc123", days=1)
        self.sweep()
        self.assertTrue(path.exists())

    def test_legacy_names_fall_back_to_age(self) -> None:
        stale = self.mkdir("xiaoyu-assert-k2j4h5g6", days=8, files=0)
        fresh = self.mkdir("xiaoyu-bg-z9y8x7w6", days=2, files=0)
        self.sweep()
        self.assertFalse(stale.exists())
        self.assertTrue(fresh.exists())

    def test_recent_write_inside_keeps_old_dir(self) -> None:
        path = self.mkdir("xiaoyu-bg-q1w2e3r4", days=8, files=2)
        (path / "0.txt").write_text("刚写", encoding="utf-8")
        _age(path, 8)
        self.sweep()
        self.assertTrue(path.exists())

    def test_foreign_prefix_is_never_touched(self) -> None:
        path = self.mkdir("someone-else-bg-abc", days=30)
        near = self.mkdir("xiaoyu-bgx-abc", days=30)
        self.sweep()
        self.assertTrue(path.exists())
        self.assertTrue(near.exists())

    def test_stale_litter_file_is_removed(self) -> None:
        path = self.root / "xiaoyu-probe-fallback-abc-probe.py"
        path.write_text("x", encoding="utf-8")
        _age(path, 8)
        self.sweep()
        self.assertFalse(path.exists())

    @unittest.skipUnless(POSIX, "符号链接在 Windows 上需要特权")
    def test_symlink_is_not_followed(self) -> None:
        target = self.root / "precious"
        target.mkdir()
        (target / "keep.txt").write_text("x", encoding="utf-8")
        link = self.root / "xiaoyu-bg-l1n2k3a4"
        link.symlink_to(target)
        _age(link, 30)
        self.sweep()
        self.assertTrue((target / "keep.txt").exists())

    def test_failures_are_swallowed(self) -> None:
        self.mkdir(f"xiaoyu-spill-{_dead_pid()}-abc123", days=8)
        with mock.patch.object(tempdirs.shutil, "rmtree", side_effect=PermissionError("占用")):
            self.assertEqual(self.sweep(), [])
        with mock.patch.object(tempdirs.os, "scandir", side_effect=OSError("坏了")):
            self.assertEqual(self.sweep(), [])
        self.assertEqual(tempdirs.sweep_stale(self.root / "不存在"), [])


class BackgroundSweepTest(unittest.TestCase):
    def test_runs_at_most_once_per_process(self) -> None:
        calls: list[object] = []
        done = threading.Event()

        def fake_sweep(*args: object, **kwargs: object) -> list[Path]:
            calls.append(args)
            done.set()
            return []

        with mock.patch.object(tempdirs, "_sweep_started", False), \
                mock.patch.object(tempdirs, "sweep_stale", side_effect=fake_sweep):
            tempdirs.sweep_in_background()
            tempdirs.sweep_in_background()
            self.assertTrue(done.wait(5))
            time.sleep(0.1)
        self.assertEqual(len(calls), 1)

    def test_cli_entry_mounts_the_sweep(self) -> None:
        from xiaoyu import cli

        with mock.patch.object(tempdirs, "sweep_in_background") as mounted, \
                mock.patch.object(cli, "config_command", return_value=0):
            cli.main(["config"])
        mounted.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
