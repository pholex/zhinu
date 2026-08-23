"""spill 召回（addressable recall）：超长输出落盘后给短 id，recall 工具按 id 取回中段。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xiaoyu.config import Config
from xiaoyu.tools import Toolbox


class SpillRecallTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.config = Config(base_url="x", model="m", workspace=Path(self.tmp.name).resolve(),
                             enable_plugins=False)
        self.config.max_tool_output = 500
        self.box = Toolbox(self.config)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _spill_big(self) -> str:
        #  中段藏一个只出现在中间的针，头尾预览都盖不到
        lines = [f"line {i}" for i in range(400)]
        lines[200] = "NEEDLE-在正中间"
        return self.box._bound_output("bash", "\n".join(lines))  # noqa: SLF001

    def test_preview_gives_recall_id_not_path(self):
        preview = self._spill_big()
        self.assertIn("召回 id: 1", preview)
        self.assertIn("recall(id=\"1\"", preview)
        #  头尾在、中段的针不在（正是要召回的部分）
        self.assertIn("line 0", preview)
        self.assertIn("line 399", preview)
        self.assertNotIn("NEEDLE", preview)

    def test_recall_available_only_after_spill(self):
        #  未落盘：recall 不进 schema
        names = [t["function"]["name"] for t in self.box.schemas()]
        self.assertNotIn("recall", names)
        self._spill_big()
        names = [t["function"]["name"] for t in self.box.schemas()]
        self.assertIn("recall", names)

    def test_recall_list(self):
        self._spill_big()
        out = self.box.run("recall", {})
        self.assertIn("id 1: bash", out)
        self.assertIn("行", out)

    def test_recall_by_pattern_finds_middle(self):
        self._spill_big()
        out = self.box.run("recall", {"id": "1", "pattern": "NEEDLE"})
        self.assertIn("201: NEEDLE-在正中间", out)  # 行号 1-based

    def test_recall_by_offset_limit(self):
        self._spill_big()
        out = self.box.run("recall", {"id": "1", "offset": 200, "limit": 3})
        self.assertIn("第 200-202 行", out)
        self.assertIn("NEEDLE", out)

    def test_recall_bad_id(self):
        self._spill_big()
        out = self.box.run("recall", {"id": "999"})
        self.assertIn("没有召回 id 999", out)
        self.assertIn("可用 id：1", out)

    def test_recall_id_only_returns_middle(self):
        self._spill_big()
        out = self.box.run("recall", {"id": "1"})
        self.assertIn("中段", out)
        self.assertIn("NEEDLE", out)

    def test_two_spills_get_distinct_ids(self):
        self._spill_big()
        self._spill_big()
        out = self.box.run("recall", {})
        self.assertIn("id 1:", out)
        self.assertIn("id 2:", out)

    def test_recall_grep_no_match(self):
        self._spill_big()
        out = self.box.run("recall", {"id": "1", "pattern": "ZZZ-不存在"})
        self.assertIn("没有匹配", out)

    def test_recall_missing_file_degrades(self):
        self._spill_big()
        #  临时目录被清理的情形
        import shutil
        shutil.rmtree(self.box._spills["1"]["path"].parent)  # noqa: SLF001
        out = self.box.run("recall", {"id": "1"})
        self.assertIn("已不可读", out)


if __name__ == "__main__":
    unittest.main()
