"""子进程加固的回归哨兵。

核心不变量：**全仓任何代码不得使用 preexec_fn**。preexec_fn 强迫 CPython
放弃 posix_spawn 改走 fork()，并在 fork 与 exec 之间的子进程里跑 Python
字节码——xiaoyu 有常驻工作线程（browser、七襄/斗巧线程池），fork 瞬间其他
线程持有的锁在子进程里永远无人释放，子进程会在 exec 前死锁并钉住继承的
每个 fd。禁 core dump 的正确位置是父进程的 RLIMIT_CORE（跨 fork/exec 继承，
见 tools._harden_core_limit）。

哨兵用 AST 扫描而非文本匹配：抓 `preexec_fn=` 关键字参数与
`{"preexec_fn": ...}` 字典键两种形态，注释/文档里提到这个词不误伤。
"""

from __future__ import annotations

import ast
import os
import unittest
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent.parent / "xiaoyu"


def _preexec_violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == "preexec_fn":
                    hits.append(f"{path.name}:{node.lineno} 调用带 preexec_fn=")
        elif isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and key.value == "preexec_fn":
                    hits.append(f"{path.name}:{key.lineno} 字典含 'preexec_fn' 键")
    return hits


class TestNoPreexecFn(unittest.TestCase):
    def test_no_preexec_fn_anywhere(self) -> None:
        violations: list[str] = []
        for path in sorted(_PKG_DIR.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            violations.extend(_preexec_violations(path))
        self.assertEqual(
            violations,
            [],
            "preexec_fn 在多线程进程里是 fork 死锁源，禁止使用；"
            "rlimit 类需求走父进程继承（tools._harden_core_limit），"
            "其余需求走 exec 后的 wrapper。违例：\n" + "\n".join(violations),
        )


class TestHardening(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX only")
    def test_hardening_kwargs_and_core_limit(self) -> None:
        import resource

        from xiaoyu import tools

        kwargs = tools._subprocess_hardening()
        self.assertEqual(kwargs, {"start_new_session": True})
        #  调用过 _subprocess_hardening 后，父进程 core limit 必须已压为 (0, 0)，
        #  由所有后代继承。
        self.assertEqual(resource.getrlimit(resource.RLIMIT_CORE), (0, 0))

    def test_windows_returns_empty(self) -> None:
        from unittest import mock

        from xiaoyu import tools

        with mock.patch.object(tools.os, "name", "nt"):
            self.assertEqual(tools._subprocess_hardening(), {})


if __name__ == "__main__":
    unittest.main()
