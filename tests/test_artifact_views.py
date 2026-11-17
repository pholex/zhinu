"""Delivery snapshots never substitute current bytes or allow path escapes."""
from __future__ import annotations

import base64
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from xiaoyu.artifacts import FileArtifact
from xiaoyu.artifact_views import TEXT_BYTES, capture, read_file
from xiaoyu.serve_artifacts import inspect


@unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "需要安全目录句柄")
class ArtifactViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "project"
        self.workspace.mkdir()

    def artifact(self, content=b"new\n", operation="updated"):
        path = self.workspace / "result.md"
        path.write_bytes(content)
        item = FileArtifact("file-1", "result.md", len(content), hashlib.sha256(content).hexdigest(), operation, "file_tool")
        return path, item

    def test_delivery_and_diff_survive_later_edit_and_deletion(self):
        from dataclasses import asdict
        path, item = self.artifact()
        point = SimpleNamespace(files={str(path): b"old\n"}, after={str(path): item.sha256})
        snapshot = capture(self.workspace, (item,), point)[0]
        path.write_text("user changed\n", encoding="utf-8")
        view = inspect(asdict(item), self.workspace, snapshot)
        self.assertEqual(view["version"], "current_changed")
        self.assertEqual(view["delivered"]["text"], "new\n")
        self.assertEqual(view["current"]["text"], "user changed\n")
        self.assertIn("-old", view["diff"]["text"])
        self.assertIn("+new", view["diff"]["text"])
        self.assertNotIn("user changed", view["diff"]["text"])
        path.unlink()
        missing = inspect(asdict(item), self.workspace, snapshot)
        self.assertEqual(missing["version"], "missing")
        self.assertFalse(missing["can_reveal"])
        self.assertEqual(missing["delivered"]["text"], "new\n")

    def test_old_metadata_requires_matching_current_hash_and_never_invents_diff(self):
        from dataclasses import asdict
        path, item = self.artifact()
        view = inspect(asdict(item), self.workspace, {})
        self.assertTrue(view["delivered"]["available"])
        self.assertFalse(view["diff"]["available"])
        path.write_bytes(b"different")
        changed = inspect(asdict(item), self.workspace, {})
        self.assertFalse(changed["delivered"]["available"])
        self.assertEqual(changed["current"]["text"], "different")

    def test_links_traversal_special_and_git_files_are_not_read(self):
        secret = self.root / "private.txt"
        secret.write_text("outside", encoding="utf-8")
        (self.workspace / "link").symlink_to(secret)
        (self.workspace / "directory").symlink_to(self.root, target_is_directory=True)
        os.link(secret, self.workspace / "hardlink")
        (self.workspace / ".git").mkdir()
        (self.workspace / ".git/config").write_text("private", encoding="utf-8")
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.workspace / "fifo")
        for value in ["link", "directory/private.txt", "hardlink", "../private.txt", str(secret), ".git/config", "./link", "a//b", "fifo"]:
            with self.subTest(value=value), self.assertRaises((OSError, ValueError)):
                read_file(self.workspace, value)

    def test_binary_large_and_invalid_utf8_have_explicit_safe_views(self):
        from dataclasses import asdict
        for content, available, truncated in [(b"a\x00b", False, False), (b"x" * (TEXT_BYTES + 1), True, True), (b"\xff\n", True, False)]:
            with self.subTest(size=len(content)):
                _, item = self.artifact(content)
                saved = capture(self.workspace, (item,))[0]
                view = inspect(asdict(item), self.workspace, saved)
                self.assertEqual(view["current"]["available"], available)
                self.assertEqual(view["current"]["truncated"], truncated)
                self.assertLessEqual(len(view["current"]["text"]), TEXT_BYTES)

    def test_changed_during_capture_is_not_saved_as_delivery(self):
        path, item = self.artifact()
        path.write_bytes(b"newer")
        snapshot = capture(self.workspace, (item,))[0]
        self.assertFalse(snapshot["delivered"]["available"])

    def test_bad_delivery_snapshot_is_not_trusted(self):
        from dataclasses import asdict
        path, item = self.artifact()
        path.unlink()
        saved = {"delivered": {"available": True, "base64": base64.b64encode(b"wrong bytes").decode()}}
        view = inspect(asdict(item), self.workspace, saved)
        self.assertFalse(view["delivered"]["available"])

    def test_created_file_diff_uses_empty_before(self):
        from dataclasses import asdict
        _, item = self.artifact(operation="created")
        view = inspect(asdict(item), self.workspace, capture(self.workspace, (item,))[0])
        self.assertTrue(view["diff"]["available"])
        self.assertIn("+new", view["diff"]["text"])


class UnsupportedPlatformTests(unittest.TestCase):
    def test_unsupported_safe_reads_preserve_metadata_and_report_missing_preview(self):
        with tempfile.TemporaryDirectory() as directory, patch("xiaoyu.artifact_views.os.supports_dir_fd", set()):
            root = Path(directory).resolve()
            artifact = {"id": "a", "path": "result.txt", "sha256": "hash", "operation": "created"}
            snapshot = capture(root, [artifact])[0]
            self.assertEqual(snapshot["id"], "a")
            self.assertFalse(snapshot["delivered"]["available"])
            view = inspect(artifact, root, snapshot)
            self.assertEqual(view["version"], "unavailable")
            self.assertFalse(view["can_reveal"])
            self.assertIn("此平台", view["current"]["note"])
