"""scripts/release.py：预检判定喂假事实（不打网络）；动手那段在临时仓库里跑。"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
_SPEC = importlib.util.spec_from_file_location("release_script", REPO / "scripts" / "release.py")
release = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
sys.modules[_SPEC.name] = release  # dataclass 解析延迟注解时按模块名回查
_SPEC.loader.exec_module(release)


def facts(**overrides) -> "release.Facts":
    base = dict(
        current="0.66.0", target="0.67.0", branch="main", dirty=[], head="a" * 40, upstream_head="a" * 40,
        fetch_error=None, tags=["v0.65.0", "v0.66.0"], last_tag="v0.66.0", change_count=3,
        pypi_versions=["0.65.0", "0.66.0"], pypi_error=None, unreleased_hits=[],
        ci_run={"conclusion": "success", "status": "completed", "headSha": "a" * 40, "url": "u"},
        self_test={"total": 18, "passed": 17, "skipped": 1, "items": [], "model": "m", "platform": "p",
                   "recorded_at": "2026-10-15T03:30:00", "source": {}},
        self_test_age=0,
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

    def test_bump(self):
        self.assertEqual(release.bumped("0.71.1", "patch"), "0.71.2")
        self.assertEqual(release.bumped("0.71.1", "minor"), "0.72.0")
        self.assertEqual(release.bumped("0.71.1", "major"), "1.0.0")


class EvaluateTest(unittest.TestCase):
    def test_all_green(self):
        checks = release.evaluate(facts())
        self.assertEqual(set(statuses(checks).values()), {release.OK})
        self.assertEqual(release.blocked(checks), [])

    def test_branch_and_dirty_tree_block(self):
        got = statuses(release.evaluate(facts(branch="feat/x", dirty=[" M a.py"])))
        self.assertEqual(got["branch"], release.FAIL)
        self.assertEqual(got["clean"], release.FAIL)

    def test_notes_start_from_last_published_tag_not_burned_one(self):
        # v0.66.0 打了 tag 但没发出去（烧号），PyPI 最高是 0.65.0：说明从 v0.65.0 起算
        got = facts(tags=["v0.65.0", "v0.66.0"], last_tag="v0.66.0", pypi_versions=["0.64.0", "0.65.0"])
        self.assertEqual(release.notes_since(got), "v0.65.0")
        # PyPI 查不到就退回最近的 tag；连 tag 都没有退回 HEAD~1
        self.assertEqual(release.notes_since(facts(pypi_versions=None)), "v0.66.0")
        self.assertEqual(release.notes_since(facts(pypi_versions=None, last_tag=None, tags=[])), "HEAD~1")

    def test_untracked_files_only_warn(self):
        got = statuses(release.evaluate(facts(dirty=["?? lab-results/"])))
        self.assertEqual(got["clean"], release.WARN)
        got = statuses(release.evaluate(facts(dirty=["?? lab-results/", " M a.py"])))
        self.assertEqual(got["clean"], release.FAIL)

    def test_unpushed_head_fails_but_only_warns_when_fetch_failed(self):
        self.assertEqual(statuses(release.evaluate(facts(upstream_head="b" * 40)))["upstream"], release.FAIL)
        got = statuses(release.evaluate(facts(upstream_head="b" * 40, fetch_error="没网")))
        self.assertEqual(got["upstream"], release.WARN)
        self.assertEqual(statuses(release.evaluate(facts(fetch_error="没网")))["upstream"], release.WARN)
        self.assertEqual(statuses(release.evaluate(facts(upstream_head=None)))["upstream"], release.WARN)

    def test_version_must_exceed_current_tags_and_pypi(self):
        got = statuses(release.evaluate(facts(target="0.66.0")))
        self.assertEqual(got["version_current"], release.FAIL)
        self.assertEqual(got["version_tags"], release.FAIL)
        self.assertEqual(got["version_pypi"], release.FAIL)
        self.assertEqual(got["tag_free"], release.FAIL)
        #  烧毁的版本号：PyPI 上有、本地 tag 已删——仍然不许复用
        got = statuses(release.evaluate(facts(target="0.66.0", current="0.65.0", tags=["v0.65.0"])))
        self.assertEqual(got["version_tags"], release.OK)
        self.assertEqual(got["version_pypi"], release.FAIL)
        #  PyPI 查不到只警告
        got = statuses(release.evaluate(facts(pypi_versions=None, pypi_error="超时")))
        self.assertEqual(got["version_pypi"], release.WARN)
        self.assertEqual(statuses(release.evaluate(facts(target="0.67")))["version_shape"], release.FAIL)

    def test_check_only_has_no_version_checks(self):
        got = statuses(release.evaluate(facts(target=None)))
        self.assertNotIn("version_current", got)
        self.assertNotIn("tag_free", got)
        self.assertEqual(got["branch"], release.OK)

    def test_empty_release_blocks(self):
        self.assertEqual(statuses(release.evaluate(facts(change_count=0)))["changes"], release.FAIL)
        self.assertEqual(statuses(release.evaluate(facts(change_count=None, last_tag=None)))["changes"], release.WARN)

    def test_unreleased_marks(self):
        checks = release.evaluate(facts(unreleased_hits=["docs/x.md:3: 尚未发布"]))
        self.assertEqual(statuses(checks)["unreleased"], release.FAIL)
        self.assertIn("docs/x.md:3", release.render(checks))

    def test_ci_states(self):
        self.assertEqual(statuses(release.evaluate(facts(ci_run=None, ci_error="没有 gh")))["ci"], release.SKIP)
        red = {"conclusion": "failure", "status": "completed", "headSha": "a" * 40, "url": "u"}
        self.assertEqual(statuses(release.evaluate(facts(ci_run=red)))["ci"], release.FAIL)
        running = {"conclusion": None, "status": "in_progress", "headSha": "a" * 40, "url": "u"}
        self.assertEqual(statuses(release.evaluate(facts(ci_run=running)))["ci"], release.WARN)
        older = {"conclusion": "success", "status": "completed", "headSha": "c" * 40, "url": "u"}
        self.assertEqual(statuses(release.evaluate(facts(ci_run=older)))["ci"], release.WARN)

    def test_self_test_only_warns(self):
        """nightly 自测不是门：缺、旧、低通过率都只 WARN。"""
        self.assertEqual(statuses(release.evaluate(facts(self_test=None, self_test_age=None)))["self_test"], release.WARN)
        self.assertEqual(statuses(release.evaluate(facts(self_test_age=9)))["self_test"], release.WARN)
        low = {"total": 10, "passed": 5, "skipped": 0, "items": [], "model": "", "platform": "", "recorded_at": "", "source": {}}
        checks = release.evaluate(facts(self_test=low))
        self.assertEqual(statuses(checks)["self_test"], release.WARN)
        self.assertEqual(release.blocked(checks), [])


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


class AcpPinTest(unittest.TestCase):
    def test_bumps_version_and_every_pinned_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.json"
            path.write_text(json.dumps({
                "id": "xiaoyu", "version": "0.70.0",
                "distribution": {"uvx": {"package": "xiaoyu-agent==0.70.0", "args": ["--acp"]},
                                 "npx": {"package": "something-without-pin"}},
            }), encoding="utf-8")
            self.assertTrue(release.bump_acp_pin("0.71.0", path))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["version"], "0.71.0")
            self.assertEqual(data["distribution"]["uvx"]["package"], "xiaoyu-agent==0.71.0")
            self.assertEqual(data["distribution"]["npx"]["package"], "something-without-pin")
            self.assertFalse(release.bump_acp_pin("0.71.0", path))  # 已经是，不再改
            self.assertFalse(release.bump_acp_pin("0.71.0", Path(tmp) / "missing.json"))


class TempRepoTest(unittest.TestCase):
    """临时仓库：改版本号 + 发版说明 + ACP 副本 + 提交 + 带注释的 tag，一条龙。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                    "GIT_COMMITTER_EMAIL": "t@x", "HOME": self.tmp.name, "GIT_CONFIG_GLOBAL": os.devnull,
                    #  自测记录目录指到临时处：不读开发机上真实的 nightly 记录
                    "XDG_CONFIG_HOME": self.tmp.name, "APPDATA": self.tmp.name}
        self.git("init", "-q", "-b", "main")
        #  prepare_release 自己调 git、不带上面那份 env：身份写进仓库本地配置，
        #  CI runner 没有全局身份也打得出带注释的 tag
        self.git("config", "user.name", "t")
        self.git("config", "user.email", "t@x")
        (self.repo / "xiaoyu").mkdir()
        (self.repo / "xiaoyu" / "__init__.py").write_text('__version__ = "0.1.0"\n', encoding="utf-8")
        (self.repo / "docs" / "acp-registry").mkdir(parents=True)
        (self.repo / "docs" / "acp-registry" / "agent.json").write_text(json.dumps(
            {"version": "0.1.0", "distribution": {"uvx": {"package": "xiaoyu-agent==0.1.0"}}}), encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "chore: 起点")
        self.git("tag", "-a", "v0.1.0", "-m", "0.1.0")
        (self.repo / "f").write_text("1", encoding="utf-8")
        self.git("add", "f")
        self.git("commit", "-q", "-m", "fix(cli): 修一个坑\n\n正文。\n\n行为变化: 默认不再自动重试")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=self.env)

    def test_prepare_release_commits_and_tags(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name, "APPDATA": self.tmp.name}), \
                redirect_stdout(io.StringIO()):
            done = release.prepare_release("0.2.0", "v0.1.0", repo=self.repo, validation="auto")
        self.assertEqual(done["tag"], "v0.2.0")
        self.assertEqual(self.git("cat-file", "-t", "v0.2.0").stdout.strip(), "tag")  # 带注释的 tag 是独立对象
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), "chore(release): 0.2.0")
        self.assertEqual(self.git("status", "--porcelain").stdout.strip(), "")
        self.assertIn('__version__ = "0.2.0"', (self.repo / "xiaoyu" / "__init__.py").read_text(encoding="utf-8"))
        notes = (self.repo / "docs" / "releases" / "0.2.0.md").read_text(encoding="utf-8")
        self.assertIn("# 0.2.0（", notes)
        self.assertIn("## 行为变化（升级前看一眼）\n\n- 默认不再自动重试（", notes)
        self.assertIn("- 修复：修一个坑（", notes)  # scope（cli）不进列表行
        self.assertIn("- 未附自测记录", notes)
        pin = json.loads((self.repo / "docs" / "acp-registry" / "agent.json").read_text(encoding="utf-8"))
        self.assertEqual(pin["distribution"]["uvx"]["package"], "xiaoyu-agent==0.2.0")
        self.assertEqual(sorted(done["staged"]),
                         ["docs/acp-registry/agent.json", "docs/releases/0.2.0.md", "xiaoyu/__init__.py"])

    def test_collect_facts_offline(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name, "APPDATA": self.tmp.name}), \
                mock.patch.object(release.shutil, "which", lambda name: None):
            got = release.collect_facts("0.2.0", repo=self.repo, fetch=False, network=False)
        self.assertEqual((got.current, got.target, got.branch, got.last_tag, got.change_count), ("0.1.0", "0.2.0", "main", "v0.1.0", 1))
        self.assertEqual(got.tags, ["v0.1.0"])
        self.assertIsNone(got.pypi_versions)
        self.assertIsNone(got.self_test)
        self.assertEqual(got.ci_error, "没有 gh")


if __name__ == "__main__":
    unittest.main()
