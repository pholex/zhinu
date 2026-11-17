"""模型切换只补诊断事实，不改变重试、降级与回探行为。"""

import contextlib
import io
from unittest import mock

import openai

from xiaoyu.providers import Provider, Registry

from .test_agent_paths import AgentTestCase, FakeClient, chunk, usage_chunk
from . import test_errors
from .test_errors import _response, rate_limit_error


class ModelSwitchTest(AgentTestCase):
    def cross_provider(self, primary_script, backup_script):
        self.config.model = "main-model"
        agent = self.build([])
        primary, backup = FakeClient(primary_script), FakeClient(backup_script)
        agent.registry = Registry([
            Provider("primary", "http://unused", "", ("main-model",), "主服务"),
            Provider("gateway", "http://unused", "", (), "备用服务"),
        ], clients={"primary": primary, "gateway": backup})
        agent.session_log = test_errors.RequestLogTest.Log()
        return agent, primary, backup

    def changes(self, agent):
        return [fields for kind, fields in agent.session_log.events if kind == "model"]

    def send(self, agent):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(agent, "_sleep") as sleep:
            agent.send("继续")
        return out.getvalue(), sleep

    def test_manual_switch_is_distinct_from_automatic_switch(self):
        agent = self.build([])
        agent.session_log = test_errors.RequestLogTest.Log()
        agent.switch_model("other-model")
        self.assertEqual(self.changes(agent), [{
            "model": "other-model", "from_model": "main-model", "reason": "manual",
        }])
        agent.switch_model("backup-model", sticky=True)
        self.assertEqual(self.changes(agent)[-1]["reason"], "automatic")

    def test_auth_and_quota_do_not_claim_retries_were_exhausted(self):
        cases = [
            (openai.AuthenticationError("bad key", response=_response(401), body=None), "auth", "鉴权失败"),
            (openai.APIStatusError("Insufficient Balance", response=_response(402), body=None), "quota", "额度耗尽"),
        ]
        for error, reason, label in cases:
            with self.subTest(reason=reason):
                agent, primary, backup = self.cross_provider([error], [[chunk(content="备用答复")]])
                output, sleep = self.send(agent)
                sleep.assert_not_called()
                self.assertNotIn("重试已耗尽", output)
                self.assertIn(label, output)
                self.assertEqual((len(primary.completions.calls), len(backup.completions.calls)), (1, 1))
                self.assertEqual(self.changes(agent), [{
                    "model": "gateway/main-model", "from_model": "main-model", "reason": reason,
                    "from_route": "primary/main-model", "to_route": "gateway/main-model",
                }])

    def test_long_retry_after_switches_with_rate_limit_reason(self):
        agent, primary, _ = self.cross_provider([rate_limit_error("300")], [[chunk(content="好了")]])
        output, sleep = self.send(agent)
        sleep.assert_not_called()
        self.assertEqual(len(primary.completions.calls), 1)
        self.assertNotIn("重试已耗尽", output)
        self.assertEqual(self.changes(agent)[0]["reason"], "rate_limit")

    def test_transient_error_retains_retries_and_has_its_own_reason(self):
        error = openai.InternalServerError("service unavailable", response=_response(503), body=None)
        agent, primary, _ = self.cross_provider([error] * 3, [[chunk(content="好了")]])
        _, sleep = self.send(agent)
        self.assertEqual(len(primary.completions.calls), 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(self.changes(agent)[0]["reason"], "transient")

    def test_multihop_records_each_immediate_cause(self):
        self.config.fallback_models = ["backup-model", "last-model"]
        agent = self.build([
            rate_limit_error(), rate_limit_error(), rate_limit_error(),
            [usage_chunk(10, 0)], [usage_chunk(10, 0)], [chunk(content="好了")],
        ])
        agent.session_log = test_errors.RequestLogTest.Log()
        output, _ = self.send(agent)
        changes = self.changes(agent)
        self.assertEqual([e["reason"] for e in changes], ["rate_limit", "empty"])
        self.assertEqual(changes[1]["from_route"], "gateway/backup-model")
        self.assertEqual(changes[1]["to_route"], "gateway/last-model")
        self.assertIn("连续空回复", output)

    def test_preferred_recovery_records_successful_probe_and_switch(self):
        error = openai.AuthenticationError("bad key", response=_response(401), body=None)
        agent, primary, backup = self.cross_provider(
            [error, [chunk(content="主服务恢复")]], [[chunk(content="备用答复")]],
        )
        self.send(agent)
        agent._preferred_retry_at = 0.0
        self.send(agent)
        self.assertEqual(self.changes(agent)[-1], {
            "model": "main-model", "from_model": "gateway/main-model", "reason": "preferred_recovered",
            "from_route": "gateway/main-model", "to_route": "primary/main-model",
        })
        requests = [e for kind, e in agent.session_log.events if kind == "request"]
        self.assertEqual([(e["provider"], e["outcome"]) for e in requests], [
            ("primary", "error"), ("gateway", "ok"), ("primary", "ok"),
        ])
        self.assertEqual((len(primary.completions.calls), len(backup.completions.calls)), (2, 1))
        self.assertIsNone(agent._preferred_model)

    def test_failed_probe_logs_request_without_claiming_a_switch(self):
        error = openai.AuthenticationError("bad key", response=_response(401), body=None)
        for reply in (error, [usage_chunk(10, 0)]):
            with self.subTest(reply=type(reply).__name__):
                agent, primary, backup = self.cross_provider(
                    [error, reply], [[chunk(content="备用一")], [chunk(content="备用二")]],
                )
                self.send(agent)
                agent._preferred_retry_at = 0.0
                self.send(agent)
                self.assertEqual(len(self.changes(agent)), 1)
                requests = [e for kind, e in agent.session_log.events if kind == "request"]
                self.assertEqual(requests[-2]["provider"], "primary")
                self.assertEqual(requests[-2]["outcome"], "empty" if isinstance(reply, list) else "error")
                self.assertEqual((len(primary.completions.calls), len(backup.completions.calls)), (2, 2))

    def test_fatal_error_does_not_invent_a_model_switch(self):
        agent, _, backup = self.cross_provider([ValueError("bad config")], [[chunk(content="unused")]])
        with self.assertRaises(ValueError):
            self.send(agent)
        self.assertEqual(self.changes(agent), [])
        self.assertEqual(backup.completions.calls, [])
