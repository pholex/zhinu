"""microcompact（清理旧工具输出）与压缩摘要 prompt 的测试。不打网络。"""

from __future__ import annotations

import contextlib
import io
import unittest

from xiaoyu.compaction import SUMMARY_INSTRUCTION, microcompact

from .test_agent_paths import AgentTestCase


def tool_exchange(call_id: str, name: str, output: str) -> list[dict]:
    """一对 assistant(tool_calls) + tool 结果消息。"""
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}
            ],
        },
        {"role": "tool", "tool_call_id": call_id, "content": output},
    ]


class MicrocompactTest(unittest.TestCase):
    def setUp(self):
        self.big = "x" * 2000

    def test_clears_old_big_whitelisted_outputs(self):
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "任务"},
            *tool_exchange("c1", "read_file", self.big),
            *tool_exchange("c2", "grep", self.big),
            {"role": "assistant", "content": "结论"},
        ]
        result, cleared, saved = microcompact(messages, keep_recent=1)
        self.assertEqual(cleared, 2)
        self.assertGreater(saved, 3000)
        for message in result:
            if message.get("role") == "tool":
                self.assertIn("已清理", message["content"])
                self.assertIn("重新调用", message["content"])
        #  消息结构不变：条数、角色、tool_call_id 全部原样
        self.assertEqual(len(result), len(messages))
        self.assertEqual(
            [m.get("role") for m in result], [m.get("role") for m in messages]
        )

    def test_recent_messages_protected(self):
        messages = [
            {"role": "system", "content": "s"},
            *tool_exchange("c1", "read_file", self.big),
        ]
        #  keep_recent 覆盖住 tool 消息：不清
        result, cleared, _ = microcompact(messages, keep_recent=2)
        self.assertEqual(cleared, 0)
        self.assertEqual(result[-1]["content"], self.big)

    def test_small_and_nonwhitelisted_outputs_kept(self):
        messages = [
            {"role": "system", "content": "s"},
            *tool_exchange("c1", "read_file", "短输出"),
            *tool_exchange("c2", "explore", self.big),  # 蒸馏产物，不清
            {"role": "user", "content": "继续"},
        ]
        result, cleared, _ = microcompact(messages, keep_recent=1)
        self.assertEqual(cleared, 0)
        self.assertEqual(result[2]["content"], "短输出")
        self.assertEqual(result[4]["content"], self.big)

    def test_idempotent(self):
        messages = [
            {"role": "system", "content": "s"},
            *tool_exchange("c1", "bash", self.big),
            {"role": "user", "content": "继续"},
        ]
        once, cleared_first, _ = microcompact(messages, keep_recent=1)
        twice, cleared_second, saved_second = microcompact(once, keep_recent=1)
        self.assertEqual(cleared_first, 1)
        self.assertEqual(cleared_second, 0, "占位符不该被再次清理")
        self.assertEqual(saved_second, 0)
        self.assertEqual(once, twice)

    def test_original_list_not_mutated(self):
        messages = [
            {"role": "system", "content": "s"},
            *tool_exchange("c1", "grep", self.big),
            {"role": "user", "content": "继续"},
        ]
        microcompact(messages, keep_recent=1)
        self.assertEqual(messages[2]["content"], self.big)


class AgentMicrocompactIntegrationTest(AgentTestCase):
    """maybe_compact 的分层：micro 够用就不花摘要调用。"""

    def test_micro_enough_skips_summary_call(self):
        agent = self.build([])  # 空脚本：任何模型调用都会 AssertionError
        agent.messages.append({"role": "user", "content": "任务"})
        for index in range(6):
            agent.messages += tool_exchange(f"c{index}", "read_file", "x" * 8000)
        agent.messages.append({"role": "assistant", "content": "结论"})

        #  把阈值压到"清理后就能低于"的位置：micro 后大约剩几百 token。
        #  keep_recent 也压小，否则默认保护最近 8 条、6 组交换大半清不掉
        agent.config.context_limit = 20_000
        agent.compactor.context_limit = 20_000
        agent.config.keep_recent = 2
        agent.compactor.keep_recent = 2

        with contextlib.redirect_stdout(io.StringIO()):
            note = agent.maybe_compact()

        self.assertIsNotNone(note)
        self.assertIn("microcompact", note)
        #  假 client 脚本为空却没炸 → 全程没有发起摘要调用
        cleaned = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertTrue(all("已清理" in m["content"] for m in cleaned[:-1]))

    def test_full_compaction_still_runs_when_micro_not_enough(self):
        from .test_agent_paths import GOOD_SUMMARY, text_response

        agent = self.build([text_response(GOOD_SUMMARY)])
        agent.messages.append({"role": "user", "content": "任务"})
        #  大量 assistant 正文（micro 清不掉），必须走到全量摘要
        for index in range(40):
            agent.messages.append({"role": "user", "content": f"要求 {index}" + "阿" * 200})
            agent.messages.append({"role": "assistant", "content": "回答" + "阿" * 400})

        agent.config.context_limit = 10_000
        agent.compactor.context_limit = 10_000

        with contextlib.redirect_stdout(io.StringIO()):
            note = agent.maybe_compact()

        self.assertIsNotNone(note)
        self.assertIn("已压缩", note)


class SummaryInstructionTest(unittest.TestCase):
    """锁住压缩 prompt 的关键结构要素。"""

    def test_no_tools_preamble_first(self):
        first_line = SUMMARY_INSTRUCTION.splitlines()[0]
        self.assertIn("没有任何工具", first_line)

    def test_fixed_sections_present(self):
        for section in ("任务目标", "用户消息清单", "错误与修复", "未完成事项", "当前状态"):
            self.assertIn(section, SUMMARY_INSTRUCTION)

    def test_no_speculation_rule_kept(self):
        self.assertIn("不要推测", SUMMARY_INSTRUCTION)

    def test_cost_asymmetry_declared(self):
        #  错误代价声明（反垃圾 prompt 要素）：漏写可补、编造不可核对
        self.assertIn("代价不对等", SUMMARY_INSTRUCTION)
        self.assertIn("宁可短，不可编", SUMMARY_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
