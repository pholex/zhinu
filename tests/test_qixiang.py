"""七襄（批量并行委托）的测试。不打网络。

校验层（spec/items/模板/去重/上限）、扇出与按序聚合、失败隔离与续跑提示、
min-summary 追问闸、批量 resume、非只读默认 worktree 隔离，各卡一处。
并发路径用"全同响应"脚本（FakeClient 的脚本消费顺序在多线程下不定，
需要区分各项答案的用例一律并发=1 串行跑）。
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

from xiaoyu import qixiang as qixiang_mod
from xiaoyu.agent import Usage
from xiaoyu.agents import AgentSpec, RunStore
from xiaoyu.providers import Registry
from xiaoyu.qixiang import _ItemState, _status_of, make_qixiang_tool
from xiaoyu.render import PlainSink

from .test_agent_paths import AgentTestCase, FakeClient, call_fragment, chunk

HAS_GIT = shutil.which("git") is not None

READER = AgentSpec(
    name="reader", description="d", system_prompt="工作区 {workspace}",
    tools=("read_file", "grep", "list_files"),
)
WRITER = AgentSpec(
    name="writer", description="d", system_prompt="工作区 {workspace}",
    tools=("read_file", "write_file"),
)

#  ≥200 字符的长结论：不触发 min-summary 追问
LONG = "结论：" + "详" * 210


def text_turn(text: str) -> list:
    return [chunk(content=text)]


def tool_turn(call_id: str, name: str, args: dict) -> list:
    return [chunk(tool_calls=[call_fragment(0, call_id, name, json.dumps(args))])]


class QixiangTestCase(AgentTestCase):
    def make_tool(self, specs, script, runs=None):
        self.sub_client = FakeClient(script)
        self.runs = runs if runs is not None else RunStore()
        return make_qixiang_tool(
            list(specs),
            self.config,
            Registry.for_client(self.sub_client),
            Usage(),
            PlainSink(indent="", verbose=False),
            lambda name, args: True,
            None,
            self.runs,
        )

    def call(self, tool, **kwargs) -> str:
        with contextlib.redirect_stdout(io.StringIO()):
            return tool.handler(**kwargs)


class ValidationTest(QixiangTestCase):
    def test_unknown_spec(self):
        tool = self.make_tool([READER], [])
        result = self.call(tool, spec="nobody", prompt_template="{{item}}", items=["a", "b"])
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("reader", result)

    def test_single_item_rejected(self):
        tool = self.make_tool([READER], [])
        result = self.call(tool, spec="reader", prompt_template="{{item}}", items=["a"])
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("至少 2 项", result)

    def test_template_placeholder_required(self):
        tool = self.make_tool([READER], [])
        result = self.call(tool, spec="reader", prompt_template="没有占位符", items=["a", "b"])
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("{{item}}", result)

    def test_duplicate_expansion_rejected(self):
        tool = self.make_tool([READER], [])
        result = self.call(tool, spec="reader", prompt_template="查 {{item}}", items=["x", "x"])
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("完全相同", result)

    def test_over_limit_rejected(self):
        tool = self.make_tool([READER], [])
        with mock.patch.object(qixiang_mod, "MAX_ITEMS", 3):
            result = self.call(
                tool, spec="reader", prompt_template="{{item}}",
                items=["a", "b", "c", "d"],
            )
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("最多 3 项", result)

    def test_bad_resume_shape(self):
        tool = self.make_tool([READER], [])
        result = self.call(tool, spec="reader", resume=["not-a-dict"])
        self.assertTrue(result.startswith("ERROR"))
        self.assertIn("resume", result)

    def test_nothing_to_do(self):
        tool = self.make_tool([READER], [])
        result = self.call(tool, spec="reader")
        self.assertTrue(result.startswith("ERROR"))


class FanOutTest(QixiangTestCase):
    def setUp(self):
        super().setUp()
        self.config.qixiang_concurrency = 1  # 串行：脚本消费顺序 = 输入顺序

    def test_ordered_report_with_resume_handles(self):
        script = [text_turn("甲的结论 " + LONG), text_turn("乙的结论 " + LONG)]
        tool = self.make_tool([READER], script)
        result = self.call(
            tool, spec="reader", prompt_template="查 {{item}}", items=["甲", "乙"]
        )
        self.assertIn("完成 2 / 失败 0", result)
        #  输入顺序聚合
        self.assertLess(result.index("甲的结论"), result.index("乙的结论"))
        self.assertEqual(len(re.findall(r"resume_from: [0-9a-f]{8}", result)), 2)
        self.assertEqual(len(self.runs), 2)

    def test_failure_isolated_with_retry_hint(self):
        script = [RuntimeError("模型炸了"), text_turn("乙的结论 " + LONG)]
        tool = self.make_tool([READER], script)
        with mock.patch.object(qixiang_mod, "MIN_ANSWER_CHARS", 0):
            result = self.call(
                tool, spec="reader", prompt_template="查 {{item}}", items=["甲", "乙"]
            )
        self.assertIn("完成 1 / 失败 1", result)
        self.assertIn("子 agent 失败", result)
        self.assertIn("批量续跑", result)
        #  失败的也有 resume 句柄（从 failed 恢复继续修是正当用法）
        self.assertEqual(len(re.findall(r"resume_from: [0-9a-f]{8}", result)), 2)

    def test_short_answer_gets_continuation(self):
        script = [
            text_turn("太短"),                      # 第 1 项首轮：触发追问
            text_turn("补全后的完整交接 " + LONG),   # 追问轮（resume 通道）
            text_turn("乙的结论 " + LONG),           # 第 2 项
        ]
        tool = self.make_tool([READER], script)
        result = self.call(
            tool, spec="reader", prompt_template="查 {{item}}", items=["甲", "乙"]
        )
        self.assertIn("补全后的完整交接", result)
        self.assertNotIn("--- 1/2 完成 · 甲\n太短", result)

    def test_continuation_keeps_capability_mode(self):
        """追问轮不许丢档位收紧：read-only 批里被追问的子 agent 试图 write_file
        必须仍然写不进（评审抓出的越权回归）。"""
        script = [
            text_turn("太短"),  # 触发追问
            #  追问轮里模型试图写文件——档位若继承，write_file 不在工具箱里
            tool_turn("w1", "write_file", {"path": "sneak.txt", "content": "x"}),
            text_turn("写不了，补交接 " + LONG),
            text_turn("乙 " + LONG),
        ]
        tool = self.make_tool([WRITER], script)
        result = self.call(
            tool, spec="writer", prompt_template="改 {{item}}", items=["甲", "乙"],
            capability_mode="read-only", isolation="none",
        )
        self.assertIn("完成 2", result)
        self.assertFalse((self.root / "sneak.txt").exists(), "追问轮拿回了写权限")

    def test_store_capacity_scales_with_batch(self):
        """批内的 resume 句柄不许被滚动淘汰成死链。"""
        script = [text_turn(f"第{n}项 " + LONG) for n in range(3)]
        tool = self.make_tool([READER], script)
        with mock.patch("xiaoyu.agents.MAX_RUNS", 2):
            result = self.call(
                tool, spec="reader", prompt_template="查 {{item}}",
                items=["a", "b", "c"],
            )
        ids = re.findall(r"resume_from: ([0-9a-f]{8})", result)
        self.assertEqual(len(ids), 3)
        for rid in ids:
            self.assertIn(rid, self.runs, "报告里的句柄已被淘汰成死链")

    def test_batch_resume_inherits_transcript(self):
        first = self.make_tool([READER], [text_turn("首轮 " + LONG), text_turn("次轮 " + LONG)])
        report = self.call(
            first, spec="reader", prompt_template="任务 {{item}}", items=["A", "B"]
        )
        rid = re.search(r"resume_from: ([0-9a-f]{8})", report).group(1)
        second = self.make_tool([READER], [text_turn("续上了 " + LONG)], runs=self.runs)
        result = self.call(second, spec="reader", resume={rid: "接着干"})
        self.assertIn("续上了", result)
        new_id = re.search(r"resume_from: ([0-9a-f]{8})", result).group(1)
        dump = json.dumps(self.runs[new_id].messages, ensure_ascii=False)
        self.assertIn("任务 A", dump)
        self.assertIn("接着干", dump)

    def test_unknown_resume_id_fails_item_not_batch(self):
        script = [text_turn("乙 " + LONG)]
        tool = self.make_tool([READER], script)
        result = self.call(tool, spec="reader", resume={"deadbeef": "继续"})
        self.assertIn("未执行 1", result)
        self.assertIn("resume_from='deadbeef'", result)


class ConcurrentTest(QixiangTestCase):
    def test_parallel_batch_completes(self):
        self.config.qixiang_concurrency = 4
        script = [text_turn(LONG) for _ in range(6)]
        tool = self.make_tool([READER], script)
        result = self.call(
            tool, spec="reader", prompt_template="查 {{item}}",
            items=[f"i{n}" for n in range(6)],
        )
        self.assertIn("完成 6 / 失败 0", result)
        #  块按输入顺序（与完成先后无关）
        positions = [result.index(f"· i{n}") for n in range(6)]
        self.assertEqual(positions, sorted(positions))


@unittest.skipUnless(HAS_GIT, "机器上没有 git")
class IsolationDefaultTest(QixiangTestCase):
    def setUp(self):
        super().setUp()
        self.config.qixiang_concurrency = 1
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
            cwd=self.root, check=True,
        )

    def _write_script(self, filename: str) -> list:
        return [
            tool_turn("w1", "write_file", {"path": filename, "content": "x"}),
            text_turn(f"写完 {filename} " + LONG),
        ]

    def test_nonreadonly_defaults_to_worktree(self):
        from xiaoyu import worktree as worktree_mod

        script = self._write_script("a.txt") + self._write_script("b.txt")
        tool = self.make_tool([WRITER], script)
        real_create = worktree_mod.create
        with mock.patch.object(
            worktree_mod, "user_config_dir", lambda: self.root / "cfg"
        ), mock.patch.object(
            worktree_mod, "create", side_effect=real_create
        ) as created:
            result = self.call(
                tool, spec="writer", prompt_template="写 {{item}}",
                items=["a.txt", "b.txt"],
            )
        self.assertIn("完成 2", result)
        self.assertEqual(created.call_count, 2)
        #  改动没落主工作区
        self.assertFalse((self.root / "a.txt").exists())
        self.assertIn("worktree:", result)

    def test_isolation_none_opts_out(self):
        script = self._write_script("c.txt") + self._write_script("d.txt")
        tool = self.make_tool([WRITER], script)
        result = self.call(
            tool, spec="writer", prompt_template="写 {{item}}",
            items=["c.txt", "d.txt"], isolation="none",
        )
        self.assertIn("完成 2", result)
        self.assertTrue((self.root / "c.txt").exists())
        self.assertNotIn("worktree:", result)


class StatusClassifyTest(unittest.TestCase):
    def test_timed_out_is_aborted(self):
        state = _ItemState(index=0, label="x", task="t")
        state.timed_out = True
        self.assertEqual(_status_of(state, cancelled=False), "aborted")

    def test_never_started_is_aborted(self):
        state = _ItemState(index=0, label="x", task="t")
        state.never_started = True
        self.assertEqual(_status_of(state, cancelled=True), "aborted")

    def test_crash_is_failed(self):
        state = _ItemState(index=0, label="x", task="t")
        state.crash = "boom"
        self.assertEqual(_status_of(state, cancelled=False), "failed")


if __name__ == "__main__":
    unittest.main()
