"""AcpSink 的 write_file 预览：读旧内容前的类型/编码/体积闸。"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import acp
from xiaoyu.acp import AcpSink


class WriteFilePreviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self._tmp.name)
        self.sink = AcpSink(mock.Mock(), "sess-1", self.workspace)

    def tearDown(self):
        self._tmp.cleanup()

    def extras(self, path: str) -> dict:
        return self.sink._pending_extras("write_file", {"path": path, "content": "新内容\n"})

    def test_new_file_has_null_old_text(self):
        diff = self.extras("新文件.txt")["content"][0]
        self.assertIsNone(diff["oldText"])
        self.assertEqual(diff["newText"], "新内容\n")

    def test_existing_utf8_file_shows_old_text(self):
        (self.workspace / "a.txt").write_text("旧内容\n", encoding="utf-8")
        self.assertEqual(self.extras("a.txt")["content"][0]["oldText"], "旧内容\n")

    @unittest.skipUnless(hasattr(os, "mkfifo"), "需要 FIFO")
    def test_fifo_is_not_read(self):
        os.mkfifo(self.workspace / "pipe")
        result: dict = {}
        worker = threading.Thread(target=lambda: result.update(self.extras("pipe")), daemon=True)
        worker.start()
        worker.join(timeout=5)
        #  读 FIFO 会一直阻塞到有人写入：挂住就是没拦下
        self.assertFalse(worker.is_alive(), "预览读 FIFO 挂死")
        self.assertNotIn("content", result)
        self.assertIn("locations", result)

    def test_non_utf8_file_gives_no_diff_instead_of_raising(self):
        (self.workspace / "gbk.txt").write_bytes("中文".encode("gbk"))
        extras = self.extras("gbk.txt")
        #  oldText=null 会被 client 当成新建：解不开宁可不给 diff
        self.assertNotIn("content", extras)
        self.assertIn("locations", extras)

    def test_oversized_file_gives_no_diff(self):
        (self.workspace / "big.txt").write_text("x" * 64, encoding="utf-8")
        with mock.patch.object(acp, "_PREVIEW_MAX_BYTES", 16):
            extras = self.extras("big.txt")
        self.assertNotIn("content", extras)


if __name__ == "__main__":
    unittest.main()
