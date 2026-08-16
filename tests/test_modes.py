"""交互模式（默认 / auto / plan）的测试。不打网络。

卡的行为：
- auto 档的放行判定矩阵（modes.auto_approves 是纯函数，逐行锁死）
- 走完整审批管线时 auto 真的免了确认框，且**该问的一个没少**
- auto 压不过 deny 规则、压不过危险命令、没有沙箱就不放行命令
- plan_mode 的向后兼容（升级成三档后，老调用方一行没改）
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import modes

from .test_agent_paths import AgentTestCase, call_fragment, chunk


def tool_call_turn(name: str, args: dict) -> list:
    return [chunk(tool_calls=[call_fragment(0, f"call_{name}", name, json.dumps(args))])]


def text_turn(text: str) -> list:
    return [chunk(content=text)]


# ---------- 表本身 ----------


class TableTest(unittest.TestCase):
    def test_cycle_order(self) -> None:
        """Shift-Tab 的循环顺序：默认 → auto → plan → 默认。"""
        seen = [modes.DEFAULT]
        for _ in range(3):
            seen.append(modes.next_mode(seen[-1]))
        self.assertEqual(seen, [modes.DEFAULT, modes.AUTO, modes.PLAN, modes.DEFAULT])

    def test_yolo_is_not_in_the_cycle(self) -> None:
        """--yolo 刻意不在循环里：没有沙箱兜底的那一档不该手滑就能进。"""
        self.assertNotIn("yolo", modes.CYCLE)
        self.assertEqual(set(modes.CYCLE), {modes.DEFAULT, modes.AUTO, modes.PLAN})

    def test_unknown_name_falls_back_to_default(self) -> None:
        """配置里写错档名不该炸主循环。"""
        self.assertEqual(modes.get("nonsense").name, modes.DEFAULT)
        self.assertEqual(modes.next_mode("nonsense"), modes.DEFAULT)

    def test_describe_tells_the_truth_without_sandbox(self) -> None:
        """静默降级是最坏的失败方式：没沙箱就得明说 bash 仍要确认。"""
        text = modes.describe(modes.AUTO, sandbox_ready=False)
        self.assertIn("沙箱不可用", text)
        self.assertIn("仍逐条确认", text)
        self.assertNotIn("沙箱不可用", modes.describe(modes.AUTO, sandbox_ready=True))

    def test_prompt_prefix_only_decorates_non_default(self) -> None:
        self.assertEqual(modes.prompt_prefix(modes.DEFAULT), "")
        self.assertIn("auto", modes.prompt_prefix(modes.AUTO))


# ---------- auto 的放行判定（纯函数） ----------


class AutoApprovesTest(unittest.TestCase):
    def approves(self, name: str, args: dict, *, outside=False, sandbox=True) -> bool:
        return modes.auto_approves(
            name, args, outside_workspace=outside, sandbox_ready=sandbox
        )

    def test_edits_inside_workspace_are_approved(self) -> None:
        for tool in ("write_file", "str_replace"):
            with self.subTest(tool=tool):
                self.assertTrue(self.approves(tool, {"path": "a.py"}))

    def test_edits_outside_workspace_still_ask(self) -> None:
        for tool in ("write_file", "str_replace"):
            with self.subTest(tool=tool):
                self.assertFalse(self.approves(tool, {"path": "~/.zshrc"}, outside=True))

    def test_plain_command_runs_in_sandbox(self) -> None:
        self.assertTrue(self.approves("bash", {"command": "pytest -q"}))

    def test_no_sandbox_no_auto_command(self) -> None:
        """沙箱是 auto 放行命令的前提，不是装饰。"""
        self.assertFalse(self.approves("bash", {"command": "pytest -q"}, sandbox=False))

    def test_edits_still_auto_without_sandbox(self) -> None:
        """沙箱没了，auto 只降级到"改文件免确认"，不是整档失效。"""
        self.assertTrue(self.approves("write_file", {"path": "a.py"}, sandbox=False))

    def test_dangerous_command_still_asks(self) -> None:
        """沙箱救不了工作区内的 rm -rf：写权限本来就是开的。"""
        for command in ("rm -rf build", "bash -lc 'rm -rf .'", "xargs rm -f"):
            with self.subTest(command=command):
                self.assertFalse(self.approves("bash", {"command": command}))

    def test_privileged_command_still_asks(self) -> None:
        for command in ("sudo apt install foo", "bash -lc 'sudo ls'", "su - root"):
            with self.subTest(command=command):
                self.assertFalse(self.approves("bash", {"command": command}))

    def test_empty_command_asks(self) -> None:
        self.assertFalse(self.approves("bash", {"command": "   "}))
        self.assertFalse(self.approves("bash", {}))

    def test_unknown_tools_fail_closed(self) -> None:
        """browser / MCP / 插件工具的副作用在沙箱管辖之外，一律照问。"""
        for name in ("browser", "mcp__server__write", "exit_plan_mode", "some_plugin_tool"):
            with self.subTest(name=name):
                self.assertFalse(self.approves(name, {"url": "https://example.com"}))

    def test_reads_outside_workspace_still_ask(self) -> None:
        """"读 ~/.ssh 会弹窗"这条线在 auto 档下也要保住。"""
        self.assertFalse(self.approves("read_file", {"path": "~/.ssh/id_rsa"}, outside=True))


# ---------- 走完整审批管线 ----------


class RecordingApprover:
    """记下每一次被问到的调用；一律批准（测的是"问没问"，不是"批没批"）。"""

    def __init__(self) -> None:
        self.asked: list[str] = []

    def __call__(self, name: str, args: dict) -> bool:
        self.asked.append(name)
        return True


class PipelineTest(AgentTestCase):
    """AgentTestCase 默认 auto_approve=True（--yolo），这里要的是真实审批路径。"""

    def setUp(self) -> None:
        super().setUp()
        self.config.auto_approve = False
        self.approver = RecordingApprover()

    def run_auto(self, script: list, *, sandbox_ready: bool = True):
        agent = self.build(script, approver=self.approver)
        agent.set_mode(modes.AUTO)
        #  沙箱可用性按平台而异（macOS 恒有 Seatbelt、CI 上的 Linux 常没有 bwrap），
        #  测试不能靠运气：两条路都要显式造出来
        with mock.patch("xiaoyu.sandbox.enabled", return_value=sandbox_ready):
            with contextlib.redirect_stdout(io.StringIO()):
                agent.send("干活")
        return agent

    def test_workspace_edit_is_not_asked(self) -> None:
        agent = self.run_auto(
            [tool_call_turn("write_file", {"path": "new.py", "content": "x = 1\n"}),
             text_turn("写好了")]
        )
        self.assertEqual(self.approver.asked, [])
        self.assertTrue((self.root / "new.py").exists())
        self.assertTrue(agent.trace[0]["ok"])

    def test_edit_outside_workspace_is_asked(self) -> None:
        outside = str(Path(self.tmp.name).parent / "escape.py")
        self.run_auto(
            [tool_call_turn("write_file", {"path": outside, "content": "x = 1\n"}),
             text_turn("好了")]
        )
        self.assertEqual(self.approver.asked, ["write_file"])

    def test_sandboxed_command_is_not_asked(self) -> None:
        self.run_auto(
            [tool_call_turn("bash", {"command": "echo hello"}), text_turn("跑完了")]
        )
        self.assertEqual(self.approver.asked, [])

    def test_command_without_sandbox_is_asked(self) -> None:
        self.run_auto(
            [tool_call_turn("bash", {"command": "echo hello"}), text_turn("跑完了")],
            sandbox_ready=False,
        )
        self.assertEqual(self.approver.asked, ["bash"])

    def test_dangerous_command_is_asked(self) -> None:
        self.run_auto(
            [tool_call_turn("bash", {"command": "rm -rf build"}), text_turn("好")]
        )
        self.assertEqual(self.approver.asked, ["bash"])

    def test_deny_rule_beats_auto(self) -> None:
        """deny 是 bypass-immune：auto 档同样不放行，且根本不该问用户。"""
        from xiaoyu.permissions import parse_rule

        agent = self.build(
            [tool_call_turn("bash", {"command": "curl http://example.com"}), text_turn("好")],
            approver=self.approver,
        )
        agent.permissions.rules.append(parse_rule("deny bash(curl *)"))
        agent.set_mode(modes.AUTO)
        with mock.patch("xiaoyu.sandbox.enabled", return_value=True):
            with contextlib.redirect_stdout(io.StringIO()):
                agent.send("下载点东西")
        self.assertEqual(self.approver.asked, [])
        self.assertEqual([t["output"] for t in agent.trace], ["DENIED_BY_RULE"])

    def test_default_mode_still_asks_everything(self) -> None:
        """没切档就什么都没变——auto 的放行只在 auto 档里成立。"""
        agent = self.build(
            [tool_call_turn("write_file", {"path": "new.py", "content": "x = 1\n"}),
             text_turn("写好了")],
            approver=self.approver,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("写个文件")
        self.assertEqual(self.approver.asked, ["write_file"])


# ---------- 向后兼容 ----------


class BackCompatTest(AgentTestCase):
    def test_plan_mode_property_tracks_mode(self) -> None:
        agent = self.build([])
        self.assertFalse(agent.plan_mode)
        agent.set_mode(modes.PLAN)
        self.assertTrue(agent.plan_mode)
        agent.set_mode(modes.AUTO)
        self.assertFalse(agent.plan_mode)

    def test_enter_leave_plan_mode_still_work(self) -> None:
        agent = self.build([])
        agent.enter_plan_mode()
        self.assertEqual(agent.mode, modes.PLAN)
        self.assertIn("已经在", agent.enter_plan_mode())
        agent.leave_plan_mode()
        self.assertEqual(agent.mode, modes.DEFAULT)
        self.assertIn("不在", agent.leave_plan_mode())

    def test_reset_clears_plan_but_keeps_auto(self) -> None:
        """plan 的规则注入在历史里，历史一清就必须退档；auto 没这个依赖，
        /clear 清的是对话不是授权偏好。"""
        agent = self.build([])
        agent.set_mode(modes.PLAN)
        agent.reset()
        self.assertEqual(agent.mode, modes.DEFAULT)

        agent.set_mode(modes.AUTO)
        agent.reset()
        self.assertEqual(agent.mode, modes.AUTO)

    def test_config_mode_seeds_the_agent(self) -> None:
        """--mode auto 起手（无人值守场景靠它，不必 /allow 也不必 --yolo）。"""
        self.config.mode = modes.AUTO
        self.assertEqual(self.build([]).mode, modes.AUTO)


if __name__ == "__main__":
    unittest.main()
