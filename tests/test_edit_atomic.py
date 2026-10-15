"""真实写到一半失败时，编辑工具不能损坏原文件或留下半个新文件。"""

import errno
import os
import stat
from pathlib import Path
from unittest import mock

from xiaoyu import fsguard, tools
from .test_tools import ToolboxTestCase


class AtomicEditTest(ToolboxTestCase):
    def test_partial_write_failure_keeps_original_and_cleans_new_file(self):
        original_open, original_fdopen = Path.open, os.fdopen

        class FailingWriter:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                self.handle.__enter__()
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def write(self, data):
                self.handle.write(data[:2])
                self.handle.flush()
                raise OSError(errno.ENOSPC, "injected disk full")

        def opening(path, mode="r", *args, **kwargs):
            handle = original_open(path, mode, *args, **kwargs)
            return FailingWriter(handle) if "w" in mode else handle

        cases = [
            ("write_file", b"old\r\n", {"content": "new\n"}),
            ("write_file", None, {"content": "new\n"}),
            ("str_replace", b"old\r\n", {"old_str": "old", "new_str": "new"}),
            ("str_replace", b"old\r\nkeep\n", {"old_str": "old", "new_str": "new"}),
            ("str_replace", b"  old\n  keep\n", {"old_str": "    old\n    keep\n", "new_str": "    new\n    keep\n"}),
        ]
        for index, (name, before, args) in enumerate(cases):
            with self.subTest(name=name, index=index):
                target = self.root / f"edit-{index}.txt"
                if before is not None:
                    target.write_bytes(before)
                    self.box.run("read_file", {"path": target.name})
                with mock.patch.object(Path, "open", opening), mock.patch.object(
                    fsguard.os, "fdopen", side_effect=lambda *a, **k: FailingWriter(original_fdopen(*a, **k))
                ):
                    result = self.box.run(name, {"path": target.name, **args})
                self.assertIn("写入失败", result)
                self.assertEqual(target.read_bytes() if target.exists() else None, before)
                self.assertEqual(list(self.root.glob(".*.tmp")), [])
                # 错误不会把读标记推进到新内容；恢复磁盘后同一调用能成功。
                self.assertFalse(self.box.run(name, {"path": target.name, **args}).startswith("ERROR:"))

    def test_replace_failure_keeps_original(self):
        self.box.run("read_file", {"path": "calc.py"})
        original = self.sample.read_bytes()
        with mock.patch.object(fsguard, "_replace", side_effect=OSError("replace failed")):
            result = self.box.run("write_file", {"path": "calc.py", "content": "changed"})
        self.assertIn("写入失败", result)
        self.assertEqual(self.sample.read_bytes(), original)
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_symlink_target_mode_and_encoded_bytes_survive(self):
        # 编辑链路按 UTF-8 → 本地代码页 → GBK 的顺序解码，英文 Windows 的 cp1252
        # 排在 GBK 前面且几乎什么字节都认，所以"非 UTF-8 原文"要按本机排在
        # UTF-8 之后的那个编码来造，否则 GBK 字节会被当成 cp1252 乱码读出。
        encoding = next(e for e in tools._edit_encodings() if e != "utf-8")
        old, new, keep = ("旧行", "新行", "保留") if encoding == "gbk" else ("ligne \u00e9", "ligne \u00e8", "gard\u00e9")
        target = self.root / "real.txt"
        target.write_bytes(f"{old}\r\n{keep}\r\n".encode(encoding))
        if os.name != "nt":
            target.chmod(0o750)
            link = self.root / "alias.txt"
            link.symlink_to(target.name)
        else:
            link = target
        self.box.run("read_file", {"path": link.name})
        result = self.box.run("str_replace", {"path": link.name, "old_str": old, "new_str": new})
        self.assertFalse(result.startswith("ERROR:"), result)
        self.assertEqual(target.read_bytes(), f"{new}\r\n{keep}\r\n".encode(encoding))
        if os.name != "nt":
            self.assertTrue(link.is_symlink())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o750)

    def test_existing_write_permission_failure_cannot_be_bypassed_by_rename(self):
        self.box.run("read_file", {"path": "calc.py"})
        original_open = os.open
        def denied(path, flags, *args, **kwargs):
            if Path(path) == self.sample and flags & os.O_WRONLY:
                raise PermissionError(errno.EACCES, "read only")
            return original_open(path, flags, *args, **kwargs)
        original = self.sample.read_bytes()
        with mock.patch.object(fsguard.os, "open", side_effect=denied):
            result = self.box.run("write_file", {"path": "calc.py", "content": "changed"})
        self.assertIn("写入失败", result)
        self.assertEqual(self.sample.read_bytes(), original)

    def test_private_original_has_private_temporary_file_before_writing(self):
        if os.name == "nt":
            self.skipTest("POSIX 权限位")
        self.sample.chmod(0o600)
        self.box.run("read_file", {"path": "calc.py"})
        original_fdopen = os.fdopen
        seen = []
        def opening(fd, *args, **kwargs):
            seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
            return original_fdopen(fd, *args, **kwargs)
        with mock.patch.object(fsguard.os, "fdopen", side_effect=opening):
            self.box.run("write_file", {"path": "calc.py", "content": "changed"})
        self.assertEqual(seen, [0o600])

    def test_hardlink_other_path_keeps_old_content(self):
        alias = self.root / "hardlink.py"
        os.link(self.sample, alias)
        before = alias.read_bytes()
        self.box.run("read_file", {"path": "calc.py"})
        result = self.box.run("write_file", {"path": "calc.py", "content": "changed"})
        self.assertFalse(result.startswith("ERROR:"), result)
        self.assertEqual(self.sample.read_bytes(), b"changed")
        self.assertEqual(alias.read_bytes(), before)
