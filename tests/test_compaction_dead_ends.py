"""上下文压缩走不通的几种形状：大批并行工具结果、带图的原始任务、落盘预览、压不动的超窗。

不打网络：摘要器用桩，Agent 用假 client。
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import compaction, errors, media, session_log, tokens, tools
from xiaoyu.compaction import (
    CONTEXT_PREFIX,
    Compactor,
    age_tool_images,
    microcompact,
    split_head,
)
from xiaoyu.config import Config

from .test_agent_paths import AgentTestCase, chunk
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


def pictured_task() -> dict:
    return {
        "role": "user",
        "content": [
            media.text_part("照这张设计稿改首页"),
            media.image_part("xiaoyu-media://mock.png"),
            media.text_part("按钮颜色以图为准"),
        ],
    }


def turns(count: int, tag: str) -> list[dict]:
    messages: list[dict] = []
    for n in range(count):
        messages.append({"role": "assistant", "content": f"{tag} 进展 {n}：" + "阿" * 300})
        messages.append({"role": "user", "content": f"{tag} 要求 {n}"})
    return messages


class TestPicturedTaskSurvivesCompaction(unittest.TestCase):
    """原始任务带图：压缩后图还在原位，摘要接在后面，下次压缩照常拆得开。"""

    def compact_twice(self) -> tuple[list[dict], list[dict], list[str]]:
        transcripts: list[str] = []

        def summarizer(transcript: str, _prefix: list) -> str:
            transcripts.append(transcript)
            return f"第 {len(transcripts)} 份交接说明"

        compactor = build_compactor(keep_recent=2, summarizer=summarizer, user_voice_tokens=0)
        messages = [{"role": "system", "content": "s"}, pictured_task(), *turns(8, "甲")]
        first, note = compactor.compact(messages)
        self.assertIn("已压缩", note)
        second, note = compactor.compact([*first, *turns(8, "乙")])
        self.assertIn("已压缩", note)
        return first, second, transcripts

    def test_image_part_is_kept_in_place(self) -> None:
        first, second, _ = self.compact_twice()
        for compacted in (first, second):
            head = compacted[1]
            self.assertEqual(head["content"][:3], pictured_task()["content"])
            self.assertEqual(len(media.images_of(head["content"])), 1)
            #  用户贴的图不带工具图标记，老化碰不到它
            self.assertNotIn(media.TOOL_MEDIA_KEY, head)
            assert_valid_sequence(self, compacted)
        self.assertIs(age_tool_images(second, high_water=0, keep=0)[0], second)

    def test_next_compaction_still_splits_task_from_summary(self) -> None:
        first, second, transcripts = self.compact_twice()
        original, previous = split_head(media.text_of(first[1]["content"]))
        self.assertEqual(original, "照这张设计稿改首页[图片]按钮颜色以图为准")
        self.assertTrue(previous.startswith("第 1 份交接说明"))
        #  第二次压缩：上一份摘要作为「此前的压缩摘要」重新参与，不层层累加
        self.assertIn("【此前的压缩摘要】\n第 1 份交接说明", transcripts[1])
        text = media.text_of(second[1]["content"])
        self.assertEqual(text.count(CONTEXT_PREFIX), 1)
        self.assertIn("第 2 份交接说明", text)
        self.assertNotIn("第 1 份交接说明", text)
        self.assertEqual(text.count("照这张设计稿改首页"), 1)

    def test_text_only_task_keeps_string_head(self) -> None:
        """不带图的首条（哪怕是部件列表）仍拼成字符串，与纯文本任务同一形态。"""
        for task in ("只有文字的任务", [media.text_part("只有文字的任务")]):
            with self.subTest(task=task):
                compactor = build_compactor(keep_recent=2, user_voice_tokens=0)
                messages = [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": task},
                    *turns(8, "甲"),
                ]
                compacted, _ = compactor.compact(messages)
                self.assertEqual(
                    compacted[1]["content"],
                    f"只有文字的任务\n\n{CONTEXT_PREFIX}读过前面几个模块",
                )


class TestClearedSpillKeepsRecallId(unittest.TestCase):
    """被清理的输出若是落盘预览：占位留住召回 id、指向 recall，而不是让模型重跑。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        config = Config(
            base_url="x", model="m", workspace=Path(self.tmp.name).resolve(), enable_plugins=False
        )
        config.max_tool_output = 1200
        self.box = tools.Toolbox(config)

    def history(self, output: str) -> list[dict]:
        return [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "跑一下迁移"},
            *parallel_batch("run", 1, name="bash", output=output),
            {"role": "assistant", "content": "跑完了"},
        ]

    def test_placeholder_points_at_recall(self) -> None:
        #  先落一次别的，让真实 id 不是 1：认错成序号之外的数字会露馅
        self.box._bound_output("bash", "y" * 5000)  # noqa: SLF001
        preview = self.box._bound_output(  # noqa: SLF001
            "bash", "\n".join(f"migrated row {n}" for n in range(2000))
        )
        messages = self.history(preview)
        messages[3]["content"] = preview
        result, cleared, _ = microcompact(messages, keep_recent=1)
        self.assertEqual(cleared, 1)
        stub = result[3]["content"]
        self.assertIn('recall(id="2")', stub)
        self.assertNotIn("重新调用", stub)
        self.assertEqual(compaction.recall_id_of(preview), "2")
        #  接回历史时工具箱靠这句认出带进来的 id（adopt_history），占位里不能丢
        self.assertEqual(tools._RECALL_ID_MENTION.search(stub).group(1), "2")  # noqa: SLF001

    def test_plain_output_still_asks_to_rerun(self) -> None:
        #  正文里碰巧提到召回 id 的普通输出不是预览：照旧让模型重新调用
        messages = self.history("grep 到一行：完整内容见召回 id 7\n" + BIG)
        result, cleared, _ = microcompact(messages, keep_recent=1)
        self.assertEqual(cleared, 1)
        self.assertIn("请重新调用 bash", result[3]["content"])
        self.assertNotIn("recall", result[3]["content"])


def log_lines(count: int, tag: str = "row") -> str:
    """一段头、中、尾各不相同的长输出：验证砍的是中段。"""
    return "\n".join(f"{tag} {n:05d} ok" for n in range(count))


class TestTightenLastResort(unittest.TestCase):
    """摘要压缩缩不动时的最后手段：逐档收紧工具结果，必要时砍原始任务的中段。"""

    def history(self, outputs: int = 4, size: int = 3000) -> list[dict]:
        return [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "跑一遍全量检查"},
            *parallel_batch("chk", outputs, name="bash", output=log_lines(size)),
        ]

    def test_picks_the_lightest_rung_that_fits(self) -> None:
        messages = self.history()
        for limit, cap in ((40_000, 8_000), (9_000, 2_000), (1_000, 500)):
            with self.subTest(limit=limit):
                compactor = build_compactor()
                compactor.context_limit = limit
                tightened, note = compactor.tighten(messages)
                self.assertIn(f"{cap} 字符以内", note)
                assert_valid_sequence(self, tightened)
                for before, after in zip(messages[3:], tightened[3:]):
                    self.assertLessEqual(len(after["content"]), cap)
                    #  保头保尾，省略处留下标记并说明怎么拿回来
                    self.assertTrue(after["content"].startswith(before["content"][:100]))
                    self.assertTrue(after["content"].endswith(before["content"][-100:]))
                    self.assertIn("中段已省略", after["content"])
                    self.assertIn("缩小范围重新调用工具", after["content"])
                self.assertLess(
                    tokens.estimate_messages(tightened), tokens.estimate_messages(messages)
                )

    def test_nothing_to_tighten_returns_the_same_list(self) -> None:
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "任务"},
            {"role": "assistant", "content": "阿" * 5000},
        ]
        compactor = build_compactor()
        compactor.context_limit = 1_000
        tightened, note = compactor.tighten(messages)
        self.assertIs(tightened, messages)
        self.assertEqual(note, "")

    def test_spilled_preview_points_at_recall(self) -> None:
        preview = (
            "[输出超长：原始 90000 字符 / 约 3000 行，完整内容已存，召回 id: 4。以下保留开头和结尾]\n"
            + log_lines(3000)
        )
        messages = self.history(outputs=1)
        messages[3]["content"] = preview
        tightened, _ = build_compactor().tighten(messages)
        self.assertIn('recall(id="4")', tightened[3]["content"])
        self.assertNotIn("重新调用工具", tightened[3]["content"])

    def test_oversized_task_loses_only_its_middle(self) -> None:
        task = log_lines(6000, tag="需求")
        summary = f"{CONTEXT_PREFIX}上一份交接说明"
        for content in (
            f"{task}\n\n{summary}",
            [
                media.image_part("xiaoyu-media://mock.png"),
                media.text_part(task),
                media.text_part(f"\n\n{summary}"),
            ],
        ):
            with self.subTest(parts=media.is_parts(content)):
                messages = [
                    {"role": "system", "content": "s"},
                    {"role": "user", "content": content},
                    {"role": "assistant", "content": "好"},
                ]
                compactor = build_compactor()
                compactor.context_limit = 20_000
                tightened, note = compactor.tighten(messages)
                self.assertIn("原始任务", note)
                original, previous = split_head(media.text_of(tightened[1]["content"]))
                self.assertEqual(previous, "上一份交接说明")
                self.assertIn("原始任务超出模型窗口", original)
                self.assertIn("需求 00000 ok", original)
                self.assertIn("需求 05999 ok", original)
                self.assertNotIn("需求 03000 ok", original)
                self.assertLessEqual(
                    tokens.estimate_text(original), 20_000 * compaction.TASK_WINDOW_SHARE * 1.1
                )
                self.assertEqual(
                    media.images_of(tightened[1]["content"]), media.images_of(content)
                )

    def test_task_within_its_share_is_left_alone(self) -> None:
        messages = self.history()
        compactor = build_compactor()
        compactor.context_limit = 1_000
        tightened, note = compactor.tighten(messages)
        self.assertEqual(tightened[1], messages[1])
        self.assertNotIn("原始任务", note)


OVERFLOW_TEXT = "This model's maximum context length is 32000 tokens"


class TestOverflowRecovery(AgentTestCase):
    """服务端报上下文超限之后：缩得动才重发，缩不动立刻给出可操作的报错。"""

    def send(self, agent, text: str = "继续") -> tuple[list, str]:
        """跑一轮，返回 (退避等待的秒数列表, 屏幕输出)。"""
        buffer = io.StringIO()
        with mock.patch("xiaoyu.agent.Agent._sleep") as sleep, contextlib.redirect_stdout(buffer):
            try:
                agent.send(text)
            finally:
                self.waits = [call.args[0] for call in sleep.call_args_list]
        return self.waits, buffer.getvalue()

    def main_requests(self) -> int:
        """对话请求的次数（流式的那些；摘要调用不是流式）。"""
        return sum(1 for call in self.client.completions.calls if call.get("stream"))

    def assert_actionable(self, exc: BaseException) -> None:
        self.assertIsInstance(exc, compaction.ContextOverflow)
        for way_out in ("/rewind", "/clear", "/model"):
            self.assertIn(way_out, str(exc))
        self.assertIn(OVERFLOW_TEXT, str(exc.__cause__))
        #  宿主按分类决定重不重试：这一条重试无用，也不该再触发压缩
        verdict = errors.classify(exc)
        self.assertFalse(verdict.retryable)
        self.assertFalse(verdict.should_compact)

    def test_history_too_short_to_compact_fails_at_once(self) -> None:
        agent = self.build([RuntimeError(OVERFLOW_TEXT)])
        with self.assertRaises(compaction.ContextOverflow) as caught:
            self.send(agent, "你好")
        self.assert_actionable(caught.exception)
        self.assertEqual(self.main_requests(), 1)
        self.assertEqual(self.waits, [])

    def test_failing_summary_keeps_history_and_fails_at_once(self) -> None:
        #  主请求一次 + 摘要的降级阶梯（两档 × 便宜腿、主模型重放腿、主模型转写腿）
        agent = self.build(
            [RuntimeError(OVERFLOW_TEXT), *[ValueError("摘要后端坏了") for _ in range(6)]]
        )
        agent.messages.append({"role": "user", "content": "长任务"})
        for n in range(12):
            agent.messages.append({"role": "assistant", "content": f"进展 {n}：" + "阿" * 1500})
            agent.messages.append({"role": "user", "content": f"要求 {n}"})
        agent.messages.append({"role": "assistant", "content": "等下一步"})
        #  自动压缩此前已因连续失败暂停：发请求前不会先压
        agent.compactor.state.failures = 2
        before = list(agent.messages)
        with self.assertRaises(compaction.ContextOverflow) as caught:
            self.send(agent)
        self.assert_actionable(caught.exception)
        self.assertEqual(agent.messages[: len(before)], before)
        self.assertEqual(self.main_requests(), 1)
        self.assertEqual(len(self.client.completions.calls), 7)
        self.assertEqual(self.waits, [])

    def oversized_tail(self, agent) -> None:
        agent.messages.append({"role": "user", "content": "跑一遍全量检查"})
        agent.messages += parallel_batch("chk", 3, name="bash", output=log_lines(2200))
        agent.config.context_limit = 16_000

    def test_oversized_tail_is_tightened_then_resent(self) -> None:
        log = session_log.SessionLog(self.root / "log.jsonl")
        self.addCleanup(log.release)
        agent = self.build([RuntimeError(OVERFLOW_TEXT), [chunk(content="检查都过了")]], session_log=log)
        self.oversized_tail(agent)
        for message in agent.messages[1:]:
            log.append(message)
        untouched = [m["content"] for m in agent.messages if m.get("role") == "tool"]

        _, shown = self.send(agent)

        self.assertEqual(agent.last_assistant_text(), "检查都过了")
        self.assertEqual(self.main_requests(), 2)
        self.assertIn("收紧", shown)
        results = [m["content"] for m in agent.messages if m.get("role") == "tool"]
        for before, after in zip(untouched, results):
            self.assertLess(len(after), len(before))
            self.assertIn("中段已省略", after)
        assert_valid_sequence(self, agent.messages)
        #  第二次发出去的就是收紧后的历史
        resent = self.client.completions.calls[-1]["messages"]
        self.assertIn("中段已省略", str(resent))
        #  收紧记成一次带 replacement 的压缩：resume 重放出来的与内存里的一致
        self.assertEqual(session_log.load_messages(log.path), agent.messages[1:])
        self.assertFalse(session_log.has_orphan_compact(log.path))

    def test_second_overflow_with_nothing_left_to_shrink_stops(self) -> None:
        """收紧到底仍被拒：不再发第三次。"""
        agent = self.build([RuntimeError(OVERFLOW_TEXT) for _ in range(5)])
        self.oversized_tail(agent)
        with self.assertRaises(compaction.ContextOverflow):
            self.send(agent)
        #  每次重发之前历史都确实更小；缩到底之后不再发
        sizes = [
            tokens.estimate_messages(call["messages"])
            for call in self.client.completions.calls
        ]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertEqual(len(set(sizes)), len(sizes))
        self.assertLess(self.main_requests(), 5)

    def test_normal_compaction_never_tightens(self) -> None:
        """没有超限报错时，保留段里的大结果原样不动。"""
        from .test_agent_paths import GOOD_SUMMARY, text_response

        agent = self.build([text_response(GOOD_SUMMARY)])
        agent.messages.append({"role": "user", "content": "长任务"})
        for n in range(12):
            agent.messages.append({"role": "assistant", "content": f"进展 {n}：" + "阿" * 1500})
            agent.messages.append({"role": "user", "content": f"要求 {n}"})
        agent.messages += parallel_batch("chk", 3, name="bash", output=log_lines(2200))
        kept = [m for m in agent.messages if m.get("role") == "tool"]
        agent.config.context_limit = 16_000
        with contextlib.redirect_stdout(io.StringIO()):
            note = agent.maybe_compact()
        self.assertIn("已压缩", note)
        self.assertEqual([m for m in agent.messages if m.get("role") == "tool"], kept)


if __name__ == "__main__":
    unittest.main()
