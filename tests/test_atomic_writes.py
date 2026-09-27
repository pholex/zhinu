"""状态文件落盘的回归哨兵 + fsguard.write_atomic 的行为测试。

核心不变量：**包内除 fsguard 外不得手写"临时文件 + 改名"**。各处自己写的时候
每一份都漏过点什么——固定的 `.tmp` 名让两个会话互相踩、写完才 chmod 让密钥
文件有一瞬按 umask 可读、失败时把临时文件留在原地。哨兵用 AST 扫 `os.replace`
/ `os.rename` 调用；真要挪目录的地方进白名单并写明理由。
"""

from __future__ import annotations

import ast
import os
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import fsguard

_PKG_DIR = Path(__file__).resolve().parent.parent / "xiaoyu"

#  文件名 → 为什么这里可以直接改名
_ALLOWED = {
    "fsguard.py": "write_atomic 本身",
    "plugins.py": "插件目录整体换代（挪的是目录，不是写文件）",
}


def _rename_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        if (
            node.func.attr in ("replace", "rename")
            and isinstance(owner, ast.Name)
            and owner.id == "os"
        ):
            hits.append(f"{path.name}:{node.lineno} os.{node.func.attr}(…)")
    return hits


class TestNoHandRolledAtomicWrites(unittest.TestCase):
    def test_only_fsguard_renames_files_into_place(self) -> None:
        violations: list[str] = []
        for path in sorted(_PKG_DIR.rglob("*.py")):
            if "__pycache__" in path.parts or path.name in _ALLOWED:
                continue
            violations.extend(_rename_calls(path))
        self.assertEqual(
            violations,
            [],
            "状态文件落盘走 fsguard.write_atomic，别手写临时文件 + 改名。违例：\n"
            + "\n".join(violations),
        )

    def test_allowlist_entries_still_exist(self) -> None:
        for name in _ALLOWED:
            self.assertTrue((_PKG_DIR / name).is_file(), f"白名单里的 {name} 已不存在，该删了")


class WriteAtomicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.root.rglob("*") if p.name.endswith(".tmp"))

    def test_writes_text_and_bytes_and_creates_parents(self) -> None:
        target = self.root / "a" / "b" / "state.json"
        fsguard.write_atomic(target, "第一版")
        self.assertEqual(target.read_text(encoding="utf-8"), "第一版")
        fsguard.write_atomic(target, b"\x00\x01")
        self.assertEqual(target.read_bytes(), b"\x00\x01")
        self.assertEqual(self.leftovers(), [])

    @unittest.skipIf(os.name == "nt", "POSIX 权限位")
    def test_private_file_is_never_wider_than_owner_only(self) -> None:
        """创建的那一刻就是 0600——写完再 chmod 留着一个按 umask 可读的窗口。"""
        target = self.root / "secrets.env"
        seen: list[int] = []
        real_replace = os.replace

        def spy(src, dst):
            seen.append(stat.S_IMODE(os.stat(src).st_mode))
            return real_replace(src, dst)

        old_umask = os.umask(0o022)
        self.addCleanup(os.umask, old_umask)
        with mock.patch.object(fsguard.os, "chmod", side_effect=AssertionError("不该靠事后 chmod")):
            with mock.patch.object(fsguard.os, "replace", side_effect=spy):
                fsguard.write_atomic(target, "KEY=1\n", private=True)
        self.assertEqual(seen, [0o600])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX 权限位")
    def test_private_write_tightens_a_previously_loose_file(self) -> None:
        target = self.root / "mcp.json"
        target.write_text("{}", encoding="utf-8")
        os.chmod(target, 0o644)
        fsguard.write_atomic(target, "{}", private=True)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    @unittest.skipIf(os.name == "nt", "POSIX 权限位")
    def test_plain_write_keeps_the_mode_the_user_set(self) -> None:
        target = self.root / "notes.json"
        target.write_text("{}", encoding="utf-8")
        os.chmod(target, 0o640)
        fsguard.write_atomic(target, "{\"a\": 1}")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)

    def test_failure_leaves_target_untouched_and_no_temp_file(self) -> None:
        target = self.root / "state.json"
        target.write_text("原样", encoding="utf-8")
        with mock.patch.object(fsguard.os, "replace", side_effect=OSError("磁盘满了")):
            with self.assertRaises(OSError):
                fsguard.write_atomic(target, "新内容")
        self.assertEqual(target.read_text(encoding="utf-8"), "原样")
        self.assertEqual(self.leftovers(), [])

    def test_replace_retries_while_target_is_busy(self) -> None:
        """目标被占用是瞬时的（Windows 上并发改名会撞见）：等一下再来，别把错抛给调用方。"""
        target = self.root / "state.json"
        target.write_text("原样", encoding="utf-8")
        real_replace = os.replace
        calls: list[int] = []

        def busy_twice(src, dst):  # noqa: ANN001, ANN202
            calls.append(1)
            if len(calls) <= 2:
                raise PermissionError(13, "Access is denied")
            return real_replace(src, dst)

        with mock.patch.object(fsguard, "_REPLACE_ATTEMPTS", 5), mock.patch.object(
            fsguard.time, "sleep"
        ), mock.patch.object(fsguard.os, "replace", side_effect=busy_twice):
            fsguard.write_atomic(target, "新内容")
        self.assertEqual(len(calls), 3)
        self.assertEqual(target.read_text(encoding="utf-8"), "新内容")
        self.assertEqual(self.leftovers(), [])

    def test_replace_gives_up_after_the_last_attempt(self) -> None:
        target = self.root / "state.json"
        target.write_text("原样", encoding="utf-8")
        with mock.patch.object(fsguard, "_REPLACE_ATTEMPTS", 3), mock.patch.object(
            fsguard.time, "sleep"
        ), mock.patch.object(
            fsguard.os, "replace", side_effect=PermissionError(13, "Access is denied")
        ) as replace:
            with self.assertRaises(PermissionError):
                fsguard.write_atomic(target, "新内容")
        self.assertEqual(replace.call_count, 3)
        self.assertEqual(target.read_text(encoding="utf-8"), "原样")
        self.assertEqual(self.leftovers(), [])

    def test_concurrent_writers_never_produce_a_torn_file(self) -> None:
        target = self.root / "shared.json"
        bodies = [str(index) * 20000 for index in range(8)]
        errors: list[BaseException] = []

        def write(body: str) -> None:
            try:
                for _ in range(20):
                    fsguard.write_atomic(target, body)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=write, args=(body,)) for body in bodies]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertEqual(errors, [])
        self.assertIn(target.read_text(encoding="utf-8"), bodies)
        self.assertEqual(self.leftovers(), [])


if __name__ == "__main__":
    unittest.main()
