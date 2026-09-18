"""护栏表与无护栏预设：表与 Config 一致、同意门、各层开关真的接到了执行点。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from unittest import mock

from xiaoyu import guardrails, mcp
from xiaoyu.cli import build_parser, resolve_folder_trust, resolve_guardrail_flags
from xiaoyu.config import Config
from xiaoyu.session_log import SessionLog
from xiaoyu.tools import Toolbox

from .test_agent_paths import AgentTestCase, call_fragment, chunk
from .test_subagent_knobs import FakeManager, remote


def _no_consent() -> mock._patch_dict:
    env = {k: v for k, v in os.environ.items() if k != guardrails.CONSENT_ENV}
    return mock.patch.dict(os.environ, env, clear=True)


class TableTest(unittest.TestCase):
    """表是唯一事实来源：每一层都是 Config 字段，且关值≠出厂值（否则"关"无从谈起）。"""

    def test_every_layer_is_a_config_field_with_a_different_default(self) -> None:
        names = {item.name for item in fields(Config)}
        cfg = Config(base_url="http://unused", model="m", workspace=Path("."))
        for layer in guardrails.LAYERS:
            self.assertIn(layer.field, names, layer.field)
            self.assertNotEqual(getattr(cfg, layer.field), layer.off_value, layer.field)

    def test_overrides_follow_table(self) -> None:
        self.assertEqual(
            guardrails.overrides(), {layer.field: layer.off_value for layer in guardrails.LAYERS}
        )

    def test_consent_reads_real_env_only(self) -> None:
        with _no_consent():
            self.assertFalse(guardrails.consented())
        with mock.patch.dict(os.environ, {guardrails.CONSENT_ENV: "0"}):
            self.assertFalse(guardrails.consented())
        with mock.patch.dict(os.environ, {guardrails.CONSENT_ENV: "1"}):
            self.assertTrue(guardrails.consented())

    def test_relaxed_and_notice_reflect_config(self) -> None:
        cfg = Config(base_url="http://unused", model="m", workspace=Path("."))
        self.assertEqual(guardrails.relaxed(cfg), [])
        cfg = replace(cfg, unguarded=True, **guardrails.overrides())
        self.assertEqual({layer.field for layer in guardrails.relaxed(cfg)}, set(guardrails.overrides()))
        text = guardrails.notice(cfg)
        self.assertIn(guardrails.FLAG, text)
        self.assertIn("bash 硬红线", text)
        self.assertIn(guardrails.TRUST_GATE, text)
        for kept in guardrails.KEPT:
            self.assertIn(kept, text, "仍生效的层必须在横幅里列出来")
        self.assertEqual(
            guardrails.snapshot(cfg),
            {"unguarded": True, "off": list(guardrails.overrides()), "trust_gate": True},
        )


class FlagTest(unittest.TestCase):
    """--unguarded 只在环境同意时生效；单项旗标互不牵连。"""

    @staticmethod
    def parse(*argv: str):
        args = build_parser().parse_args(list(argv))
        resolve_guardrail_flags(args)
        return args

    def test_unguarded_without_consent_is_an_error(self) -> None:
        with _no_consent(), self.assertRaisesRegex(ValueError, guardrails.CONSENT_ENV):
            self.parse("--unguarded", "任务")

    def test_unguarded_with_consent_applies_whole_table(self) -> None:
        with mock.patch.dict(os.environ, {guardrails.CONSENT_ENV: "1"}):
            args = self.parse("--unguarded", "任务")
        self.assertTrue(args.unguarded)
        self.assertTrue(args.yolo)
        self.assertIs(args.sandbox, False)
        self.assertIs(args.hardline, False)
        self.assertTrue(args.unattended)
        self.assertTrue(args.mcp_trust_changes)

    def test_plain_start_touches_nothing(self) -> None:
        with _no_consent():
            args = self.parse("任务")
        self.assertFalse(args.unguarded)
        self.assertFalse(args.yolo)
        self.assertIsNone(args.hardline)
        self.assertIsNone(args.unattended)
        self.assertIsNone(args.mcp_trust_changes)

    def test_unattended_alone_does_not_imply_yolo(self) -> None:
        args = self.parse("--unattended", "任务")
        self.assertTrue(args.unattended)
        self.assertFalse(args.yolo)

    def test_trust_gate_bypassed_without_recording(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch(
            "xiaoyu.folder_trust.record_decision"
        ) as record, mock.patch("xiaoyu.folder_trust.evaluate") as evaluate:
            decision = resolve_folder_trust(
                Path(tmp), grant=False, interactive=False, unguarded=True
            )
        self.assertTrue(decision.trusted)
        record.assert_not_called()
        evaluate.assert_not_called()


class ConfigEnvTest(unittest.TestCase):
    def test_env_switches_land_on_config(self) -> None:
        env = {
            "XIAOYU_HARDLINE": "0",
            "XIAOYU_UNATTENDED": "1",
            "XIAOYU_MCP_TRUST_CHANGES": "yes",
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, env):
            cfg = Config.from_env(workspace=Path(tmp))
        self.assertFalse(cfg.hardline)
        self.assertTrue(cfg.unattended)
        self.assertTrue(cfg.mcp_trust_changes)
        self.assertFalse(cfg.unguarded)

    def test_defaults_keep_every_layer_on(self) -> None:
        clean = {
            k: v
            for k, v in os.environ.items()
            if k not in ("XIAOYU_HARDLINE", "XIAOYU_UNATTENDED", "XIAOYU_MCP_TRUST_CHANGES")
        }
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, clean, clear=True):
            cfg = Config.from_env(workspace=Path(tmp))
        self.assertTrue(cfg.hardline)
        self.assertFalse(cfg.unattended)
        self.assertFalse(cfg.mcp_trust_changes)


class HardlineSwitchTest(AgentTestCase):
    """硬红线查 Config.hardline：默认拦，关掉后命令走到执行层。"""

    def test_on_by_default(self) -> None:
        box = Toolbox(self.config)
        result = box.run("bash", {"command": "rm -rf /"})
        self.assertIn("硬性拦截", result)

    def test_off_lets_command_reach_the_shell(self) -> None:
        self.config.hardline = False
        box = Toolbox(self.config)
        #  绝不真跑：把 shell argv 换成无害命令，只验证"没被硬红线截住"
        with mock.patch("xiaoyu.tools._shell_argv", return_value=["echo", "reached-shell"]):
            result = box.run("bash", {"command": "rm -rf /"})
        self.assertNotIn("硬性拦截", result)
        self.assertIn("reached-shell", result)


class UnattendedTest(AgentTestCase):
    """--unattended：--yolo 下原本 bypass-immune 的两处必问不再问。"""

    def _run(self, script, approver):
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            agent = self.build(script, approver=approver)
            with contextlib.redirect_stdout(io.StringIO()):
                agent.send("跑")
        return agent

    def test_escalated_bash_runs_without_asking(self) -> None:
        self.config.unattended = True
        asked: list[str] = []
        args = json.dumps(
            {"command": "echo up", "sandbox_permissions": "danger-full-access", "justification": "x"}
        )
        script = [[chunk(tool_calls=[call_fragment(0, "e1", "bash", args)])], [chunk(content="好")]]
        agent = self._run(script, lambda name, a: asked.append(name) or False)
        self.assertEqual(asked, [], "unattended 下升权不该弹确认")
        self.assertIn("up", agent.messages[-2]["content"])

    def test_exit_plan_mode_runs_without_asking(self) -> None:
        self.config.unattended = True
        asked: list[str] = []
        args = json.dumps({"plan": "1. 改 calc.py"})
        script = [
            [chunk(tool_calls=[call_fragment(0, "e1", "exit_plan_mode", args)])],
            [chunk(content="开工")],
        ]
        with mock.patch("xiaoyu.tools.sandbox.available", return_value=True):
            agent = self.build(script, approver=lambda name, a: asked.append(name) or False)
            agent.enter_plan_mode()
            with contextlib.redirect_stdout(io.StringIO()):
                agent.send("交计划")
        self.assertEqual(asked, [])
        self.assertFalse(agent.plan_mode, "无人值守下计划自动获批、退出 plan 态")

    def test_default_still_asks(self) -> None:
        asked: list[str] = []
        args = json.dumps(
            {"command": "echo up", "sandbox_permissions": "danger-full-access", "justification": "x"}
        )
        script = [[chunk(tool_calls=[call_fragment(0, "e1", "bash", args)])], [chunk(content="好")]]
        self._run(script, lambda name, a: asked.append(name) or False)
        self.assertEqual(asked, ["bash"])


class TrustContentTest(AgentTestCase):
    """.mcp.json 的 trustContent：该 server 的结果不套 <untrusted_content>。"""

    def test_spec_parses_trust_content_default_off(self) -> None:
        (self.root / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "a": {"command": "cmd", "trustContent": True},
                        "b": {"command": "cmd"},
                        "c": {"url": "https://example.com/mcp", "trustContent": True},
                    }
                }
            ),
            encoding="utf-8",
        )
        specs = {spec.name: spec for spec in mcp.load_server_specs(self.root)}
        self.assertTrue(specs["a"].trust_content)
        self.assertFalse(specs["b"].trust_content, "默认必须是关")
        self.assertTrue(specs["c"].trust_content)

    def _view(self):
        trusted = replace(remote("noc", "query"), trust_content=True)
        return FakeManager([trusted, remote("web", "fetch")])

    def test_use_tool_path_skips_wrapping_for_trusted_server(self) -> None:
        agent = self.build([])
        agent.toolbox = Toolbox(self.config, mcp_view=self._view())
        self.assertIsNone(agent._untrusted_source("use_tool", {"tool_name": "mcp__noc__query"}))
        self.assertEqual(
            agent._untrusted_source("use_tool", {"tool_name": "mcp__web__fetch"}), "mcp__web__fetch"
        )
        plain = agent._for_model("use_tool", {"tool_name": "mcp__noc__query"}, "数据", "数据")
        self.assertEqual(plain, "数据")
        wrapped = agent._for_model("use_tool", {"tool_name": "mcp__web__fetch"}, "数据", "数据")
        self.assertIn("<untrusted_content", wrapped)

    def test_full_schema_registration_honors_trust_content(self) -> None:
        self.config.mcp_tool_search = False
        box = Toolbox(self.config, mcp_view=self._view())
        self.assertFalse(box.get("mcp__noc__query").untrusted)
        self.assertTrue(box.get("mcp__web__fetch").untrusted)


class SessionTraceTest(AgentTestCase):
    """放开护栏的会话在前言里留痕；默认配置一字不记。"""

    def _events(self, log: SessionLog) -> list[dict]:
        lines = log.path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def test_unguarded_leaves_trace(self) -> None:
        self.config.unguarded = True
        self.config.hardline = False
        log = SessionLog.create("m", str(self.root), directory=self.root / "s")
        self.addCleanup(log.release)
        self.build([], session_log=log)
        traces = [item for item in self._events(log) if item.get("event") == guardrails.EVENT]
        self.assertEqual(len(traces), 1)
        self.assertTrue(traces[0]["unguarded"])
        self.assertIn("hardline", traces[0]["off"])

    def test_default_leaves_nothing(self) -> None:
        log = SessionLog.create("m", str(self.root), directory=self.root / "s")
        self.addCleanup(log.release)
        self.build([], session_log=log)
        self.assertFalse(
            [item for item in self._events(log) if item.get("event") == guardrails.EVENT]
        )


if __name__ == "__main__":
    unittest.main()
