"""Windows 保留设备名读闸：CON / NUL / COM1 带任意扩展名都是设备，stat 看不出来。

判定函数是纯字符串逻辑，在任何平台都测；"只在 Windows 生效"单独验。不打网络。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import fsguard
from xiaoyu.tools import _unreadable_file_error


class ReservedNameTest(unittest.TestCase):
    def test_reserved_names_with_any_case_and_extension(self) -> None:
        cases = {
            "CON": "CON",
            "con": "CON",
            "NUL.txt": "NUL",
            "nul.tar.gz": "NUL",
            "Com1": "COM1",
            "COM9.log": "COM9",
            "LPT1": "LPT1",
            "prn ": "PRN",
            "AUX.": "AUX",
        }
        for name, device in cases.items():
            self.assertEqual(fsguard.windows_reserved_name(name), device, name)

    def test_ordinary_names_pass(self) -> None:
        for name in ("console.py", "nullable.txt", "COM0", "COM10", "LPT0", "config", "NULL", "xCON"):
            self.assertIsNone(fsguard.windows_reserved_name(name), name)

    def test_kind_only_on_windows(self) -> None:
        path = Path("NUL.txt")
        with mock.patch("os.name", "nt"):
            self.assertIn("NUL", fsguard.reserved_device_kind(path) or "")
        with mock.patch("os.name", "posix"):
            self.assertIsNone(fsguard.reserved_device_kind(path))


class ReadGateTest(unittest.TestCase):
    def test_read_gate_refuses_reserved_name_on_windows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "NUL.txt"
            #  真 Windows 上这个名字就是设备，写它会写进 NUL：名字闸在 stat 之前，
            #  文件不必存在；只在别的平台造出同名普通文件做对照
            if os.name != "nt":
                target.write_text("not a device here", encoding="utf-8")
            with mock.patch("os.name", "nt"):
                error = _unreadable_file_error(target, "NUL.txt")
                with self.assertRaises(fsguard.NotRegularFile) as caught:
                    fsguard.require_regular(target)
            self.assertIsNotNone(error)
            self.assertIn("Windows 保留设备名", error)
            self.assertIn("NUL", error)
            self.assertIn("保留设备名", caught.exception.kind)
            if os.name != "nt":
                #  非 Windows：同名文件就是普通文件
                with mock.patch("os.name", "posix"):
                    self.assertIsNone(_unreadable_file_error(target, "NUL.txt"))
