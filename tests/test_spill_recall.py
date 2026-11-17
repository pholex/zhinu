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

    # ---------- 历史是接进来的：里面点过名的 id 不属于这个工具箱 ----------

    def _foreign_history(self) -> list[dict]:
        return [
            {"role": "user", "content": "跑测试"},
            {"role": "tool", "tool_call_id": "c1",
             "content": "[输出超长：原始 9 字符，完整内容已存，召回 id: 1。以下保留开头和结尾]\n旧输出"},
            {"role": "tool", "tool_call_id": "c2", "content": "… [中间省略 5 字符，完整内容见召回 id 3] …"},
        ]

    def test_adopted_ids_never_resolve_to_new_content(self):
        """接回的历史说"召回 id: 1"——这边新落的盘不能再编出个 1 来冒名顶替。"""
        self.box.adopt_history(self._foreign_history())
        preview = self._spill_big()
        self.assertIn("召回 id: 4", preview)  # 从历史里的最大号之后起编
        for stale in ("1", "3"):
            out = self.box.run("recall", {"id": stale})
            self.assertIn("带进来的历史", out)
            self.assertNotIn("NEEDLE", out)
        self.assertIn("NEEDLE", self.box.run("recall", {"id": "4"}))

    def test_adopting_drops_this_boxs_earlier_spills(self):
        """同一进程里先聊过一段、再接回别的会话：先前编的 1 也不能留着。"""
        self._spill_big()
        self.box.adopt_history(self._foreign_history())
        out = self.box.run("recall", {"id": "1"})
        self.assertIn("带进来的历史", out)
        self.assertNotIn("NEEDLE", out)

    def test_recall_stays_visible_after_adopting_history_with_ids(self):
        self.box.adopt_history(self._foreign_history())
        names = {schema["function"]["name"] for schema in self.box.schemas()}
        self.assertIn("recall", names)

    def test_adopting_history_without_ids_changes_nothing(self):
        self.box.adopt_history([{"role": "user", "content": "hi"}])
        names = {schema["function"]["name"] for schema in self.box.schemas()}
        self.assertNotIn("recall", names)
        self.assertIn("召回 id: 1", self._spill_big())


if __name__ == "__main__":
    unittest.main()
