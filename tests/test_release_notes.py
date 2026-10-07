"""scripts/release_notes.py 的纯函数与退化路径（不调模型、不打网络）。"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("release_notes", REPO / "scripts" / "release_notes.py")
release_notes = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(release_notes)


class ParseSubjectTest(unittest.TestCase):
    def test_conventional_prefixes(self):
        self.assertEqual(release_notes.parse_subject("feat(serve): 新端点"), ("feat", "serve：新端点"))
        self.assertEqual(release_notes.parse_subject("fix: 修了"), ("fix", "修了"))
        self.assertEqual(release_notes.parse_subject("docs(ci): 补一句"), ("docs", "ci：补一句"))
        self.assertEqual(release_notes.parse_subject("chore(release): 0.61.0"), ("chore", "release：0.61.0"))

    def test_breaking_marker_and_unknown_types(self):
        self.assertEqual(release_notes.parse_subject("feat!: 破坏性"), ("feat", "破坏性"))
        self.assertEqual(release_notes.parse_subject("refactor(x): 整理"), ("other", "x：整理"))
        self.assertEqual(release_notes.parse_subject("没有前缀的提交"), ("other", "没有前缀的提交"))
        #  冒号后没空格 / 大写类型都不算 conventional：原样进其它
        self.assertEqual(release_notes.parse_subject("Feat: 大写"), ("other", "Feat: 大写"))


class RenderTest(unittest.TestCase):
    def test_groups_in_fixed_order_and_skips_empty(self):
        commits = [
            {"sha": "a" * 40, "subject": "fix: 二", "body": ""},
            {"sha": "b" * 40, "subject": "feat(tui): 一", "body": "第一段\n两行\n\n第二段不要"},
            {"sha": "c" * 40, "subject": "test: 三", "body": ""},
        ]
        text = release_notes.render_plain(release_notes.group_commits(commits))
        self.assertEqual(
            text,
            "## 新功能\n\n- tui：一（bbbbbbb）\n  第一段\n  两行\n\n"
            "## 修复\n\n- 二（aaaaaaa）\n\n"
            "## 其它\n\n- 三（ccccccc）\n",
        )
        self.assertNotIn("文档", text)
        self.assertNotIn("第二段", text)

    def test_document_header(self):
        doc = release_notes.render_document("0.61.0", "v0.60.0", "## 修复\n\n- x\n", today=dt.date(2026, 10, 2))
        self.assertTrue(doc.startswith("# 0.61.0（2026-10-02）\n\n自 v0.60.0 以来的变化。\n\n## 修复"))

    def test_document_ends_with_validation_section_even_without_record(self):
        doc = release_notes.render_document("0.61.0", "v0.60.0", "## 修复\n\n- x\n", today=dt.date(2026, 10, 2))
        self.assertTrue(doc.endswith("## 修复\n\n- x\n\n## 本版验证\n\n- 未附自测记录\n"), doc)


class ValidationSectionTest(unittest.TestCase):
    """「本版验证」：吃 self_test.md 的收尾对象，缺文件 / 形态不对都退化为「未附自测记录」。"""

    RECORD = {
        "result": "done", "model": "deepseek-flash", "platform": "Darwin arm64",
        "output": {
            "total": 18, "passed": 16, "skipped": 1, "rate": 16 / 17,
            "items": [
                {"id": "P1-1", "status": "pass", "evidence": "工具返回成功"},
                {"id": "P5-1", "status": "skip", "evidence": "无 mcp.json"},
                {"id": "P6-3", "status": "fail", "evidence": "输出含 ESCAPED"},
            ],
        },
    }

    def _write(self, data) -> Path:
        self.tmp = tempfile.TemporaryDirectory(prefix="release-validation-")
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / "self_test.json"
        path.write_text(data if isinstance(data, str) else json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def test_loads_result_object_and_bare_listing(self):
        record = release_notes.load_validation(self._write(self.RECORD))
        self.assertEqual((record["model"], record["platform"], record["total"], record["passed"], record["skipped"]),
                         ("deepseek-flash", "Darwin arm64", 18, 16, 1))
        bare = release_notes.load_validation(self._write(self.RECORD["output"]))
        self.assertEqual((bare["model"], bare["total"], len(bare["items"])), ("", 18, 3))

    def test_missing_or_malformed_record_is_none(self):
        self.assertIsNone(release_notes.load_validation(None))
        self.assertIsNone(release_notes.load_validation(Path(tempfile.gettempdir()) / "no-such-self-test.json"))
        self.assertIsNone(release_notes.load_validation(self._write("not json")))
        self.assertIsNone(release_notes.load_validation(self._write({"output": {"total": "18"}})))
        self.assertIsNone(release_notes.load_validation(self._write([1, 2])))

    def test_section_lists_counts_failures_and_skips(self):
        text = release_notes.render_validation(release_notes.load_validation(self._write(self.RECORD)))
        self.assertEqual(
            text,
            "## 本版验证\n\n- 模型：deepseek-flash\n- 平台：Darwin arm64\n"
            "- 第一人称自测（tests_ai/self_test.md）：16/17 项通过，1 项未跑\n"
            "- 未通过：P6-3（输出含 ESCAPED）\n- 未跑：P5-1（无 mcp.json）\n",
        )

    def test_section_without_record(self):
        self.assertEqual(release_notes.render_validation(None), "## 本版验证\n\n- 未附自测记录\n")
        text = release_notes.render_validation(release_notes.load_validation(self._write(
            {"output": {"total": 3, "passed": 3, "skipped": 0, "items": []}})))
        self.assertIn("- 模型：未记录\n- 平台：未记录\n", text)
        self.assertIn("3/3 项通过\n", text)
        self.assertNotIn("未通过", text)


class GitAndFallbackTest(unittest.TestCase):
    """临时仓库：两个 tag 之间的非 merge 提交；模型不可用时退化为分组列表。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="release-notes-")
        self.repo = Path(self.tmp.name)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
               "GIT_COMMITTER_EMAIL": "t@x", "HOME": self.tmp.name, "GIT_CONFIG_GLOBAL": os.devnull}

        def git(*args):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, env=env)

        git("init", "-q", "-b", "main")
        (self.repo / "f").write_text("0", encoding="utf-8")
        git("add", "f")
        git("commit", "-q", "-m", "chore: 起点")
        git("tag", "v0.1.0")
        (self.repo / "f").write_text("1", encoding="utf-8")
        git("commit", "-q", "-am", "feat(cli): 新开关\n\n正文第一段。")
        (self.repo / "f").write_text("2", encoding="utf-8")
        git("commit", "-q", "-am", "fix: 修一个坑")

    def tearDown(self):
        self.tmp.cleanup()

    def test_collect_between_tag_and_head(self):
        commits = release_notes.collect_commits("v0.1.0", repo=self.repo)
        self.assertEqual([c["subject"] for c in commits], ["feat(cli): 新开关", "fix: 修一个坑"])
        self.assertEqual(commits[0]["body"], "正文第一段。")
        self.assertEqual(len(commits[0]["sha"]), 40)

    def test_polish_fails_open_to_plain_list(self):
        #  把 xiaoyu 换成一个必失败的解释器调用：polish 必须返回 None 而不是抛
        real = sys.executable
        try:
            sys.executable = "/nonexistent/python"
            self.assertIsNone(release_notes.polish("## 修复\n\n- x\n", None, timeout=5))
        finally:
            sys.executable = real


if __name__ == "__main__":
    unittest.main()
