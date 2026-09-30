"""局部替换不该改写没碰的字节：换行不纯的文件里，少数派的行与孤立的 \\r 原样留着。"""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from .test_tools import ToolboxTestCase


class MixedNewlineEditTest(ToolboxTestCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch("xiaoyu.tools.locale.getpreferredencoding", return_value="UTF-8")
        patcher.start()
        self.addCleanup(patcher.stop)

    def edit(self, name: str, data: bytes, old: str, new: str) -> bytes:
        target: Path = self.root / name
        target.write_bytes(data)
        self.box.run("read_file", {"path": name})
        result = self.box.run("str_replace", {"path": name, "old_str": old, "new_str": new})
        self.assertNotIn("ERROR", result)
        return target.read_bytes()

    def test_minority_lines_keep_their_own_newline(self) -> None:
        after = self.edit("mixed.txt", b"a\r\nb\r\nc\nd\r\n", "b", "B")
        self.assertEqual(after, b"a\r\nB\r\nc\nd\r\n")

    def test_lone_carriage_return_inside_an_lf_file_is_content(self) -> None:
        #  CSV 字段里的 \r、测试夹具里的控制字符：变成 \n 就是内容被改了
        after = self.edit("data.csv", b"id,note\n1,line one\rline two\n2,x\n", "2,x", "2,y")
        self.assertEqual(after, b"id,note\n1,line one\rline two\n2,y\n")

    def test_new_lines_take_the_majority_newline(self) -> None:
        after = self.edit("mixed.txt", b"a\r\nb\r\nc\nd\r\n", "b", "B\nB2")
        self.assertEqual(after, b"a\r\nB\r\nB2\r\nc\nd\r\n")

    def test_edit_next_to_a_minority_newline_leaves_it_alone(self) -> None:
        after = self.edit("mixed.txt", b"a\r\nb\r\nc\nd\r\n", "c", "C")
        self.assertEqual(after, b"a\r\nb\r\nC\nd\r\n")

    def test_deleting_a_line_removes_only_that_line(self) -> None:
        after = self.edit("mixed.txt", b"a\r\nb\r\nc\nd\r\n", "b\n", "")
        self.assertEqual(after, b"a\r\nc\nd\r\n")

    def test_fuzzy_path_keeps_untouched_bytes_too(self) -> None:
        #  old_str 少了行尾空格：走容错匹配
        after = self.edit(
            "mixed.py", b"def f():\r\n    return 1  \r\nx = 1\ny = 2\r\n", "    return 1", "    return 2"
        )
        self.assertIn(b"return 2", after)
        self.assertIn(b"x = 1\ny = 2\r\n", after)
        self.assertTrue(after.startswith(b"def f():\r\n"))

    def test_bom_and_encoding_survive_the_splice(self) -> None:
        data = b"\xef\xbb\xbf" + "甲\r\n乙\n丙\r\n丁\r\n".encode("utf-8")
        after = self.edit("bom.txt", data, "丙", "丙丙")
        self.assertEqual(after, b"\xef\xbb\xbf" + "甲\r\n乙\n丙丙\r\n丁\r\n".encode("utf-8"))
        gbk = "第一行\r\n第二行\n第三行\r\n末行\r\n".encode("gbk")
        after = self.edit("gbk.txt", gbk, "第三行", "改过的")
        self.assertEqual(after, "第一行\r\n第二行\n改过的\r\n末行\r\n".encode("gbk"))

    def test_uniform_files_are_written_exactly_as_before(self) -> None:
        self.assertEqual(self.edit("win.txt", b"a\r\nb\r\n", "b", "B\nC"), b"a\r\nB\r\nC\r\n")
        self.assertEqual(self.edit("unix.txt", b"a\nb\n", "b", "B\nC"), b"a\nB\nC\n")
        self.assertEqual(self.edit("mac.txt", b"a\rb\r", "b", "B"), b"a\rB\r")


if __name__ == "__main__":
    unittest.main()
