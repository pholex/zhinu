"""技能负例（when_not）+ 触发准确率机制的自证测试——纯本地、不打网络。"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import skills
from xiaoyu.evals import cases as eval_cases
from xiaoyu.evals.harness import Context, loaded_skill, no_skill_loaded


def _skill(name="s", desc="d", when_not="", **kw):
    return skills.Skill(name=name, description=desc, path=Path("/x"), when_not=when_not, **kw)


class WhenNotParseTest(unittest.TestCase):
    def test_when_not_is_allowed_and_parsed(self):
        self.assertIn("when_not", skills.FRONTMATTER_KEYS)
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "pdf"
            d.mkdir()
            (d / "SKILL.md").write_text(
                "---\nname: pdf\ndescription: 生成 PDF\nwhen_not: 纯文本或 Markdown\n---\n正文",
                encoding="utf-8",
            )
            with mock.patch.dict(os.environ, {"XIAOYU_SKILLS_DIR": tmp}):
                found = {s.name: s for s in skills.scan_skills()}
        self.assertEqual(found["pdf"].when_not, "纯文本或 Markdown")

    def test_when_not_absent_defaults_empty(self):
        self.assertEqual(_skill().when_not, "")


class WhenNotRenderTest(unittest.TestCase):
    def test_index_includes_when_not(self):
        block = skills.index_block([_skill("pdf", "生成 PDF", "纯文本导出")], max_tokens=2000)
        self.assertIn("- pdf: 生成 PDF（别用于：纯文本导出）", block)

    def test_when_not_capped(self):
        block = skills.index_block([_skill("s", "d", "x" * 500)], max_tokens=4000)
        self.assertIn("别用于：" + "x" * skills.WHEN_NOT_CAP + "）", block)
        self.assertNotIn("x" * (skills.WHEN_NOT_CAP + 1), block)

    def test_when_not_dropped_first_under_budget(self):
        """预算紧张时负例（描述尾部）先被截，技能名和描述主体保住。"""
        sk = [_skill(f"skill{i}", "一句挺长的技能描述用来占预算" * 2, "这是负例应当先被截掉") for i in range(6)]
        tight = skills.index_block(sk, max_tokens=200)
        # 名字都在
        for i in range(6):
            self.assertIn(f"skill{i}", tight)
        # 负例基本被挤掉（waterfill 保前缀，"别用于"在尾部）
        self.assertNotIn("这是负例应当先被截掉", tight)


class SkillsDirOverrideTest(unittest.TestCase):
    def test_override_replaces_default_dirs(self):
        with mock.patch.dict(os.environ, {"XIAOYU_SKILLS_DIR": "/a" + os.pathsep + "/b"}):
            dirs = skills.skill_dirs()
        self.assertEqual([str(d) for d in dirs], ["/a", "/b"])

    def test_no_override_uses_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("XIAOYU_SKILLS_DIR", None)
            dirs = skills.skill_dirs()
        self.assertTrue(any("skills" in str(d) for d in dirs))


class TriggerCheckTest(unittest.TestCase):
    def _ctx(self, *skill_names: str) -> Context:
        trace = [{"tool": "skill", "args": {"name": n}, "ok": True, "output": ""} for n in skill_names]
        return Context(workspace=Path("/x"), before={}, trace=trace, transcript="")

    def test_skills_loaded_dedupes_and_orders(self):
        self.assertEqual(self._ctx("a", "b", "a").skills_loaded(), ["a", "b"])

    def test_loaded_skill_positive(self):
        ok, _ = loaded_skill("pdf-export")(self._ctx("pdf-export"))
        self.assertTrue(ok)
        wrong, _ = loaded_skill("pdf-export")(self._ctx("sql-tuning"))
        self.assertFalse(wrong)

    def test_no_skill_loaded_negative(self):
        ok, _ = no_skill_loaded()(self._ctx())
        self.assertTrue(ok)
        bad, _ = no_skill_loaded()(self._ctx("pdf-export"))
        self.assertFalse(bad)

    def test_non_skill_tools_ignored(self):
        ctx = Context(workspace=Path("/x"), before={},
                      trace=[{"tool": "read_file", "args": {"path": "x"}, "ok": True, "output": ""}],
                      transcript="")
        self.assertEqual(ctx.skills_loaded(), [])


class TriggerCaseSetTest(unittest.TestCase):
    def test_ten_cases_balanced_and_isolated(self):
        self.assertEqual(len(eval_cases.TRIGGER_POSITIVES), 5)
        self.assertEqual(len(eval_cases.TRIGGER_NEGATIVES), 5)
        for case in eval_cases.TRIGGER_CASES:
            self.assertTrue(case.enable_skills)
            self.assertTrue(case.skills)  # 自带隔离技能
        #  隔离技能里带 when_not（负例字段真的用上了）
        pdf = eval_cases._TRIGGER_SKILLS["pdf-export"]
        self.assertIn("when_not:", pdf)

    def test_trigger_cases_in_master_list(self):
        names = {c.name for c in eval_cases.CASES}
        self.assertTrue({"trig_pos_pdf", "trig_neg_sql"} <= names)


if __name__ == "__main__":
    unittest.main()
