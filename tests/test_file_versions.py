"""读标记必须绑定实际展示的字节，不能仅凭文件时间与大小拼接版本。"""

import os
from unittest import mock

from xiaoyu import tools
from .test_tools import ToolboxTestCase


class FileVersionTest(ToolboxTestCase):
    def change_preserving_metadata(self, target, raw):
        before = target.stat()
        self.assertEqual(len(raw), before.st_size)
        target.write_bytes(raw)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))

    def test_equal_size_and_timestamp_external_edit_is_rejected(self):
        for name, args in (
            ("write_file", {"content": "overwritten\n"}),
            ("str_replace", {"old_str": "a + b", "new_str": "a - b"}),
            ("str_replace", {"old_str": "return a + b  ", "new_str": "return a - b"}),
        ):
            with self.subTest(tool=name, args=args):
                self.sample.write_bytes(b"return a + b\n# owner=A\n")
                self.box.run("read_file", {"path": "calc.py"})
                changed = b"return a + b\n# owner=B\n"
                self.change_preserving_metadata(self.sample, changed)
                result = self.box.run(name, {"path": "calc.py", **args})
                self.assertIn("被改动过", result)
                self.assertEqual(self.sample.read_bytes(), changed)
                self.box.run("read_file", {"path": "calc.py"})
                self.assertFalse(self.box.run(name, {"path": "calc.py", **args}).startswith("ERROR:"))

    def test_partial_reads_from_different_versions_do_not_accumulate(self):
        self.sample.write_bytes(b"owner=A\nvalue=1\n")
        self.box.run("read_file", {"path": "calc.py", "offset": 1, "limit": 1})
        changed = b"owner=B\nvalue=1\n"
        self.change_preserving_metadata(self.sample, changed)
        self.box.run("read_file", {"path": "calc.py", "offset": 2, "limit": 1})
        self.assertIn("只读过一部分", self.box.run("write_file", {"path": "calc.py", "content": "new"}))
        self.assertEqual(self.sample.read_bytes(), changed)
        self.box.run("read_file", {"path": "calc.py", "offset": 1, "limit": 1})
        self.assertIn("已覆盖", self.box.run("write_file", {"path": "calc.py", "content": "new"}))

    def test_partial_read_of_new_version_does_not_inherit_full_read(self):
        self.sample.write_bytes(b"owner=A\nvalue=1\n")
        self.box.run("read_file", {"path": "calc.py"})
        self.change_preserving_metadata(self.sample, b"owner=B\nvalue=1\n")
        self.box.run("read_file", {"path": "calc.py", "offset": 2, "limit": 1})
        self.assertIn("只读过一部分", self.box.run("write_file", {"path": "calc.py", "content": "new"}))

    def test_change_between_read_and_mark_is_not_certified(self):
        original_read = tools._read_for_edit
        self.sample.write_bytes(b"owner=A\n")

        def read_then_change(target):
            result = original_read(target)
            self.change_preserving_metadata(target, b"owner=B\n")
            return result

        with mock.patch.object(tools, "_read_for_edit", side_effect=read_then_change):
            shown = self.box.run("read_file", {"path": "calc.py"})
        self.assertIn("owner=A", shown)
        self.assertIn("被改动过", self.box.run("write_file", {"path": "calc.py", "content": "new"}))
        self.assertEqual(self.sample.read_bytes(), b"owner=B\n")

    def test_change_between_write_and_mark_is_not_certified(self):
        original_write = tools.fsguard.write_atomic

        def write_then_change(target, data, **kwargs):
            original_write(target, data, **kwargs)
            self.change_preserving_metadata(target, b"owner=B\n")

        with mock.patch.object(tools.fsguard, "write_atomic", side_effect=write_then_change):
            self.assertIn("已创建", self.box.run("write_file", {"path": "new.txt", "content": "owner=A\n"}))
        self.assertIn("被改动过", self.box.run("write_file", {"path": "new.txt", "content": "new"}))
        self.assertEqual((self.root / "new.txt").read_bytes(), b"owner=B\n")

    def test_fingerprint_uses_raw_bytes_before_newline_normalization(self):
        self.sample.write_bytes(b"a\rb\n")
        self.box.run("read_file", {"path": "calc.py"})
        self.change_preserving_metadata(self.sample, b"a\nb\n")
        self.assertIn("被改动过", self.box.run("write_file", {"path": "calc.py", "content": "new"}))

    def test_lossy_read_still_tracks_original_bytes(self):
        self.sample.write_bytes(b"owner=\xff\n")
        self.assertIn("有损", self.box.run("read_file", {"path": "calc.py"}))
        self.change_preserving_metadata(self.sample, b"owner=\xfe\n")
        self.assertIn("被改动过", self.box.run("write_file", {"path": "calc.py", "content": "new"}))
