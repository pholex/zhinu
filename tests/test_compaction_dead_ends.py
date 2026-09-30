"""上下文压缩走不通的几种形状：大批并行工具结果、带图的原始任务、落盘预览、压不动的超窗。

不打网络：摘要器用桩，Agent 用假 client。
"""

from __future__ import annotations

import unittest

from xiaoyu.compaction import Compactor, microcompact

from .test_context import assert_valid_sequence

BIG = "x" * 2000


def parallel_batch(prefix: str, count: int, name: str = "read_file", output: str = BIG) -> list[dict]:
    """一条 assistant 并行调 count 个工具 + 各自的结果（一个结果一条消息）。"""
    calls = [
        {"id": f"{prefix}{n}", "type": "function", "function": {"name": name, "arguments": "{}"}}
        for n in range(count)
    ]
    results = [
        {"role": "tool", "tool_call_id": f"{prefix}{n}", "content": f"{prefix}{n}:{output}"}
        for n in range(count)
    ]
    return [{"role": "assistant", "content": None, "tool_calls": calls}, *results]


def history_ending_in_batch(count: int) -> list[dict]:
    """前面有几轮已被回应过的工具往复，末尾是一批还没人读过的并行结果。"""
    messages: list[dict] = [
        {"role": "system", "content": "你是小羽"},
        {"role": "user", "content": "把这些模块都看一遍"},
    ]
    for n in range(4):
        messages += parallel_batch(f"old{n}_", 1)
    messages += parallel_batch("last", count)
    return messages


def build_compactor(keep_recent: int = 8, **kwargs) -> Compactor:
    kwargs.setdefault("summarizer", lambda _t, _p: "读过前面几个模块")
    return Compactor(context_limit=50_000, compact_at=0.7, keep_recent=keep_recent, **kwargs)


class TestCutWithLargeParallelBatch(unittest.TestCase):
    """末批并行结果不少于 keep_recent 条：切点退到这一批的 assistant 上。"""

    def test_cut_falls_back_to_batch_owner(self) -> None:
        for count in (8, 9, 12):
            with self.subTest(count=count):
                messages = history_ending_in_batch(count)
                cut = build_compactor().find_cut(messages, min_index=2)
                owner = len(messages) - count - 1
                self.assertEqual(cut, owner)
                self.assertTrue(messages[cut].get("tool_calls"))

    def test_compaction_proceeds_and_keeps_whole_batch(self) -> None:
        messages = history_ending_in_batch(12)
        compacted, note = build_compactor().compact(messages)
        self.assertIn("已压缩", note)
        assert_valid_sequence(self, compacted)
        kept = [m["tool_call_id"] for m in compacted if m.get("role") == "tool"]
        self.assertEqual(kept, [f"last{n}" for n in range(12)])
        #  早先的往复被压掉了
        self.assertNotIn("old0_0", str(compacted[2:]))

    def test_nothing_before_the_batch_means_no_cut(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "任务"},
            *parallel_batch("only", 9),
        ]
        self.assertEqual(build_compactor().find_cut(messages, min_index=2), -1)


class TestMicrocompactAlignsToBatches(unittest.TestCase):
    """保护边界不落进一批的中间；模型还没回应过的那一批整批不清。"""

    def test_unread_batch_larger_than_keep_recent_is_untouched(self) -> None:
        for count in (9, 12):
            with self.subTest(count=count):
                messages = history_ending_in_batch(count)
                result, cleared, _ = microcompact(messages, keep_recent=8)
                #  更早的四条照清，末批一条不动
                self.assertEqual(cleared, 4)
                self.assertEqual(result[-count:], messages[-count:])

    def test_unread_batch_survives_a_tiny_keep_recent(self) -> None:
        messages = history_ending_in_batch(3)
        #  工具回图 / 中途插话会以 user 身份排在结果后面：结果仍然没人读过
        messages.append({"role": "user", "content": "顺便看下 README"})
        result, cleared, _ = microcompact(messages, keep_recent=1)
        self.assertEqual(cleared, 4)
        self.assertEqual(result[-4:], messages[-4:])

    def test_answered_batch_straddling_the_boundary_is_kept_whole(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "任务"},
            *parallel_batch("early", 1),
            *parallel_batch("mid", 4),
            {"role": "assistant", "content": "看完了"},
            {"role": "user", "content": "继续"},
            {"role": "assistant", "content": "好"},
        ]
        #  按条数数的边界落在 mid 这一批的第 2 个结果上
        result, cleared, _ = microcompact(messages, keep_recent=6)
        self.assertEqual(cleared, 1)
        self.assertIn("已清理", result[3]["content"])
        self.assertEqual(result[4:], messages[4:])


if __name__ == "__main__":
    unittest.main()
