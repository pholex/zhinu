"""斗巧（竞争织造模式）的测试。不打网络。

校验层 / 同题竞织与判官裁决 / 裁决解析失败的 fail-open / 席位弃权 /
异构模型席位 / 写型竞争的 worktree 隔离，各卡一处。
需要区分各席响应顺序的用例一律并发=1 串行跑（脚本消费顺序=席位顺序）。
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import unittest
from unittest import mock

from xiaoyu.agent import Usage
from xiaoyu.agents import AgentSpec, RunStore
from xiaoyu.douqiao import make_douqiao_tool
from xiaoyu.providers import Registry, UnknownModel
from xiaoyu.render import PlainSink

from .test_agent_paths import AgentTestCase, FakeClient, call_fragment, chunk

HAS_GIT = shutil.which("git") is not None

READER = AgentSpec(
    name="thinker", description="d", system_prompt="工作区 {workspace}",
    tools=("read_file", "grep", "list_files"),
)
WRITER = AgentSpec(
    name="builder", description="d", system_prompt="工作区 {workspace}",
    tools=("read_file", "write_file"),
)

LONG = "方案：" + "详" * 210
VERDICT = "各有所长。\n胜者：#2\n理由：更完备。\n败者亮点：#1 的命名更好。"


def text_turn(text: str) -> list:
    return [chunk(content=text)]


def tool_turn(call_id: str, name: str, args: dict) -> list:
    return [chunk(tool_calls=[call_fragment(0, call_id, name, json.dumps(args))])]


class DouqiaoTestCase(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.config.qixiang_concurrency = 1  # 串行：脚本消费顺序 = 席位顺序

    def make_tool(self, specs, script, runs=None):
        self.sub_client = FakeClient(script)
        self.runs = runs if runs is not None else RunStore()
        self.registry = Registry.for_client(self.sub_client)
        return make_douqiao_tool(
            list(specs),
            self.config,
            self.registry,
            Usage(),
            PlainSink(indent="", verbose=False),
            lambda name, args: True,
            None,
            self.runs,
        )

    def call(self, tool, **kwargs) -> str:
        with contextlib.redirect_stdout(io.StringIO()):
            return tool.handler(**kwargs)


class ValidationTest(DouqiaoTestCase):
    def test_unknown_spec(self):
        tool = self.make_tool([READER], [])
        result = self.call(tool, spec="nobody", task="t")
        self.assertTrue(result.startswith("ERROR"))

    def test_task_required(self):
        tool = self.make_tool([READER], [])
        result = self.call(tool, spec="thinker", task="  ")
        self.assertTrue(result.startswith("ERROR"))

    def test_contestant_bounds(self):
        tool = self.make_tool([READER], [])
        for bad in (1, 7):
            result = self.call(tool, spec="thinker", task="t", contestants=bad)
            self.assertTrue(result.startswith("ERROR"), result)

    def test_unknown_model_rejected_upfront(self):
        tool = self.make_tool([READER], [])
        with mock.patch.object(
            self.registry, "resolve", side_effect=UnknownModel("没人认领")
        ):
            result = self.call(tool, spec="thinker", task="t", models=["ghost-1", "ghost-2"])
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("ghost-1", result)

    def test_zero_contestants_hits_bounds_not_default(self):
        """falsy 0 不许静默换成默认席位数（3 席 = 3 倍真金白银）。"""
        tool = self.make_tool([READER], [])
        result = self.call(tool, spec="thinker", task="t", contestants=0)
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("当前 0", result)

    def test_bad_capability_mode_rejected_upfront(self):
        tool = self.make_tool([READER], [])
        result = self.call(tool, spec="thinker", task="t", capability_mode="sandbox")
        self.assertTrue(result.startswith("ERROR"))


class ContestTest(DouqiaoTestCase):
    def test_judge_picks_winner(self):
        script = [
            text_turn("甲席 " + LONG),   # #1
            text_turn("乙席 " + LONG),   # #2
            text_turn(VERDICT),          # 判官
        ]
        tool = self.make_tool([READER], script)
        result = self.call(tool, spec="thinker", task="设计一个方案", contestants=2)
        self.assertIn("2 席完赛", result)
        self.assertIn("胜者 #2", result)
        self.assertIn("👑", result)
        self.assertIn("败者亮点", result)
        #  两席各有 resume 句柄（判官不给句柄：没有任何工具能消费它，
        #  死链宁可不写——评审 finding 的回归）
        self.assertEqual(len(re.findall(r"resume_from: [0-9a-f]{8}", result)), 2)

    def test_unparseable_verdict_fails_open(self):
        script = [
            text_turn("甲席 " + LONG),
            text_turn("乙席 " + LONG),
            text_turn("都不错，难分高下。"),  # 没有「胜者：#N」
        ]
        tool = self.make_tool([READER], script)
        result = self.call(tool, spec="thinker", task="t", contestants=2)
        self.assertIn("没有可解析的「胜者：#N」", result)
        #  成果照常在
        self.assertIn("甲席", result)
        self.assertIn("乙席", result)

    def test_forfeit_below_min_skips_judge(self):
        #  席 1 弃权、席 2 完赛 → 只剩一席，无赛可评；脚本恰好耗尽即证明
        #  判官没有被调用（多调会触发 FakeClient 的脚本耗尽断言）
        script = [RuntimeError("模型炸了"), text_turn("乙席 " + LONG)]
        tool = self.make_tool([READER], script)
        result = self.call(tool, spec="thinker", task="t", contestants=2)
        self.assertIn("1 席完赛 / 1 席弃权", result)
        self.assertIn("完赛席位不足", result)
        self.assertNotIn("判官裁决", result)

    def test_heterogeneous_models_per_seat(self):
        script = [
            text_turn("甲席 " + LONG),
            text_turn("乙席 " + LONG),
            text_turn(VERDICT),
        ]
        tool = self.make_tool([READER], script)
        result = self.call(
            tool, spec="thinker", task="t", models=["model-a", "model-b"]
        )
        self.assertIn("model=model-a", result)
        self.assertIn("model=model-b", result)
        #  存档里各席钉住了自己的模型
        models = sorted(run.model for run in self.runs.values() if run.spec_name == "thinker")
        self.assertEqual(models, ["model-a", "model-b"])

    def test_verdict_takes_last_match(self):
        """判官中途的「某维度胜者：#1」不算定论——取最后一处（误加冕回归）。"""
        verdict = "论简洁维度的胜者：#1。但综合看——\n胜者：#2\n理由：更完备。\n败者亮点：无"
        script = [
            text_turn("甲席 " + LONG),
            text_turn("乙席 " + LONG),
            text_turn(verdict),
        ]
        tool = self.make_tool([READER], script)
        result = self.call(tool, spec="thinker", task="t", contestants=2)
        self.assertIn("胜者 #2", result)
        self.assertNotIn("胜者 #1", result)

    def test_judge_failure_not_authoritative(self):
        """判官中途失败（哪怕留了半截「胜者：#1」）不冒充权威裁决。"""
        from xiaoyu import douqiao as douqiao_mod
        from xiaoyu.agents import DelegationResult

        script = [text_turn("甲席 " + LONG), text_turn("乙席 " + LONG)]
        tool = self.make_tool([READER], script)
        broken = DelegationResult(
            failure="APIError: 429", answer="初评：胜者：#1，接下来核查……", run_id="deadbeef"
        )
        with mock.patch.object(douqiao_mod, "_run_judge", return_value=broken):
            result = self.call(tool, spec="thinker", task="t", contestants=2)
        self.assertIn("判官中途失败", result)
        self.assertIn("不作数", result)
        self.assertNotIn("👑", result)
        self.assertNotIn("胜者 #1", result.split("\n")[0])

    def test_verdict_naming_forfeited_seat(self):
        """判官点名不在完赛之列的席位：说清点名了谁，不谎报「不可解析」。"""
        script = [
            text_turn("甲席 " + LONG),
            text_turn("乙席 " + LONG),
            text_turn("胜者：#9\n理由：？\n败者亮点：无"),
        ]
        tool = self.make_tool([READER], script)
        result = self.call(tool, spec="thinker", task="t", contestants=2)
        self.assertIn("判官点名 #9", result)
        self.assertNotIn("没有可解析", result)
        self.assertNotIn("👑", result)

    def test_readonly_narrowing_skips_worktree_in_nongit(self):
        """写型 spec + capability_mode=read-only 在非 git 工作区照样能比
        （隔离按有效工具集判定，不再按 spec 静态属性一刀切）。"""
        script = [
            text_turn("甲席 " + LONG),
            text_turn("乙席 " + LONG),
            text_turn(VERDICT),
        ]
        tool = self.make_tool([WRITER], script)  # self.root 不是 git 仓库
        result = self.call(
            tool, spec="builder", task="t", contestants=2, capability_mode="read-only"
        )
        self.assertIn("2 席完赛", result)
        self.assertIn("胜者 #2", result)
        self.assertNotIn("worktree:", result)

    def test_short_answer_gets_continuation(self):
        script = [
            text_turn("太短"),                     # #1 首轮触发追问
            text_turn("补全的完整方案 " + LONG),   # #1 追问轮
            text_turn("乙席 " + LONG),             # #2
            text_turn(VERDICT),                    # 判官
        ]
        tool = self.make_tool([READER], script)
        result = self.call(tool, spec="thinker", task="t", contestants=2)
        self.assertIn("补全的完整方案", result)
        self.assertIn("2 席完赛", result)


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class WriteContestTest(DouqiaoTestCase):
    def setUp(self):
        super().setUp()
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
            cwd=self.root, check=True,
        )
        from xiaoyu import worktree as worktree_mod

        patcher = mock.patch.object(
            worktree_mod, "user_config_dir", lambda: self.root / "cfg"
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_each_seat_isolated_in_worktree(self):
        script = [
            tool_turn("w1", "write_file", {"path": "plan.md", "content": "甲案"}),
            text_turn("甲席 " + LONG),
            tool_turn("w2", "write_file", {"path": "plan.md", "content": "乙案"}),
            text_turn("乙席 " + LONG),
            text_turn(VERDICT),
        ]
        tool = self.make_tool([WRITER], script)
        result = self.call(tool, spec="builder", task="写方案", contestants=2)
        self.assertIn("胜者 #2", result)
        #  两席写同名文件也互不冲突（各自 worktree），主工作区没被碰
        self.assertEqual(result.count("worktree:"), 2)
        self.assertFalse((self.root / "plan.md").exists())
        from pathlib import Path

        paths = re.findall(r"worktree: (\S+)（", result)
        self.assertEqual(len(paths), 2)
        contents = {
            (Path(path) / "plan.md").read_text(encoding="utf-8") for path in paths
        }
        self.assertEqual(contents, {"甲案", "乙案"})


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class WorktreeCommittedWorkTest(AgentTestCase):
    """detached worktree 里已提交的成果不许被当"干净"删掉（数据丢失回归）。"""

    def test_committed_detached_worktree_is_dirty(self):
        from xiaoyu import worktree as worktree_mod

        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "a.txt").write_text("a\n", encoding="utf-8")
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
            cwd=self.root, check=True,
        )
        with mock.patch.object(
            worktree_mod, "user_config_dir", lambda: self.root / "cfg"
        ):
            wt = worktree_mod.create(self.root, "t")
        #  刚建好：detached 在分支 tip 上 → 干净
        self.assertFalse(worktree_mod.dirty(wt))
        (wt / "b.txt").write_text("b\n", encoding="utf-8")
        self.assertTrue(worktree_mod.dirty(wt))  # 未提交改动
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "work"],
            cwd=wt, check=True,
        )
        #  porcelain 干净但 HEAD 不属于任何分支 → 仍算 dirty（保留）
        self.assertTrue(worktree_mod.dirty(wt))


if __name__ == "__main__":
    unittest.main()
