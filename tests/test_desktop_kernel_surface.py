"""桌面应用对内核的依赖面：它 import 的每个内核名字都必须存在，且不是私有名。

桌面应用（labs/desktop）与内核同仓、同一次提交构建，直接使用内核模块而不只走 HTTP。
这些名字不在 `xiaoyu.__all__` 的 semver 承诺里，所以内核重构时可以改——但要在这里
当场知道桌面应用在用它，而不是等桌面那套测试在别处间接失败。

依赖清单不手写：直接从桌面源码的 import 语句读出来，新增依赖自动纳入。
"""

from __future__ import annotations

import ast
import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "labs" / "desktop"
KERNEL = ("xiaoyu", "xiaoyu_agent_sdk")
#  SDK 在仓库里是源码目录，不一定装进了当前环境（与 tests/test_sdk_*.py 同一做法）
sys.path.insert(0, str(ROOT / "packages/xiaoyu-agent-sdk/src"))


def kernel_imports() -> dict[tuple[str, str | None], list[str]]:
    """(模块, 名字或 None) → 出现的位置。只看桌面应用自身的源码，不含它的测试。"""
    found: dict[tuple[str, str | None], list[str]] = {}
    for path in sorted(DESKTOP.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if "tests" in relative.parts or "local-results" in relative.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
        for node in ast.walk(tree):
            where = f"{relative}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                if node.module.split(".")[0] in KERNEL:
                    for alias in node.names:
                        found.setdefault((node.module, alias.name), []).append(where)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] in KERNEL:
                        found.setdefault((alias.name, None), []).append(where)
    return found


@unittest.skipUnless(DESKTOP.is_dir(), "这份源码里没有桌面应用")
class DesktopKernelSurfaceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.imports = kernel_imports()

    def test_desktop_actually_depends_on_the_kernel(self) -> None:
        """清单读空说明扫描本身坏了，下面两项会无声通过。"""
        self.assertGreater(len(self.imports), 10)
        self.assertIn(("xiaoyu.serve", "create_app"), self.imports)

    def test_every_imported_name_exists(self) -> None:
        missing = []
        for (module, name), places in sorted(self.imports.items(), key=lambda kv: (kv[0][0], kv[0][1] or "")):
            try:
                loaded = importlib.import_module(module)
            except ImportError as exc:
                missing.append(f"{module}（{exc}）← {places[0]}")
                continue
            if name is None or name == "*" or hasattr(loaded, name):
                continue
            try:  # from 包 import 子模块
                importlib.import_module(f"{module}.{name}")
            except ImportError:
                missing.append(f"{module}.{name} ← {places[0]}")
        self.assertEqual(missing, [], "桌面应用在用这些内核名字，改名或删除时要同步改它")

    def test_no_private_names(self) -> None:
        """下划线开头的名字内核可以随手改；桌面应用要用，就先把它改成正式名字。"""
        private = [f"{module}.{name} ← {places[0]}"
                   for (module, name), places in sorted(self.imports.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
                   if name and name.startswith("_") or any(part.startswith("_") for part in module.split("."))]
        self.assertEqual(private, [])


if __name__ == "__main__":
    unittest.main()
