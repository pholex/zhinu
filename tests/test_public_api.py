"""顶层公开面契约（docs/embedding.md「稳定性承诺」的机器可查部分）。

锁三件事：① `xiaoyu.__all__` 里每个名字都能从顶层拿到、且就是所在模块的
同一个对象（不是复制品）；② 文档清单与导出表**双向**一致——漏写文档或表里
多出没承诺的名字都要响亮失败；③ `import xiaoyu` 不拖起内核与 SDK（懒导出），
`--version` / 包内 `from . import __version__` 不为一个版本号付整包 import 的代价。
"""

from __future__ import annotations

import importlib
import re
import subprocess
import sys
import unittest
from pathlib import Path

import xiaoyu

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "embedding.md"


class TestExports(unittest.TestCase):
    def test_every_name_resolves_to_origin_object(self) -> None:
        for name in xiaoyu.__all__:
            if name == "__version__":
                continue
            module = importlib.import_module(xiaoyu._EXPORTS[name])
            with self.subTest(name=name):
                self.assertIs(getattr(xiaoyu, name), getattr(module, name))

    def test_all_matches_export_table(self) -> None:
        self.assertEqual(set(xiaoyu.__all__), {"__version__", *xiaoyu._EXPORTS})
        self.assertEqual(len(xiaoyu.__all__), len(set(xiaoyu.__all__)))

    def test_dir_and_star_import(self) -> None:
        listing = dir(xiaoyu)
        for name in xiaoyu.__all__:
            self.assertIn(name, listing)
        namespace: dict = {}
        exec("from xiaoyu import *", namespace)
        for name in xiaoyu.__all__:
            self.assertIn(name, namespace)

    def test_unknown_attribute_raises(self) -> None:
        with self.assertRaises(AttributeError):
            xiaoyu.definitely_not_exported  # noqa: B018

    def test_repeated_access_is_cached(self) -> None:
        self.assertIs(xiaoyu.Agent, xiaoyu.Agent)
        self.assertIn("Agent", vars(xiaoyu))


class TestDocContract(unittest.TestCase):
    """docs/embedding.md「公开面清单」与 `_EXPORTS` 双向一致。"""

    def _documented(self) -> set[str]:
        text = DOC.read_text(encoding="utf-8")
        start = text.index("## 公开面清单")
        section = text[start:]
        return set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", section))

    def test_doc_lists_every_export(self) -> None:
        documented = self._documented()
        missing = [name for name in xiaoyu.__all__ if name not in documented]
        self.assertEqual(missing, [], "导出表里有名字没写进 docs/embedding.md 公开面清单")

    def test_doc_does_not_promise_unexported_names(self) -> None:
        """清单分组行里出现的名字必须在表里——文档不许承诺代码没导出的东西。

        只看「- **分组**：」那些行；下面 Agent/AsyncAgent 的方法清单是对象成员，
        不是顶层导出，不在此校验。
        """
        text = DOC.read_text(encoding="utf-8")
        section = text[text.index("## 公开面清单"):]
        promised: set[str] = set()
        for line in section.splitlines():
            if line.startswith("- **"):
                promised.update(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", line))
            elif line.startswith("  `") or (line.startswith("  ") and promised):
                #  分组行的续行（缩进两格）
                promised.update(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", line))
            if line.startswith("`Agent` 上"):
                break
        extra = sorted(promised - set(xiaoyu.__all__))
        self.assertEqual(extra, [], "docs/embedding.md 承诺了导出表里没有的名字")


class TestLazyImport(unittest.TestCase):
    def test_import_xiaoyu_does_not_load_kernel(self) -> None:
        code = (
            "import sys, xiaoyu\n"
            "loaded = [m for m in ('xiaoyu.agent', 'xiaoyu.tools', 'openai', 'anthropic') if m in sys.modules]\n"
            "print(xiaoyu.__version__ + '|' + ','.join(loaded))\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, encoding="utf-8", check=True, cwd=ROOT
        )
        version, loaded = proc.stdout.strip().split("|", 1)
        self.assertEqual(version, xiaoyu.__version__)
        self.assertEqual(loaded, "", f"import xiaoyu 不该拖起：{loaded}")


if __name__ == "__main__":
    unittest.main()
