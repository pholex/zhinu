"""后台研究的 REST 协议、报告边界、凭据保护与工具挂载；不打网络。"""

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import httpx2

from xiaoyu.agent import Agent, Usage, PLAN_MODE_TOOLS
from xiaoyu.config import Config
from xiaoyu.deepresearch import AGENTS, ENDPOINT, make_deep_research_tools
from xiaoyu.providers import PRESETS, Provider, Registry
from xiaoyu.render import NullSink


class TestDeepResearch(unittest.TestCase):
    def setUp(self):
        self.config = Config(base_url="", model="m", workspace=Path("."))
        self.registry = Registry([Provider("gemini", PRESETS["gemini"].base_url, "secret-key", ("m",))])
        self.usage = Usage()
        self.tools = {t.name: t for t in make_deep_research_tools(self.config, self.registry, self.usage)}
        self.requests = []

    def transport(self, replies):
        replies = iter(replies)

        def respond(request):
            self.requests.append(request)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            if isinstance(reply, tuple):
                code, data = reply
            else:
                code, data = 200, reply
            return httpx2.Response(code, json=data)

        return mock.patch("xiaoyu.deepresearch.netproxy.http_client", side_effect=lambda:
                          httpx2.Client(transport=httpx2.MockTransport(respond), follow_redirects=True))

    def completed(self, **extra):
        return {"id": "job_1", "status": "completed", "steps": [
            {"type": "thought", "content": [{"type": "text", "text": "hidden thought"}]},
            {"type": "model_output", "content": [{"type": "text", "text": "旧输出"}]},
            {"type": "model_output", "content": [
                {"type": "thought", "text": "hidden signature"},
                {"type": "text", "text": "研究报告", "annotations": [
                    {"type": "url_citation", "url": "https://example.com/source"},
                    {"type": "url_citation", "url": "https://example.com/source"},
                    {"type": "url_citation", "url": "file:///secret"}]}]}],
            "usage": {"total_input_tokens": 100, "total_output_tokens": 20,
                      "total_thought_tokens": 30}, **extra}

    def test_start_poll_and_report_accounted_once(self):
        with self.transport([{"id": "job_1", "status": "in_progress"},
                             {"id": "job_1", "status": "in_progress"},
                             self.completed(), self.completed()]):
            out = self.tools["deep_research"].handler(query="研究问题")
            self.assertIn("job_1", out)
            self.assertIn("10 秒", out)
            self.assertIn("尚未完成", self.tools["deep_research_status"].handler("job_1"))
            report = self.tools["deep_research_status"].handler("job_1")
            self.tools["deep_research_status"].handler("job_1")
        self.assertEqual(json.loads(self.requests[0].content),
                         {"input": "研究问题", "agent": AGENTS["standard"], "background": True})
        self.assertEqual(str(self.requests[0].url), ENDPOINT)
        self.assertEqual(self.requests[0].headers["x-goog-api-key"], "secret-key")
        self.assertEqual(self.requests[1].method, "GET")
        self.assertEqual(str(self.requests[1].url), ENDPOINT + "/job_1")
        self.assertIn("研究报告", report)
        self.assertEqual(report.count("https://example.com/source"), 1)
        for hidden in ("hidden", "旧输出", "file:///secret", "secret-key"):
            self.assertNotIn(hidden, report)
        entry = self.usage.by_model["gemini/" + AGENTS["standard"]]
        self.assertEqual((entry.prompt_tokens, entry.completion_tokens, entry.calls), (100, 50, 1))

    def test_max_followup_and_cancel(self):
        with self.transport([{"id": "job_1", "status": "in_progress"},
                             {"id": "job_1", "status": "cancelled"}]):
            self.tools["deep_research"].handler("深入研究", tier="max", previous_interaction_id="old_id")
            out = self.tools["deep_research_cancel"].handler("job_1")
        body = json.loads(self.requests[0].content)
        self.assertEqual(body["agent"], AGENTS["max"])
        self.assertEqual(body["previous_interaction_id"], "old_id")
        self.assertEqual(str(self.requests[1].url), ENDPOINT + "/job_1/cancel")
        self.assertEqual(self.requests[1].method, "POST")
        self.assertIn("cancelled", out)
        self.assertTrue(self.tools["deep_research_cancel"].requires_approval)
        self.assertNotIn("deep_research_cancel", PLAN_MODE_TOOLS)

    def test_old_task_usage_visible_without_duplicate_accounting(self):
        with self.transport([self.completed()]):
            out = self.tools["deep_research_status"].handler("job_1")
        self.assertIn("输入 100", out)
        self.assertEqual(self.usage.turns, 0)

    def test_invalid_arguments_do_not_request(self):
        with mock.patch("xiaoyu.deepresearch.netproxy.http_client") as client:
            for kwargs in ({"query": " "}, {"query": "q", "tier": []},
                           {"query": "q", "tier": "unknown"},
                           {"query": "q", "previous_interaction_id": "../other"}):
                self.assertTrue(self.tools["deep_research"].handler(**kwargs).startswith("ERROR:"))
            for value in ("", "../other", "id?key=secret", "https://bad", None):
                for name in ("deep_research_status", "deep_research_cancel"):
                    self.assertTrue(self.tools[name].handler(value).startswith("ERROR:"))
            client.assert_not_called()

    def test_failures_never_echo_credentials_or_retry(self):
        for reply in ((403, {"error": {"message": "secret-key"}}),
                      RuntimeError("secret-key"), (302, {"location": "https://bad"}),
                      [], {"id": "job_1"}):
            with self.subTest(reply=reply), self.transport([reply]):
                out = self.tools["deep_research"].handler("问题")
                self.assertTrue(out.startswith("ERROR:"))
                self.assertNotIn("secret-key", out)
        self.assertEqual(len(self.requests), 5)

    def test_terminal_failures_and_missing_report(self):
        with self.transport([{"id": "job_1", "status": "failed", "error": "secret-key"},
                             {"id": "job_1", "status": "completed"}]):
            out = self.tools["deep_research_status"].handler("job_1")
            self.assertIn("已终止", out)
            self.assertNotIn("secret-key", out)
            self.assertIn("未返回文本报告", self.tools["deep_research_status"].handler("job_1"))

    def test_malformed_report_blocks_do_not_crash(self):
        for steps in (42, [{"type": "model_output", "content": 42}],
                      [{"type": "model_output", "content": [{"type": "text", "text": "报告",
                                                             "annotations": 42}]}]):
            with self.transport([self.completed(steps=steps)]):
                out = self.tools["deep_research_status"].handler("job_1")
                self.assertIn("completed", out)

    def test_empty_final_output_does_not_return_an_earlier_draft(self):
        data = self.completed()
        data["steps"].append({"type": "model_output", "content": []})
        with self.transport([data]):
            out = self.tools["deep_research_status"].handler("job_1")
        self.assertIn("未返回文本报告", out)
        self.assertNotIn("研究报告", out)
        self.assertNotIn("旧输出", out)

    def test_disabled_and_missing_provider(self):
        self.config.enable_deep_research = False
        for tool in self.tools.values():
            self.assertFalse(tool.available())
        self.assertIn("已禁用", self.tools["deep_research"].handler("问题"))
        self.config.enable_deep_research = True
        tools = make_deep_research_tools(self.config, Registry([
            Provider("other", "https://example.invalid/v1", "test-key", ("m",))]), self.usage)
        self.assertFalse(tools[0].available())
        self.assertIn("GEMINI_API_KEY", tools[0].handler("问题"))

    def test_config_switch(self):
        with mock.patch.dict(os.environ, {"XIAOYU_ENABLE_DEEP_RESEARCH": "0"}, clear=True):
            self.assertFalse(Config.from_env().enable_deep_research)
        with mock.patch.dict(os.environ, {"XIAOYU_ENABLE_DEEP_RESEARCH": "1"}, clear=True):
            self.assertTrue(Config.from_env().enable_deep_research)

    def test_agent_mounts_tools_independent_of_search(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, workspace=Path(directory), enable_web_search=False,
                             enable_x_search=False, enable_skills=False, enable_agents=False,
                             enable_hooks=False, enable_plugins=False, enable_mcp=False,
                             enable_explore=False)
            agent = Agent(config, registry=self.registry, sink=NullSink())
            names = [schema["function"]["name"] for schema in agent.toolbox.schemas()]
            for name in self.tools:
                self.assertIn(name, names)
                self.assertTrue(agent.toolbox.get(name).untrusted)
            self.assertIn("deep_research_status", PLAN_MODE_TOOLS)
            config.enable_deep_research = False
            agent = Agent(config, registry=self.registry, sink=NullSink())
            self.assertIsNone(agent.toolbox.get("deep_research"))
