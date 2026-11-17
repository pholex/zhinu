"""文件交付以实际文件为准，不能凭模型声称或上一轮结果生成。"""
from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from xiaoyu.artifacts import ArtifactCollector
from xiaoyu.events import ArtifactsReady
from xiaoyu.render import artifact_summary


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.collector = ArtifactCollector(self.root)
        self.collector.begin()

    def write(self, name, before, content):
        path = self.root / name
        path.write_bytes(content)
        self.collector.written(path, before, content)

    def test_first_version_last_content_and_no_net_change(self):
        self.write("new.txt", None, b"one")
        first_id = self.collector._items["new.txt"].id
        self.write("new.txt", b"one", b"two")
        self.write("unchanged.txt", b"original", b"edit")
        self.write("unchanged.txt", b"edit", b"original")
        items = self.collector.finish(True)
        self.assertEqual(len(items), 1)
        self.assertEqual((items[0].path, items[0].operation, items[0].id), ("new.txt", "created", first_id))
        self.assertEqual(items[0].sha256, hashlib.sha256(b"two").hexdigest())
        self.assertEqual(items[0].size, 3)
        self.collector.begin()
        self.assertEqual(self.collector.finish(True), ())

    def test_external_changes_and_deleted_files_are_not_deliveries(self):
        self.write("external.txt", None, b"tool")
        self.write("deleted.txt", None, b"tool")
        (self.root / "external.txt").write_bytes(b"user")
        (self.root / "deleted.txt").unlink()
        self.assertEqual(self.collector.finish(True), ())

    def test_report_binary_and_incomplete_run(self):
        (self.root / "report.pdf").write_bytes(b"%PDF\x00\xff")
        self.collector.report("report.pdf")
        item, = self.collector.finish(False)
        self.assertEqual((item.operation, item.source, item.status), ("reported", "tool", "incomplete"))
        self.assertEqual(item.mime_type, "application/pdf")
        self.assertEqual(ArtifactsReady((item,)).to_dict()["artifacts"][0]["path"], "report.pdf")

    def test_rejects_outside_internal_missing_and_directory_paths_atomically(self):
        (self.root / "valid.txt").write_bytes(b"good")
        for bad in ("../outside.txt", ".git/config", "missing.txt", "."):
            with self.subTest(path=bad), self.assertRaises((ValueError, OSError)):
                self.collector.report_many(("valid.txt", bad))
            self.assertEqual(self.collector._items, {})

    def test_outside_symlink_is_not_a_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            outside = Path(directory) / "secret.txt"
            outside.write_text("secret")
            try:
                (self.root / "link.txt").symlink_to(outside)
            except OSError:
                self.skipTest("Symlink creation unavailable")
            with self.assertRaises(ValueError):
                self.collector.report("link.txt")

    @unittest.skipIf(os.name == "nt", "Windows rejects control characters in file names")
    def test_terminal_file_names_are_sanitized(self):
        self.write("bad\x1b[2J.txt", None, b"data")
        items = self.collector.finish(True)
        self.assertNotIn("\x1b", artifact_summary(ArtifactsReady(items)))
