"""plan mode（只读规划态）的测试。不打网络。

卡的行为：白名单拦截（含 --yolo bypass-immune）、exit_plan_mode 动态可见性、
审批必经（--yolo 也要问）、进出注入的历史说明、reset 清态。
"""

from __future__ import annotations

import json
import unittest

from xiaoyu import modes
from xiaoyu.agent import (
    PLAN_MODE_ENTER_NOTE,
    PLAN_MODE_LEAVE_NOTE,
    PLAN_MODE_TOOLS,
)

from .test_agent_paths import AgentTestCase, call_fragment, chunk


def tool_call_turn(name: str, args: dict) -> list:
    """一轮"模型调用一个工具"的假流。"""
    return [
        chunk(
            tool_calls=[call_fragment(0, f"call_{name}", name, json.dumps(args))]
        )
    ]


def text_turn(text: str) -> list:
    return [chunk(content=text)]


class GateTest(AgentTestCase):
    """AgentTestCase 的 config 是 auto_approve=True（--yolo）——
    正好证明拦截与审批都是 bypass-immune。"""

    def test_write_tool_blocked_in_plan_mode(self):
        agent = self.build(
            [tool_call_turn("bash", {"command": "echo should-not-run"}), text_turn("好的")]
        )
        agent.enter_plan_mode()
        agent.send("规划一下")
        denied = [t for t in agent.trace if t["output"] == "DENIED_PLAN_MODE"]
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0]["tool"], "bash")
        #  拒因回灌给模型：指路 exit_plan_mode
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("plan mode", tool_msgs[-1]["content"])
        self.assertIn("exit_plan_mode", tool_msgs[-1]["content"])

    def test_readonly_tool_allowed_in_plan_mode(self):
        agent = self.build(
            [tool_call_turn("read_file", {"path": "calc.py"}), text_turn("看完了")]
        )
        agent.enter_plan_mode()
        agent.send("看看 calc.py")
        executed = [t for t in agent.trace if t["tool"] == "read_file"]
        self.assertEqual(len(executed), 1)
        self.assertTrue(executed[0]["ok"])

    def test_whitelist_matches_registered_readonly_tools(self):
        #  白名单不该出现"根本不存在的工具名"这种漂移（exit_plan_mode/skill/
        #  explore/web_search 是条件注册，不在此列强求）
        agent = self.build([])
        registered = set(agent.toolbox.names())
        for name in ("read_file", "grep", "list_files", "update_plan"):
            self.assertIn(name, PLAN_MODE_TOOLS)
            self.assertIn(name, registered)


class ExitToolTest(AgentTestCase):
    def test_exit_tool_visible_only_in_plan_mode(self):
        agent = self.build([])
        names = [schema["function"]["name"] for schema in agent.toolbox.schemas()]
        self.assertNotIn("exit_plan_mode", names)
        agent.enter_plan_mode()
        names = [schema["function"]["name"] for schema in agent.toolbox.schemas()]
        self.assertIn("exit_plan_mode", names)

    def test_exit_requires_approval_even_with_yolo(self):
        """auto_approve=True 下 approver 仍被调用；拒绝则留在 plan mode。"""
        asked: list[str] = []

        def approver(name: str, args: dict):
            asked.append(name)
            return "计划太粗，再想想"

        agent = self.build(
            [
                tool_call_turn("exit_plan_mode", {"plan": "1. 改 calc.py"}),
                text_turn("好，我再调研"),
            ],
            approver=approver,
        )
        agent.enter_plan_mode()
        agent.send("交计划")
        self.assertEqual(asked, ["exit_plan_mode"])
        self.assertTrue(agent.plan_mode)
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("计划太粗", tool_msgs[-1]["content"])

    def test_exit_approved_leaves_plan_mode(self):
        agent = self.build(
            [
                tool_call_turn("exit_plan_mode", {"plan": "1. 改 calc.py\n2. 跑测试"}),
                text_turn("开始执行"),
            ],
            approver=lambda name, args: True,
        )
        agent.enter_plan_mode()
        agent.send("交计划")
        self.assertFalse(agent.plan_mode)
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("批准", tool_msgs[-1]["content"])

    def test_exit_with_empty_plan_exits_with_note(self):
        #  plan 文件和参数都空：用户在审批框看到的就是空的，批了就退，
        #  但明说没有计划（不硬拦——用户已经放行了）
        agent = self.build(
            [tool_call_turn("exit_plan_mode", {"plan": "  "}), text_turn("好")],
            approver=lambda name, args: True,
        )
        agent.enter_plan_mode()
        agent.send("交计划")
        self.assertFalse(agent.plan_mode)
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("空的", tool_msgs[-1]["content"])


class ToggleTest(AgentTestCase):
    def test_enter_and_leave_inject_user_notes(self):
        agent = self.build([])
        agent.enter_plan_mode()
        #  进场说明按会话格式化（含 plan 文件路径），模板是它的骨架
        self.assertEqual(
            agent.messages[-1]["content"],
            PLAN_MODE_ENTER_NOTE.format(plan_file=agent.plan_file),
        )
        self.assertIn(str(agent.plan_file), agent.messages[-1]["content"])
        self.assertEqual(agent.messages[-1]["role"], "user")
        agent.leave_plan_mode()
        self.assertEqual(agent.messages[-1]["content"], PLAN_MODE_LEAVE_NOTE)
        self.assertFalse(agent.plan_mode)

    def test_toggle_is_idempotent(self):
        agent = self.build([])
        before = len(agent.messages)
        self.assertIn("不在", agent.leave_plan_mode())
        self.assertEqual(len(agent.messages), before)  # 无变化不注入
        agent.enter_plan_mode()
        self.assertIn("已经在", agent.enter_plan_mode())

    def test_reset_clears_plan_mode(self):
        agent = self.build([])
        agent.enter_plan_mode()
        agent.reset()
        self.assertFalse(agent.plan_mode)

    def test_exit_returns_to_mode_before_plan(self):
        """退出 plan 回到进 plan 前的那档：从 auto 进的回 auto，从确认档进的回确认档。"""
        for before in (modes.AUTO, modes.DEFAULT):
            with self.subTest(before=before):
                agent = self.build(
                    [tool_call_turn("exit_plan_mode", {"plan": "1. 干活"}), text_turn("开始")],
                    approver=lambda name, args: True,
                )
                agent.set_mode(before)
                agent.set_mode(modes.PLAN)
                agent.send("交计划")
                self.assertEqual(agent.mode, before)
        #  /clear（reset）同样回到进 plan 前的档
        agent = self.build([])
        agent.set_mode(modes.AUTO)
        agent.enter_plan_mode()
        agent.reset()
        self.assertEqual(agent.mode, modes.AUTO)

    def test_exit_without_record_falls_back_to_configured_then_initial(self):
        """restore 直接落在 plan（没有"进 plan 前"记录）：回配置的起始档；
        配置起手就是 plan 则回出厂档。"""
        agent = self.build([])
        agent.adopt_mode(modes.PLAN)
        agent.reset()
        self.assertEqual(agent.mode, modes.get(self.config.mode).name)
        self.config.mode = modes.PLAN
        agent = self.build([])
        agent.adopt_mode(modes.PLAN)
        agent.reset()
        self.assertEqual(agent.mode, modes.INITIAL)


class PlanFileTest(AgentTestCase):
    """plan.md 文件化：唯一可编辑、免确认、exit 以磁盘为准。"""

    def approver_must_not_be_called(self, name, args):
        self.fail(f"plan 文件专线不该走审批：{name} {args}")

    def test_enter_seeds_empty_file_and_never_truncates(self):
        agent = self.build([])
        agent.enter_plan_mode()
        self.assertTrue(agent.plan_file.exists())
        self.assertEqual(agent.plan_file.read_text(encoding="utf-8"), "")
        agent.leave_plan_mode()
        agent.plan_file.write_text("# 上次的计划\n", encoding="utf-8")
        agent.enter_plan_mode()
        self.assertEqual(agent.plan_file.read_text(encoding="utf-8"), "# 上次的计划\n")

    def test_plan_file_editable_without_approval(self):
        agent = self.build(
            [
                tool_call_turn(
                    "write_file", {"path": "PLAN", "content": "# 计划\n1. 改 calc\n"}
                ),
                text_turn("写好了"),
            ],
            approver=self.approver_must_not_be_called,
        )
        agent.enter_plan_mode()
        #  用真实 plan_file 路径重写脚本参数（AgentTestCase 无会话文件 →
        #  plan_file 在工作区 .xiaoyu/ 下）
        script = self.client.completions.script
        script[0][0].choices[0].delta.tool_calls[0].function.arguments = json.dumps(
            {"path": str(agent.plan_file), "content": "# 计划\n1. 改 calc\n"}
        )
        agent.send("把计划写下来")
        executed = [t for t in agent.trace if t["tool"] == "write_file"]
        self.assertEqual(len(executed), 1)
        self.assertTrue(executed[0]["ok"], executed[0]["output"])
        self.assertEqual(
            agent.plan_file.read_text(encoding="utf-8"), "# 计划\n1. 改 calc\n"
        )

    def test_other_files_still_rejected_with_plan_path(self):
        agent = self.build(
            [
                tool_call_turn("write_file", {"path": "calc.py", "content": "x"}),
                text_turn("好"),
            ]
        )
        agent.enter_plan_mode()
        agent.send("改 calc")
        denied = [t for t in agent.trace if t["output"] == "DENIED_PLAN_MODE"]
        self.assertEqual(len(denied), 1)
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn(str(agent.plan_file), tool_msgs[-1]["content"])

    def test_exit_uses_disk_content_over_argument(self):
        seen: dict = {}

        def approver(name, args):
            seen.update(args)
            return True

        agent = self.build(
            [tool_call_turn("exit_plan_mode", {"plan": "上下文里的旧版本"}), text_turn("好")],
            approver=approver,
        )
        agent.enter_plan_mode()
        agent.plan_file.write_text("# 磁盘上的计划\n", encoding="utf-8")
        agent.send("交计划")
        self.assertFalse(agent.plan_mode)
        #  审批框与 handler 拿到的都是文件真身，不是模型参数里的旧文本
        self.assertEqual(seen.get("plan"), "# 磁盘上的计划\n")
        tool_msgs = [m for m in agent.messages if m.get("role") == "tool"]
        self.assertIn("批准", tool_msgs[-1]["content"])
        self.assertIn(str(agent.plan_file), tool_msgs[-1]["content"])

    def test_turn_starts_skips_formatted_note(self):
        from xiaoyu.agent import SYNTHETIC_USER_TEXTS
        from xiaoyu.session_log import turn_starts

        agent = self.build([])
        agent.enter_plan_mode()
        messages = [m for m in agent.messages if m.get("role") == "user"]
        self.assertEqual(turn_starts(messages, SYNTHETIC_USER_TEXTS), [])


if __name__ == "__main__":
    unittest.main()
