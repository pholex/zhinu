"""服务部署预设：CLI 可见面与真实 HTTP 审批回路（不打模型网络）。"""

from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from tests.test_serve import ServeCase
from xiaoyu import cli, ui
from xiaoyu.serve import ServeConfig


class EnvironmentConfigTest(unittest.TestCase):
    def test_defaults_and_explicit_overrides(self):
        host = ServeConfig(root=Path.cwd())
        self.assertEqual((host.environment, host.approval, host.sandbox), ("host", "ask", None))
        isolated = ServeConfig(root=Path.cwd(), environment="sandbox")
        self.assertEqual((isolated.approval, isolated.sandbox), ("allow_all", False))
        tightened = replace(isolated, approval="ask", sandbox=True)
        self.assertEqual((tightened.approval, tightened.sandbox), ("ask", True))
        self.assertIsNone(isolated.sandbox_network)

    def test_invalid_environment_is_rejected(self):
        with self.assertRaises(ValueError):
            ServeConfig(root=Path.cwd(), environment="sandbx")


class EnvironmentCliTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch("xiaoyu.cli.load_dotenv"),
            mock.patch("xiaoyu.cli.resolve_folder_trust"),
            mock.patch("xiaoyu.serve.serve", return_value=0) as serve,
            mock.patch.object(ui, "_ENABLED", False),
            mock.patch.dict(os.environ, {"XIAOYU_SERVE_TOKEN": ""}),
            redirect_stdout(out), redirect_stderr(err),
        ):
            result = cli.serve_command(["--workspace", str(self.root), *args])
        self.assertEqual(result, 0, err.getvalue())
        return serve.call_args.args[0], out.getvalue().replace(str(self.root), "<workspace>")

    def test_presets_override_environment_but_legacy_invocation_survives(self):
        with mock.patch.dict(os.environ, {"XIAOYU_SANDBOX": "0"}):
            cfg, _ = self.run_cli("--environment", "host")
            self.assertEqual((cfg.sandbox, cfg.approval), (True, "ask"))
            legacy, _ = self.run_cli("--approval", "allow_all")
            self.assertEqual((legacy.sandbox, legacy.approval), (None, "allow_all"))
        with mock.patch.dict(os.environ, {"XIAOYU_SANDBOX": "1"}):
            cfg, _ = self.run_cli("--environment", "sandbox")
            self.assertEqual((cfg.sandbox, cfg.approval), (False, "allow_all"))
            ask, _ = self.run_cli("--environment", "sandbox", "--approval", "ask")
            self.assertEqual((ask.sandbox, ask.approval), (False, "ask"))

    def test_startup_snapshot(self):
        parts = []
        for args in ((), ("--environment", "host"), ("--environment", "sandbox"),
                     ("--environment", "sandbox", "--approval", "ask")):
            _, output = self.run_cli(*args)
            parts.append(f"--- {' '.join(args) or 'default'} ---\n{output}")
        actual = "".join(parts)
        golden = Path(__file__).parent / "fixtures" / "serve_environment.expected.txt"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            golden.write_text(actual, encoding="utf-8")
        self.assertEqual(actual, golden.read_text(encoding="utf-8", errors="replace"))

    def test_print_openapi_stays_json_only(self):
        with mock.patch("xiaoyu.serve.print_openapi", side_effect=lambda cfg: print('{}') or 0):
            #  这里不启动服务，run_cli 的服务捕获断言不适用。
            output = io.StringIO()
            with mock.patch("xiaoyu.cli.load_dotenv"), redirect_stdout(output):
                result = cli.serve_command([
                    "--workspace", str(self.root), "--environment", "sandbox", "--print-openapi",
                ])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue()), {})


class EnvironmentHttpTest(ServeCase):
    approval = None

    def test_preset_reaches_agent_without_disabling_other_guardrails(self):
        from xiaoyu.serve import _SessionRef, build_agent

        self.start("text: 完成\n", environment="sandbox")
        cfg = ServeConfig(root=self.root, environment="sandbox")
        with mock.patch.dict(os.environ, {
            "XIAOYU_SANDBOX": "1", "XIAOYU_SANDBOX_NETWORK": "0", "XIAOYU_HARDLINE": "1",
            "XIAOYU_SEARCH_SENSITIVE": "1",
        }):
            agent, manager = build_agent("preset-probe", self.root, cfg, _SessionRef())
        self.addCleanup(agent.session_log.release)
        self.assertIsNone(manager)
        self.assertFalse(agent.config.sandbox)
        self.assertTrue(agent.config.auto_approve)
        self.assertFalse(agent.config.sandbox_network)
        self.assertTrue(agent.config.hardline)
        self.assertTrue(agent.config.search_sensitive)

    def test_sandbox_keeps_deny_rules(self):
        from xiaoyu.permissions import user_rules_path

        self.start(
            'tool_call: {"name": "write_file", "arguments": {"path": "denied.txt", "content": "x"}}\n'
            '---\ntext: 已拒绝\n', environment="sandbox",
        )
        rules = user_rules_path()
        rules.parent.mkdir(parents=True, exist_ok=True)
        rules.write_text("deny write_file\n", encoding="utf-8")
        sid = self.new_session()
        response = self.client.post(f"/session/{sid}/prompt", json={"text": "写文件"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn("tool.denied", self.kinds(sid))
        self.assertFalse((self.root / "denied.txt").exists())

    def test_sandbox_executes_without_pending_and_can_tighten_approval(self):
        self.start(
            'tool_call: {"name": "write_file", "arguments": {"path": "done.txt", "content": "ok"}}\n'
            '---\ntext: 完成\n', environment="sandbox",
        )
        self.assertEqual(self.client.get("/health").json()["environment"], "sandbox")
        session_id = self.new_session()
        response = self.client.post(f"/session/{session_id}/prompt", json={"text": "写文件"})
        self.assertEqual(response.json()["detail"], "finished")
        self.assertEqual((self.root / "done.txt").read_text(), "ok")
        self.assertNotIn("permission.requested", self.kinds(session_id))
        agent = self.client.post("/agent", json={"name": "需确认", "config": {"approval": "ask"}})
        self.assertEqual(agent.status_code, 200, agent.text)
        sid = self.new_session(agent=agent.json()["agent_id"])
        self.client.post(f"/session/{sid}/prompt_async", json={"text": "写文件"})
        self._wait_for(sid, "waiting_for_approval")
        self.client.post(f"/session/{sid}/abort")

    def test_sandbox_keeps_auth_and_server_only_settings(self):
        self.token = "test-token"
        self.start("text: 完成\n", environment="sandbox")
        self.assertEqual(self.client.post("/session", json={}).status_code, 401)
        self.assertNotIn("environment", self.client.get("/health").json())
        for config in ({"environment": "host"}, {"mcp_servers": {"local": {"command": "echo"}}}):
            response = self.client.post("/agent", json={"name": "x", "config": config}, headers=self.headers())
            self.assertEqual(response.status_code, 400, response.text)
