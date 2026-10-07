"""scripts/release.py：预检判定喂假事实（不打网络、不碰真实仓库的 tag）。"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("release_script", REPO / "scripts" / "release.py")
release = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = release  # dataclass 解析延迟注解时按模块名回查
_SPEC.loader.exec_module(release)


def facts(**overrides) -> "release.Facts":
    base = dict(
        version="0.67.0", branch="main", dirty=[], head="a" * 40, upstream_head="a" * 40,
        fetch_error=None, tags=["v0.65.0", "v0.66.0"], pypi_versions=["0.65.0", "0.66.0"], pypi_error=None,
        notes_exists=True, unreleased_hits=[],
        ci_run={"conclusion": "success", "status": "completed", "headSha": "a" * 40, "url": "u"},
    )
    base.update(overrides)
    return release.Facts(**base)


def statuses(checks) -> dict[str, str]:
    return {c.name: c.status for c in checks}


class VersionKeyTest(unittest.TestCase):
    def test_numeric_order_not_lexical(self):
        self.assertLess(release.version_key("0.9.0"), release.version_key("0.10.0"))
        self.assertEqual(release.version_key("v0.66.0"), (0, 66, 0))
        self.assertEqual(release.version_key("1.2.0rc1"), (1, 2))


class EvaluateTest(unittest.TestCase):
    def test_all_green(self):
        checks = release.evaluate(facts())
        self.assertEqual(set(statuses(checks).values()), {release.OK})
        self.assertEqual(release.blocked(checks), [])

    def test_branch_and_dirty_tree_block(self):
        got = statuses(release.evaluate(facts(branch="feat/x", dirty=[" M a.py"])))
        self.assertEqual(got["branch"], release.FAIL)
        self.assertEqual(got["clean"], release.FAIL)

    def test_unpushed_head_fails_but_only_warns_when_fetch_failed(self):
        self.assertEqual(statuses(release.evaluate(facts(upstream_head="b" * 40)))["upstream"], release.FAIL)
        got = statuses(release.evaluate(facts(upstream_head="b" * 40, fetch_error="没网")))
        self.assertEqual(got["upstream"], release.WARN)
        self.assertEqual(statuses(release.evaluate(facts(fetch_error="没网")))["upstream"], release.WARN)
        self.assertEqual(statuses(release.evaluate(facts(upstream_head=None)))["upstream"], release.WARN)

    def test_version_must_exceed_tags_and_pypi(self):
        got = statuses(release.evaluate(facts(version="0.66.0")))
        self.assertEqual(got["version_tags"], release.FAIL)
        self.assertEqual(got["version_pypi"], release.FAIL)
        self.assertEqual(got["tag_free"], release.FAIL)
        #  烧毁的版本号：PyPI 上有、本地 tag 已删——仍然不许复用
        got = statuses(release.evaluate(facts(version="0.66.0", tags=["v0.65.0"])))
        self.assertEqual(got["version_tags"], release.OK)
        self.assertEqual(got["version_pypi"], release.FAIL)
        #  PyPI 查不到只警告
        got = statuses(release.evaluate(facts(pypi_versions=None, pypi_error="超时")))
        self.assertEqual(got["version_pypi"], release.WARN)

    def test_notes_and_unreleased_marks(self):
        got = statuses(release.evaluate(facts(notes_exists=False)))
        self.assertEqual(got["notes"], release.FAIL)
        checks = release.evaluate(facts(unreleased_hits=["docs/x.md:3: 尚未发布"]))
        self.assertEqual(statuses(checks)["unreleased"], release.FAIL)
        self.assertIn("docs/x.md:3", release.render(checks))

    def test_ci_states(self):
        self.assertEqual(statuses(release.evaluate(facts(ci_run=None, ci_error="没有 gh")))["ci"], release.SKIP)
        red = {"conclusion": "failure", "status": "completed", "headSha": "a" * 40, "url": "u"}
        self.assertEqual(statuses(release.evaluate(facts(ci_run=red)))["ci"], release.FAIL)
        older = {"conclusion": "success", "status": "completed", "headSha": "c" * 40, "url": "u"}
        self.assertEqual(statuses(release.evaluate(facts(ci_run=older)))["ci"], release.WARN)


class ScanUnreleasedTest(unittest.TestCase):
    def test_exempts_history_and_internal(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / "docs" / "releases").mkdir(parents=True)
            (repo / "docs" / "internal").mkdir()
            (repo / "README.md").write_text("第一行\n功能 X 尚未发布\n", encoding="utf-8")
            (repo / "docs" / "guide.md").write_text("好\n", encoding="utf-8")
            (repo / "docs" / "releases" / "0.1.0.md").write_text("当时尚未发布\n", encoding="utf-8")
            (repo / "docs" / "sdk-validation.md").write_text("当时尚未发布\n", encoding="utf-8")
            (repo / "docs" / "internal" / "n.md").write_text("尚未发布\n", encoding="utf-8")
            self.assertEqual(release.scan_unreleased(repo), ["README.md:2: 功能 X 尚未发布"])


class TagTest(unittest.TestCase):
    def test_creates_annotated_tag_in_temp_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                   "GIT_COMMITTER_EMAIL": "t@x", "HOME": tmp, "GIT_CONFIG_GLOBAL": os.devnull}

            def git(*args):
                return subprocess.run(["git", "-C", tmp, *args], check=True, capture_output=True, text=True,
                                      encoding="utf-8", errors="replace", env=env)

            git("init", "-q", "-b", "main")
            (repo / "f").write_text("0", encoding="utf-8")
            git("add", "f")
            git("commit", "-q", "-m", "chore: 起点")
            tag = release.create_tag("0.1.0", repo=repo)
            self.assertEqual(tag, "v0.1.0")
            self.assertEqual(git("cat-file", "-t", "v0.1.0").stdout.strip(), "tag")  # 带注释的 tag 是独立对象


if __name__ == "__main__":
    unittest.main()
