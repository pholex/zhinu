"""microcompact（清理旧工具输出）与压缩摘要 prompt 的测试。不打网络。"""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest import mock

from xiaoyu import media
from xiaoyu.compaction import (
    SUMMARY_INSTRUCTION,
    TOOL_IMAGE_HIGH_WATER,
    TOOL_IMAGE_KEEP,
    age_tool_images,
    merge_consecutive_users,
    microcompact,
)

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

    def test_update_mode_declared_in_both_prompts(self):
        """迭代压缩的增量更新指令：旧摘要要"保留并更新"而不是再压一遍——
        没有这段时多次压缩是双重有损（摘要的摘要），事实会逐轮蒸发。
        两条腿（渲染转写 / 前缀重放）都必须带。"""
        from xiaoyu.compaction import PREFIX_SUMMARY_INSTRUCTION

        for prompt in (SUMMARY_INSTRUCTION, PREFIX_SUMMARY_INSTRUCTION):
            self.assertIn("持续维护的状态文档", prompt)
            self.assertIn("增量更新", prompt)
            self.assertIn("确已失效的条目才可以删", prompt)
        #  各自引用的"上一轮摘要"定位方式要和真实输入形态对上：
        #  渲染转写腿用【此前的压缩摘要】节标签，前缀重放腿用分界标记原文开头
        self.assertIn("此前的压缩摘要", SUMMARY_INSTRUCTION)
        self.assertIn("以下是另一个模型对本会话早期内容做的交接摘要", PREFIX_SUMMARY_INSTRUCTION)


def tool_image(ref: str) -> dict:
    """工具回图那条 user 消息（带私有标记，形状同 Agent._attach_media）。"""
    return {
        "role": "user",
        "content": [
            media.text_part("[上一步的工具返回了 1 张图片，如下]"),
            media.image_part(ref),
        ],
        media.TOOL_MEDIA_KEY: True,
    }


def screenshot_loop(count: int) -> list[dict]:
    """浏览器截图循环：每轮一次工具调用 + 一条工具回图。"""
    messages: list[dict] = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "任务"},
    ]
    for index in range(count):
        messages += tool_exchange(f"c{index}", "browser_screenshot", "图片见下一条")
        messages.append(tool_image(f"xiaoyu-media://shot{index}.png"))
    return messages


def image_urls(messages: list[dict]) -> list[str]:
    return [
        part["image_url"]["url"]
        for message in messages
        for part in media.images_of(message.get("content"))
    ]


class ToolImageAgingTest(unittest.TestCase):
    """工具截图批量老化：超高水位一次剔到只剩最新几张，用户贴图不动。"""

    def test_over_high_water_keeps_latest(self):
        messages = screenshot_loop(TOOL_IMAGE_HIGH_WATER + 1)
        result, aged = age_tool_images(messages)
        self.assertEqual(aged, TOOL_IMAGE_HIGH_WATER + 1 - TOOL_IMAGE_KEEP)
        #  留下的是最新的几张，顺序不变
        expected = [f"xiaoyu-media://shot{i}.png" for i in range(7 - TOOL_IMAGE_KEEP, 7)]
        self.assertEqual(image_urls(result), expected)
        placeholders = [m for m in result if "重新截图" in media.text_of(m.get("content"))]
        self.assertEqual(len(placeholders), aged)
        #  消息结构不变：条数、角色、标记都在（老化只换部件，不删消息）
        self.assertEqual([m.get("role") for m in result], [m.get("role") for m in messages])
        self.assertTrue(all(m.get(media.TOOL_MEDIA_KEY) for m in placeholders))

    def test_at_high_water_untouched(self):
        """未达高水位不动（返回原列表）：逐张剔会让 prompt 缓存每轮断在被剔处。"""
        messages = screenshot_loop(TOOL_IMAGE_HIGH_WATER)
        result, aged = age_tool_images(messages)
        self.assertEqual(aged, 0)
        self.assertIs(result, messages)

    def test_batch_then_stable(self):
        """剔完一批后要再攒到高水位以上才动第二次——不是来一张剔一张。"""
        messages, aged = age_tool_images(screenshot_loop(TOOL_IMAGE_HIGH_WATER + 1))
        self.assertTrue(aged)
        for index in range(TOOL_IMAGE_HIGH_WATER - TOOL_IMAGE_KEEP):
            messages = [*messages, tool_image(f"xiaoyu-media://more{index}.png")]
            same, again = age_tool_images(messages)
            self.assertEqual(again, 0)
            self.assertIs(same, messages)

    def test_user_pasted_images_never_aged(self):
        messages = screenshot_loop(TOOL_IMAGE_HIGH_WATER + 3)
        pasted = {
            "role": "user",
            "content": [media.text_part("看这张"), media.image_part("xiaoyu-media://mine.png")],
        }
        messages.insert(2, pasted)
        result, _ = age_tool_images(messages)
        self.assertIn("xiaoyu-media://mine.png", image_urls(result))
        self.assertEqual(result[2], pasted)

    def test_user_images_do_not_count_toward_high_water(self):
        messages = screenshot_loop(TOOL_IMAGE_HIGH_WATER)
        for index in range(5):
            messages.append(
                {"role": "user", "content": [media.image_part(f"xiaoyu-media://p{index}.png")]}
            )
        result, aged = age_tool_images(messages)
        self.assertEqual(aged, 0)
        self.assertIs(result, messages)

    def test_microcompact_ages_tail_images_too(self):
        """keep_recent 尾部的图同样老化：尾部锁住的图会让摘要压缩收效甚微、触发熔断。"""
        messages = screenshot_loop(TOOL_IMAGE_HIGH_WATER + 1)
        result, cleared, _ = microcompact(messages, keep_recent=len(messages))
        self.assertEqual(len(image_urls(result)), TOOL_IMAGE_KEEP)
        self.assertEqual(cleared, TOOL_IMAGE_HIGH_WATER + 1 - TOOL_IMAGE_KEEP)

    def test_idempotent(self):
        once, _ = age_tool_images(screenshot_loop(TOOL_IMAGE_HIGH_WATER + 1))
        twice, aged = age_tool_images(once)
        self.assertEqual(aged, 0)
        self.assertEqual(once, twice)

    def test_merge_keeps_marker_only_when_all_images_are_tool_images(self):
        head = {"role": "user", "content": "任务 + 摘要"}
        merged = merge_consecutive_users([head, tool_image("xiaoyu-media://a.png")])
        self.assertTrue(merged[0].get(media.TOOL_MEDIA_KEY))
        pasted = {"role": "user", "content": [media.image_part("xiaoyu-media://mine.png")]}
        mixed = merge_consecutive_users([pasted, tool_image("xiaoyu-media://a.png")])
        self.assertFalse(mixed[0].get(media.TOOL_MEDIA_KEY))


class ToolMediaMarkerOffWireTest(unittest.TestCase):
    """私有标记止于内核边界：三种协议的出网载荷里都不能出现。"""

    MESSAGES = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "任务"},
        tool_image("xiaoyu-media://gone.png"),
    ]

    def test_chat_payload(self):
        from xiaoyu import responses

        from .test_responses import FakeClient, FakeResponses

        inner = FakeClient(FakeResponses())
        responses.wrap(inner, ()).chat.completions.create(model="m", messages=list(self.MESSAGES))
        sent = inner.chat.completions.calls[0]["messages"]
        self.assertNotIn(media.TOOL_MEDIA_KEY, str(sent))

    def test_messages_and_responses_payloads(self):
        from xiaoyu import messages as msgs
        from xiaoyu import responses

        anthropic = msgs.to_request("m", list(self.MESSAGES), None, False, {})
        self.assertNotIn(media.TOOL_MEDIA_KEY, str(anthropic))
        native = responses.to_request("m", list(self.MESSAGES), None, {})
        self.assertNotIn(media.TOOL_MEDIA_KEY, str(native))


class AgentToolImageAgingTest(AgentTestCase):
    """工具回图入历史时带标记，攒过高水位当场批量老化（不必等压缩阈值）。"""

    def test_attach_marks_and_ages(self):
        from xiaoyu import providers
        from xiaoyu.agent import Agent
        from xiaoyu.tools import Toolbox

        registry = providers.Registry(
            [providers.Provider("gateway", "", "", (), "网关", (), ("*",))],
            clients={"gateway": mock.MagicMock()},
        )
        agent = Agent(self.config, Toolbox(self.config), registry=registry)
        for index in range(TOOL_IMAGE_HIGH_WATER + 1):
            ref = f"xiaoyu-media://shot{index}.png"
            agent.toolbox.take_media = lambda ref=ref: [media.image_part(ref)]  # type: ignore[method-assign]
            with contextlib.redirect_stdout(io.StringIO()):
                agent._attach_media()
            self.assertTrue(agent.messages[-1].get(media.TOOL_MEDIA_KEY))
        self.assertEqual(len(image_urls(agent.messages)), TOOL_IMAGE_KEEP)
        self.assertIn(f"xiaoyu-media://shot{TOOL_IMAGE_HIGH_WATER}.png", image_urls(agent.messages))

    def test_restore_from_session_log_re_ages(self):
        """老化不写回会话日志：resume 重放出全部原图，接回时要再老化一次。"""
        from xiaoyu import providers, session_log
        from xiaoyu.agent import Agent
        from xiaoyu.tools import Toolbox

        log = session_log.SessionLog.create("m", str(self.root), directory=self.root / "sessions")
        self.addCleanup(log.release)
        pasted = {
            "role": "user",
            "content": [media.text_part("看这张"), media.image_part("xiaoyu-media://mine.png")],
        }
        history = screenshot_loop(TOOL_IMAGE_HIGH_WATER + 1)[1:]  # 去掉 system
        history.insert(1, pasted)
        for message in history:
            log.append(message)
        loaded = session_log.load_messages(log.path)
        #  私有标记随消息原样落盘：重放后照样认得出工具图
        self.assertEqual(
            sum(1 for m in loaded if m.get(media.TOOL_MEDIA_KEY)), TOOL_IMAGE_HIGH_WATER + 1
        )

        registry = providers.Registry(
            [providers.Provider("gateway", "", "", (), "网关", (), ("*",))],
            clients={"gateway": mock.MagicMock()},
        )
        agent = Agent(self.config, Toolbox(self.config), registry=registry)
        with contextlib.redirect_stdout(io.StringIO()):
            agent.restore(loaded, copy=False)
        urls = image_urls(agent.messages)
        #  工具图只剩最新几张，用户贴的图不动也不计数
        self.assertEqual(len(urls), TOOL_IMAGE_KEEP + 1)
        self.assertIn("xiaoyu-media://mine.png", urls)
        self.assertIn(f"xiaoyu-media://shot{TOOL_IMAGE_HIGH_WATER}.png", urls)
        placeholders = [m for m in agent.messages if "重新截图" in media.text_of(m.get("content"))]
        self.assertEqual(len(placeholders), TOOL_IMAGE_HIGH_WATER + 1 - TOOL_IMAGE_KEEP)


if __name__ == "__main__":
    unittest.main()
