"""宸枢（编排总控模式）的测试。不打网络。

scope 语义 / init 与收养 / plan 校验 / comms 过滤 / merge 五道闸 /
写守卫 / worker 线程端到端（假 client 脚本），各卡一处。
git 级用例需要 git，机器上没有就跳过。
"""

from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import worktree as worktree_mod
from xiaoyu.agent import Usage
from xiaoyu.chenshu import (
    ChenshuError,
    ChenshuRuntime,
    _write_guard,
    scope_match,
    scopes_conflict,
)
from xiaoyu.providers import Registry
from xiaoyu.render import PlainSink
from xiaoyu.sandbox import _worktree_git_paths

from .test_agent_paths import AgentTestCase, FakeClient, call_fragment, chunk

HAS_GIT = shutil.which("git") is not None
LONG = "交接：" + "详" * 210


def text_turn(text: str) -> list:
    return [chunk(content=text)]


def tool_turn(call_id: str, name: str, args: dict) -> list:
    return [chunk(tool_calls=[call_fragment(0, call_id, name, json.dumps(args))])]


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=True,
    )
    return result.stdout.strip()


class ScopeSemanticsTest(unittest.TestCase):
    def test_conflict_lattice(self):
        self.assertTrue(scopes_conflict("src/**", "src/api/"))
        self.assertTrue(scopes_conflict("src", "src/**"))
        self.assertFalse(scopes_conflict("src/api/", "src/web/"))
        self.assertFalse(scopes_conflict("docs/", "src/"))
        #  空干 = 整仓，保守判冲突
        self.assertTrue(scopes_conflict("**", "src/"))

    def test_nontrailing_glob_conflicts_by_stem(self):
        """非尾随通配符（src/**/*.py）的根干是 src——与 src/ 下任何 scope 冲突。
        评审抓出的双向失效回归：plan 侧不许放过重叠。"""
        self.assertTrue(scopes_conflict("src/**/*.py", "src/utils/"))
        self.assertTrue(scopes_conflict("src/a*", "src/abc/"))
        #  顶层裸 glob 根干为空 = 整仓 → 冲突
        self.assertTrue(scopes_conflict("*.py", "docs/"))

    def test_scope_match_forms(self):
        patterns = ("src/api/", "docs/*.md", "Makefile")
        self.assertTrue(scope_match("src/api/a.py", patterns))
        self.assertTrue(scope_match("docs/readme.md", patterns))
        self.assertTrue(scope_match("Makefile", patterns))
        self.assertFalse(scope_match("src/web/b.py", patterns))
        self.assertFalse(scope_match("other.txt", patterns))

    def test_nontrailing_glob_matches_by_stem(self):
        """merge 侧与 plan 侧同一个根干：src/**/*.py 保留的是 src 子树，
        src/x.py 必须命中（评审抓出的误拒回归）。"""
        self.assertTrue(scope_match("src/x.py", ("src/**/*.py",)))
        self.assertTrue(scope_match("src/deep/y.py", ("src/**/*.py",)))
        #  docs/*.md 的根干是 docs：整个 docs 子树都算它的（与 plan 保留一致）
        self.assertTrue(scope_match("docs/sub/x.md", ("docs/*.md",)))


class ChenshuCase(AgentTestCase):
    """公共装配：真 git 仓 + runtime（worktree 落在 tmp 配置目录）。"""

    def setUp(self):
        super().setUp()
        self.config.sandbox = False  # 测试环境不套 Seatbelt，行为确定
        self.notices: list[tuple[str, str]] = []
        self.patches = [
            mock.patch.object(worktree_mod, "user_config_dir", lambda: self.root / "cfg"),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def make_runtime(self, script: list | None = None) -> ChenshuRuntime:
        client = FakeClient(script or [])
        runtime = ChenshuRuntime(
            self.config,
            Registry.for_client(client),
            Usage(),
            PlainSink(indent="", verbose=False),
            None,
            notify=lambda text, key="": self.notices.append((text, key)),
        )
        return runtime

    def init_repo(self):
        git(self.root, "init", "-q", "-b", "main")
        #  仓库级 git 身份：CI runner 没有全局身份，worker 的 bash commit 与
        #  runtime.merge 的 merge commit 都要用（worktree 共享仓库级配置）
        git(self.root, "config", "user.email", "t@t")
        git(self.root, "config", "user.name", "t")
        (self.root / "src").mkdir(exist_ok=True)
        (self.root / "src" / "a.py").write_text("a = 1\n", encoding="utf-8")
        git(self.root, "add", "-A")
        git(self.root, "commit", "-qm", "init")


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class LifecycleTest(ChenshuCase):
    def test_init_requires_git_and_commit(self):
        runtime = self.make_runtime()
        with self.assertRaises(ChenshuError):
            runtime.init()  # 不是 git 仓库
        git(self.root, "init", "-q", "-b", "main")
        with self.assertRaises(ChenshuError):
            runtime.init()  # 没有 commit

    def test_init_creates_state_and_exclude(self):
        self.init_repo()
        runtime = self.make_runtime()
        out = runtime.init()
        self.assertIn("宸枢已启动", out)
        self.assertTrue((self.root / ".xiaoyu" / "chenshu" / "state.json").is_file())
        exclude = (self.root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
        self.assertIn(".xiaoyu/chenshu/", exclude)

    def test_reinit_adopts_and_retires(self):
        self.init_repo()
        first = self.make_runtime()
        first.init()
        first.plan([{"title": "改 API", "scope": ["src/"]}])
        with first.lock:
            first.members.append(
                __import__("xiaoyu.chenshu", fromlist=["Member"]).Member(
                    name="w1", kind="worker", mission_id="M1", status="running"
                )
            )
            first._save()
        second = self.make_runtime()
        out = second.init()
        self.assertIn("退役", out)
        self.assertEqual(second.member("w1").status, "retired")
        self.assertEqual(len(second.missions), 1)

    def test_plan_validations(self):
        self.init_repo()
        runtime = self.make_runtime()
        runtime.init()
        with self.assertRaises(ChenshuError):
            runtime.plan([{"title": "无 scope"}])  # build 缺 scope
        with self.assertRaises(ChenshuError):
            runtime.plan([{"title": "整仓", "scope": ["**"]}])
        runtime.plan([{"title": "改 API", "scope": ["src/api/"]}])
        with self.assertRaises(ChenshuError):
            runtime.plan([{"title": "又改 API", "scope": ["src/api/inner/"]}])  # 重叠
        with self.assertRaises(ChenshuError):
            runtime.plan([{"title": "依赖不存在", "scope": ["docs/"], "deps": ["M99"]}])
        out = runtime.plan([{"title": "调研", "kind": "survey"},
                            {"title": "改文档", "scope": ["docs/"], "deps": ["M1"]}])
        self.assertIn("M2", out)
        self.assertIn("M3", out)


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class CommsTest(ChenshuCase):
    def setUp(self):
        super().setUp()
        self.init_repo()
        self.runtime = self.make_runtime()
        self.runtime.init()
        from xiaoyu.chenshu import Member

        with self.runtime.lock:
            self.runtime.members += [
                Member(name="w1", kind="worker"), Member(name="w2", kind="worker"),
            ]

    def test_send_validates_recipient(self):
        with self.assertRaises(ChenshuError):
            self.runtime.send("w1", "nobody", "s", "b")
        with self.assertRaises(ChenshuError):
            self.runtime.send("w1", "w1", "s", "b")  # 不能发给自己

    def test_read_filters_by_caller(self):
        self.runtime.send("w1", "w2", "点对点", "只有 w2 能看")
        self.runtime.send("w2", "all", "广播", "所有人可见")
        self.runtime.send("w1", "chenshu", "升级", "给塔")
        w2_view = self.runtime.read_inbox("w2")
        self.assertIn("点对点", w2_view)
        self.assertIn("广播", w2_view)
        self.assertNotIn("升级", w2_view)
        w1_view = self.runtime.read_inbox("w1")
        self.assertNotIn("点对点", w1_view)
        tower_view = self.runtime.read_inbox("chenshu")
        self.assertIn("点对点", tower_view)
        self.assertIn("升级", tower_view)

    def test_finding_kinds(self):
        with self.assertRaises(ChenshuError):
            self.runtime.file_finding("w1", "typo", "t", "b")
        out = self.runtime.file_finding("w1", "bug", "空指针", "位置与复现")
        self.assertIn("归档", out)
        files = list((self.runtime.root / "findings").glob("*.md"))
        self.assertEqual(len(files), 1)


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class MergeGateTest(ChenshuCase):
    """merge 五道闸逐个卡（真 git 仓，分支上真提交）。"""

    def setUp(self):
        super().setUp()
        self.init_repo()
        self.runtime = self.make_runtime()
        self.runtime.init()
        self.runtime.plan([{"title": "改 src", "scope": ["src/"]}])
        self.mission = self.runtime.missions[0]

    def commit_on_branch(self, filename: str, base: str = "main") -> Path:
        """在 mission 分支的 worktree 里做一笔提交，返回 worktree 路径。"""
        if self.mission.worktree and Path(self.mission.worktree).is_dir():
            wt = Path(self.mission.worktree)
        else:
            wt = worktree_mod.create_branch(self.root, self.mission.branch, "t", base)
            self.mission.worktree = str(wt)
        target = wt / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n", encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", f"add {filename}")
        return wt

    def test_no_review_blocks(self):
        self.commit_on_branch("src/new.py")
        with self.assertRaises(ChenshuError) as ctx:
            self.runtime.merge("M1")
        self.assertIn("评审", str(ctx.exception))

    def test_clean_review_merges_and_conflict_scan(self):
        self.commit_on_branch("src/new.py")
        self.runtime.submit_review("chenshu", "M1", "clean", "看过了")
        out = self.runtime.merge("M1")
        self.assertIn("已合并", out)
        self.assertTrue((self.root / "src" / "new.py").is_file())
        self.assertEqual(self.runtime.missions[0].status, "merged")

    def test_not_clean_blocks(self):
        self.commit_on_branch("src/new.py")
        self.runtime.submit_review("chenshu", "M1", "p1-2", "两个必须修")
        with self.assertRaises(ChenshuError) as ctx:
            self.runtime.merge("M1")
        self.assertIn("p1-2", str(ctx.exception))

    def test_tip_moved_invalidates_clean(self):
        self.commit_on_branch("src/new.py")
        self.runtime.submit_review("chenshu", "M1", "clean", "看过了")
        self.commit_on_branch("src/more.py")  # 评审后又提交
        with self.assertRaises(ChenshuError) as ctx:
            self.runtime.merge("M1")
        self.assertIn("tip", str(ctx.exception).lower() + "不一致")

    def test_out_of_scope_blocks(self):
        self.commit_on_branch("docs/outside.md")
        self.runtime.submit_review("chenshu", "M1", "clean", "看过了")
        with self.assertRaises(ChenshuError) as ctx:
            self.runtime.merge("M1")
        self.assertIn("scope", str(ctx.exception))

    def test_deps_unmerged_blocks(self):
        self.runtime.plan([{"title": "后置", "scope": ["docs/"], "deps": ["M1"]}])
        with self.assertRaises(ChenshuError) as ctx:
            self.runtime.merge("M2")
        self.assertIn("M1", str(ctx.exception))

    def test_base_mismatch_blocks(self):
        self.commit_on_branch("src/new.py")
        self.runtime.submit_review("chenshu", "M1", "clean", "看过了")
        git(self.root, "checkout", "-q", "-b", "elsewhere")
        try:
            with self.assertRaises(ChenshuError) as ctx:
                self.runtime.merge("M1")
            self.assertIn("base", str(ctx.exception))
        finally:
            git(self.root, "checkout", "-q", "main")

    def test_survey_merges_without_git(self):
        self.runtime.plan([{"title": "摸底", "kind": "survey"}])
        out = self.runtime.merge("M2")
        self.assertIn("survey", out)
        self.assertEqual(self.runtime.mission("M2").status, "merged")

    def test_empty_diff_blocks(self):
        """分支零改动（worker 没 commit 的典型形态）不许静默合并。"""
        wt = worktree_mod.create_branch(self.root, self.mission.branch, "t", "main")
        self.mission.worktree = str(wt)
        self.runtime.submit_review("chenshu", "M1", "clean", "看过了")
        with self.assertRaises(ChenshuError) as ctx:
            self.runtime.merge("M1")
        self.assertIn("没有任何已提交改动", str(ctx.exception))
        self.assertEqual(self.runtime.mission("M1").status, "pending")

    def test_review_round_numbering(self):
        self.commit_on_branch("src/new.py")
        self.runtime.submit_review("chenshu", "M1", "p1-1", "先挑一个")
        out = self.runtime.submit_review("chenshu", "M1", "clean", "修好了")
        self.assertIn("第 2 轮", out)


class WriteGuardTest(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.wt = Path(self.tmp.name).resolve()
        self.addCleanup(self.tmp.cleanup)
        self.guard = _write_guard(self.wt)

    def test_inside_relative_allowed(self):
        self.assertIs(self.guard("write_file", {"path": "src/a.py"}), True)

    def test_absolute_outside_denied(self):
        verdict = self.guard("str_replace", {"path": "/etc/hosts"})
        self.assertIn("越界", verdict.reason)

    def test_dotdot_escape_denied(self):
        verdict = self.guard("write_file", {"path": "../escape.txt"})
        self.assertIn("越界", verdict.reason)

    def test_other_tools_pass(self):
        self.assertIs(self.guard("bash", {"command": "ls"}), True)


class SandboxWorktreePointerTest(unittest.TestCase):
    def test_pointer_parsed_to_minimal_set(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            main_git = root / "repo" / ".git" / "worktrees" / "wt1"
            main_git.mkdir(parents=True)
            linked = root / "linked"
            linked.mkdir()
            (linked / ".git").write_text(f"gitdir: {main_git}\n", encoding="utf-8")
            paths = _worktree_git_paths(linked)
            common = root / "repo" / ".git"
            self.assertEqual(
                paths,
                [main_git, common / "objects", common / "refs", common / "logs"],
            )
            #  hooks 与 config 绝不可写：能写 .git/hooks 就是沙箱逃逸
            self.assertNotIn(common, paths)

    def test_normal_repo_empty(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            self.assertEqual(_worktree_git_paths(root), [])


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class WorkerEndToEndTest(ChenshuCase):
    """worker 线程端到端：假 client 脚本驱动 worker 在 worktree 里干活、
    提交、标记完成；reviewer 过闸；merge 收回主干。"""

    def wait_done(self, runtime: ChenshuRuntime, name: str, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            thread = runtime._threads.get(name)
            if thread is not None and not thread.is_alive():
                return
            time.sleep(0.05)
        self.fail(f"{name} 没有在 {timeout}s 内收工")

    def test_full_cycle(self):
        self.init_repo()
        script = [
            #  worker：写文件（相对路径落 worktree）→ 提交 → 标记完成 → 交接
            tool_turn("w1", "write_file", {"path": "src/feature.py", "content": "f = 1\n"}),
            tool_turn("w2", "bash", {"command": "git add -A && git commit -qm feat"}),
            tool_turn("w3", "chenshu_mission", {"status": "completed", "note": "写完"}),
            text_turn(LONG),
            #  reviewer：提交 clean 评审 → 收尾
            tool_turn("r1", "chenshu_review",
                      {"target": "M1", "verdict": "clean", "summary": "无问题"}),
            text_turn("评审完毕，clean。" + LONG),
        ]
        runtime = self.make_runtime(script)
        with contextlib.redirect_stdout(io.StringIO()):
            runtime.init()
            runtime.plan([{"title": "加功能", "scope": ["src/"]}])
            out = runtime.spawn("bee", "worker", mission_id="M1")
            self.assertIn("bee", out)
            self.wait_done(runtime, "bee")
            events = runtime.wait(timeout=5)
            self.assertIn("bee 完成", events)
            self.assertEqual(runtime.member("bee").status, "done")
            self.assertEqual(runtime.mission("M1").status, "completed")
            #  worktree 里的提交在分支上，主工作区还没有
            self.assertFalse((self.root / "src" / "feature.py").exists())
            runtime.spawn("hawk", "reviewer", review_target="M1")
            self.wait_done(runtime, "hawk")
            runtime.wait(timeout=5)
            merged = runtime.merge("M1")
        self.assertIn("已合并", merged)
        self.assertTrue((self.root / "src" / "feature.py").is_file())
        #  完成通知走了通知轨道
        self.assertTrue(any("bee" in text for text, _ in self.notices))

    def test_spawn_cap_and_teardown_guard(self):
        self.init_repo()
        runtime = self.make_runtime([])
        runtime.init()
        runtime.plan([{"title": "a", "scope": ["src/"]}])
        self.config.chenshu_max_workers = 0  # 强制 max(1, 0)=1 后直接占满
        with mock.patch.object(runtime, "_live_count", lambda: 1):
            with self.assertRaises(ChenshuError) as ctx:
                runtime.spawn("bee", "worker", mission_id="M1")
            self.assertIn("上限", str(ctx.exception))

    def test_worker_requires_mission(self):
        self.init_repo()
        runtime = self.make_runtime([])
        runtime.init()
        with self.assertRaises(ChenshuError):
            runtime.spawn("bee", "worker")
        with self.assertRaises(ChenshuError):
            runtime.spawn("hawk", "reviewer")  # 缺 review_target

    def test_reserved_names_rejected(self):
        self.init_repo()
        runtime = self.make_runtime([])
        runtime.init()
        runtime.plan([{"title": "a", "scope": ["src/"]}])
        for reserved in ("chenshu", "all"):
            with self.assertRaises(ChenshuError) as ctx:
                runtime.spawn(reserved, "worker", mission_id="M1")
            self.assertIn("保留名", str(ctx.exception))

    def test_cjk_titles_get_unique_branches(self):
        self.init_repo()
        runtime = self.make_runtime([])
        runtime.init()
        runtime.plan([
            {"title": "改文档", "scope": ["docs/"]},
            {"title": "改测试", "scope": ["tests/"]},
        ])
        branches = [m.branch for m in runtime.missions]
        self.assertEqual(len(set(branches)), 2, f"分支撞名：{branches}")
        self.assertTrue(all(b.startswith("feat/m") for b in branches))

    def test_retired_member_can_respawn_and_reassign(self):
        """重启收养后：原名不带 resume 直接重 spawn；换名接管退役 owner 的
        mission 也放行（评审抓出的三路死锁回归）。"""
        self.init_repo()
        script = [text_turn(LONG), text_turn(LONG)]
        runtime = self.make_runtime(script)
        runtime.init()
        runtime.plan([{"title": "a", "scope": ["src/"]}])
        from xiaoyu.chenshu import Member

        with runtime.lock:
            runtime.members.append(
                Member(name="w1", kind="worker", mission_id="M1", status="retired")
            )
            runtime.missions[0].owner = "w1"
            runtime.missions[0].status = "active"
        with contextlib.redirect_stdout(io.StringIO()):
            out = runtime.spawn("w2", "worker", mission_id="M1")  # 换名接管
        self.assertIn("w2", out)
        self.wait_done_generic(runtime, "w2")
        #  原名重 spawn（w1 已退役、mission 已归 w2——用另一个 mission 验证原名路径）
        runtime.plan([{"title": "b", "scope": ["docs/"]}])
        with runtime.lock:
            runtime.members.append(
                Member(name="w3", kind="worker", mission_id="", status="retired")
            )
        with contextlib.redirect_stdout(io.StringIO()):
            out = runtime.spawn("w3", "worker", mission_id="M2")
        self.assertIn("w3", out)
        self.wait_done_generic(runtime, "w3")

    def wait_done_generic(self, runtime, name, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            thread = runtime._threads.get(name)
            if thread is not None and not thread.is_alive():
                return
            time.sleep(0.05)
        self.fail(f"{name} 没有在 {timeout}s 内收工")

    def test_merge_diff_failure_is_fail_closed(self):
        self.init_repo()
        runtime = self.make_runtime([])
        runtime.init()
        runtime.plan([{"title": "a", "scope": ["src/"]}])
        mission = runtime.missions[0]
        wt = worktree_mod.create_branch(self.root, mission.branch, "t", "main")
        (wt / "src" / "n.py").parent.mkdir(exist_ok=True)
        (wt / "src" / "n.py").write_text("x\n", encoding="utf-8")
        git(wt, "add", "-A")
        git(wt, "commit", "-qm", "n")
        runtime.submit_review("chenshu", "M1", "clean", "看过了")
        import xiaoyu.chenshu as chenshu_mod

        real_git = chenshu_mod._git

        def flaky(args, cwd):
            if args[0] == "diff":
                raise subprocess.TimeoutExpired(cmd="git diff", timeout=1)
            return real_git(args, cwd)

        with mock.patch.object(chenshu_mod, "_git", side_effect=flaky):
            with self.assertRaises(ChenshuError) as ctx:
                runtime.merge("M1")
        self.assertIn("scope 检查失败", str(ctx.exception))
        self.assertEqual(runtime.mission("M1").status, "pending")


if __name__ == "__main__":
    unittest.main()
