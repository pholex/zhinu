"""技能使用账本 + 使用度排序的测试。不打网络。"""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import skill_usage
from xiaoyu.skills import Skill


class SkillUsageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        #  账本落进临时配置目录，且清进程内缓存做隔离
        self._patch = mock.patch("xiaoyu.skill_usage.user_config_dir", return_value=self.root)
        self._patch.start()
        skill_usage._reset_for_test()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(skill_usage._reset_for_test)

    def _skill(self, name: str) -> Skill:
        #  给每个技能一个真实 SKILL.md，mtime 排序要 stat 得到
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        md = d / "SKILL.md"
        md.write_text(f"# {name}", encoding="utf-8")
        return Skill(name=name, description=f"desc {name}", path=md)

    def test_record_and_count(self) -> None:
        skill_usage.record_load("a")
        skill_usage.record_load("a")
        skill_usage.record_load("b")
        c = skill_usage.counts()
        self.assertEqual(c["a"]["hits"], 2)
        self.assertEqual(c["b"]["hits"], 1)

    def test_flush_persists_and_reloads(self) -> None:
        skill_usage.record_load("a")
        skill_usage.flush()
        skill_usage._reset_for_test()  # 丢进程内缓存，强制从盘重读
        self.assertEqual(skill_usage.counts()["a"]["hits"], 1)

    def test_corrupt_ledger_treated_as_empty(self) -> None:
        (self.root / skill_usage._USAGE_FILE).write_text("{not json", encoding="utf-8")
        skill_usage._reset_for_test()
        self.assertEqual(skill_usage.counts(), {})

    def test_ranked_by_hits_desc(self) -> None:
        a, b, cc = self._skill("a"), self._skill("b"), self._skill("c")
        skill_usage.record_load("b")
        skill_usage.record_load("b")
        skill_usage.record_load("c")
        ordered = [s.name for s in skill_usage.ranked([a, b, cc])]
        #  b(2) > c(1) > a(0)
        self.assertEqual(ordered, ["b", "c", "a"])

    def test_unused_ranked_by_mtime_newest_first(self) -> None:
        old = self._skill("old")
        #  把 old 的 mtime 往回拨，new 保持现在
        import os
        past = time.time() - 10_000
        os.utime(old.path, (past, past))
        new = self._skill("new")
        ordered = [s.name for s in skill_usage.ranked([old, new])]
        #  都没用过 → 新的（mtime 大）在前，不让新技能被老而没用过的压底
        self.assertEqual(ordered, ["new", "old"])

    def test_ranked_is_stable_for_equal_keys(self) -> None:
        #  同为未用过、mtime 相同 → 保持输入相对顺序（prefix cache 不抖）
        a, b = self._skill("a"), self._skill("b")
        same = time.time()
        import os
        os.utime(a.path, (same, same))
        os.utime(b.path, (same, same))
        self.assertEqual([s.name for s in skill_usage.ranked([a, b])], ["a", "b"])
        self.assertEqual([s.name for s in skill_usage.ranked([b, a])], ["b", "a"])

    def test_max_entries_trimmed_by_recency(self) -> None:
        with mock.patch.object(skill_usage, "_MAX_ENTRIES", 3):
            for i in range(5):
                skill_usage.record_load(f"s{i}")
                time.sleep(0.001)
            skill_usage.flush()
            skill_usage._reset_for_test()
            kept = set(skill_usage.counts())
            #  只留最近的 3 个
            self.assertEqual(len(kept), 3)
            self.assertIn("s4", kept)
            self.assertNotIn("s0", kept)


class ImplicitLoadTest(SkillUsageTest):
    """bash 直读 SKILL.md / 跑技能脚本 → 归因到技能。"""

    def test_cat_skill_md_and_script(self) -> None:
        a, b = self._skill("a"), self._skill("b")
        (self.root / "a" / "scripts").mkdir()
        cmd = f"cat {a.path} && python3 {self.root}/a/scripts/run.py; ls /tmp"
        self.assertEqual(skill_usage.implicit_loads(cmd, [a, b]), ["a"])

    def test_relative_and_tilde(self) -> None:
        a = self._skill("a")
        self.assertEqual(
            skill_usage.implicit_loads("sed -n 1,40p a/SKILL.md", [a], cwd=self.root), ["a"]
        )
        with mock.patch.dict("os.environ", {"HOME": str(self.root)}):
            self.assertEqual(skill_usage.implicit_loads("head ~/a/SKILL.md", [a]), ["a"])
            self.assertEqual(skill_usage.implicit_loads("cat $HOME/a/references/x.md", [a]), ["a"])

    def test_no_match_and_bad_quotes(self) -> None:
        a = self._skill("a")
        self.assertEqual(skill_usage.implicit_loads("cat /etc/hosts", [a]), [])
        self.assertEqual(skill_usage.implicit_loads("echo 'unterminated", [a]), [])
        self.assertEqual(skill_usage.implicit_loads("ls", []), [])
        #  同名前缀目录不算（a 与 ab）
        ab = self._skill("ab")
        self.assertEqual(skill_usage.implicit_loads(f"cat {ab.path}", [a, ab]), ["ab"])


class IndexRankingIntegrationTest(unittest.TestCase):
    """index_block(rank_by_usage=True) 端到端：预算够时全列但顺序按使用度。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._patch = mock.patch("xiaoyu.skill_usage.user_config_dir", return_value=self.root)
        self._patch.start()
        skill_usage._reset_for_test()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(skill_usage._reset_for_test)

    def _skill(self, name: str) -> Skill:
        d = self.root / name
        d.mkdir(parents=True, exist_ok=True)
        md = d / "SKILL.md"
        md.write_text(f"# {name}", encoding="utf-8")
        return Skill(name=name, description=f"desc {name}", path=md)

    def test_used_skill_listed_first(self) -> None:
        from xiaoyu import skills as skills_mod

        a, b = self._skill("alpha"), self._skill("beta")
        skill_usage.record_load("beta")
        block = skills_mod.index_block([a, b], rank_by_usage=True)
        self.assertLess(block.index("beta"), block.index("alpha"), block)

    def test_no_rank_keeps_source_order(self) -> None:
        from xiaoyu import skills as skills_mod

        a, b = self._skill("alpha"), self._skill("beta")
        skill_usage.record_load("beta")
        block = skills_mod.index_block([a, b], rank_by_usage=False)
        self.assertLess(block.index("alpha"), block.index("beta"), block)


if __name__ == "__main__":
    unittest.main()
