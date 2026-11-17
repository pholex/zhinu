"""状态更新的坏内容、临时 IO 错误和备份失败窗口。"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from xiaoyu import config, folder_trust, plugins, statefiles


class StateFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"

    def test_missing_valid_bom_and_corrupt_bytes(self):
        self.assertEqual(statefiles.read_json_for_update(self.path), {})
        self.path.write_bytes(b'\xef\xbb\xbf{"a": 1}')
        self.assertEqual(statefiles.read_json_for_update(self.path), {"a": 1})
        raw = b'{"a": "\xff"}'
        self.path.write_bytes(raw)
        with self.assertLogs("xiaoyu.statefiles", "WARNING"):
            self.assertEqual(statefiles.read_json_for_update(self.path), {})
        self.assertEqual(next(self.path.parent.glob("*.corrupt-*")).read_bytes(), raw)
        self.assertEqual(self.path.read_bytes(), raw)

    def test_unreadable_or_failed_backup_never_allows_default_write(self):
        self.path.write_bytes(b"broken")
        with patch.object(Path, "read_bytes", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                statefiles.read_json_for_update(self.path)
        with patch("xiaoyu.statefiles.fsguard.write_atomic", side_effect=OSError("full")):
            with self.assertRaises(OSError):
                statefiles.read_json_for_update(self.path)
        self.assertEqual(self.path.read_bytes(), b"broken")

    def test_trust_registry_and_env_preserve_invalid_original(self):
        cases = (
            (patch.object(folder_trust, "trust_store_path", return_value=self.path),
             lambda: folder_trust.record_decision(self.path.parent / "project", True)),
            (patch.object(plugins, "registry_path", return_value=self.path),
             lambda: plugins.save_registry({"new": {}})),
            (patch.object(config, "user_env_path", return_value=self.path),
             lambda: config.save_user_env({"XIAOYU_MODEL": "test"})),
        )
        for patched, update in cases:
            with self.subTest(update=update), patched:
                self.path.write_bytes(b"damaged\xff")
                with self.assertLogs("xiaoyu.statefiles", "WARNING"):
                    update()
                self.assertNotEqual(self.path.read_bytes(), b"damaged\xff")
        self.assertEqual(len(list(self.path.parent.glob("*.corrupt-*"))), 3)
