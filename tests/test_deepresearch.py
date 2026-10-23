"""后台研究的 REST 协议、报告边界、凭据保护与工具挂载；不打网络。"""

import json
import os
import tempfile
import threading
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

from .test_background import Collector, wait_until


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
        #  被 thought 步骤隔开的更早 model_output 是中间稿，不进报告
        return {"id": "job_1", "status": "completed", "steps": [
            {"type": "model_output", "content": [{"type": "text", "text": "旧输出"}]},
            {"type": "thought", "content": [{"type": "text", "text": "hidden thought"}]},
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
        data = self.completed(steps=[
            {"type": "model_output", "content": [{"type": "text", "text": "旧输出"}]},
            {"type": "thought", "content": [{"type": "text", "text": "hidden thought"}]},
            {"type": "model_output", "content": []}])
        with self.transport([data]):
            out = self.tools["deep_research_status"].handler("job_1")
        self.assertIn("未返回文本报告", out)
        self.assertNotIn("旧输出", out)

    def test_report_joins_adjacent_output_chunks_with_sources_and_images(self):
        """服务端把长报告切成相邻的多个 model_output：首段标题+引用、次段图+正文。
        只取最后一段会丢标题和全部来源（2026-10-23 真实响应的形态）。"""
        data = self.completed(steps=[
            {"type": "thought", "content": [{"type": "text", "text": "hidden thought"}]},
            {"type": "model_output", "content": [{"type": "text", "text": "# 标题\n摘要", "annotations": [
                {"type": "url_citation", "url": "https://example.com/a"}]}]},
            {"type": "model_output", "content": [
                {"type": "image", "mime_type": "image/png", "data": "AAAA"},
                {"type": "text", "text": "正文续", "annotations": [
                    {"type": "url_citation", "url": "https://example.com/b"},
                    {"type": "url_citation", "url": "https://example.com/a"}]}]}])
        with self.transport([data]):
            out = self.tools["deep_research_status"].handler("job_1")
        self.assertIn("# 标题\n摘要\n正文续", out)
        self.assertIn("1 张图", out)
        self.assertNotIn("AAAA", out)
        self.assertEqual(out.index("https://example.com/a"), out.rindex("https://example.com/a"))
        self.assertIn("https://example.com/b", out)
        self.assertNotIn("hidden", out)

    # ---------- 完成通知（watcher） ----------

    def watched_tools(self):
        self.notify = Collector()
        return {t.name: t for t in make_deep_research_tools(self.config, self.registry, self.usage,
                                                             notify=self.notify)}

    def test_watcher_notifies_on_completion_and_status_reads_the_cached_result(self):
        tools = self.watched_tools()
        with mock.patch("xiaoyu.deepresearch.POLL_SECONDS", 0.01), self.transport([
                {"id": "job_1", "status": "in_progress"},
                {"id": "job_1", "status": "in_progress"},
                self.completed()]):
            out = tools["deep_research"].handler(query="研究问题")
            self.assertIn("自动通知", out)
            self.assertIn("不要用 sleep", out)
            self.assertNotIn("10 秒", out)
            self.assertTrue(wait_until(lambda: self.notify.items), "watcher 没有送达完成通知")
            report = tools["deep_research_status"].handler("job_1")
        text, key = self.notify.items[0]
        self.assertEqual(key, "deep-research-done-job_1")
        self.assertIn("已完成", text)
        self.assertIn('deep_research_status(interaction_id="job_1")', text)
        #  报告来自 watcher 缓存：提交 + 两次轮询，status 没有再打网络
        self.assertEqual(len(self.requests), 3)
        self.assertIn("研究报告", report)
        self.assertIn("https://example.com/source", report)
        entry = self.usage.by_model["gemini/" + AGENTS["standard"]]
        self.assertEqual((entry.prompt_tokens, entry.completion_tokens, entry.calls), (100, 50, 1))
        #  工具描述也换成了"等通知"的契约
        self.assertIn("自动收到通知", tools["deep_research"].description)
        self.assertIn("不要 sleep", tools["deep_research"].description)
        self.assertNotIn("10 秒", tools["deep_research_status"].description)

    def test_watcher_gives_up_after_repeated_failures_and_status_falls_back_to_manual(self):
        tools = self.watched_tools()
        with mock.patch("xiaoyu.deepresearch.POLL_SECONDS", 0.01), \
                mock.patch("xiaoyu.deepresearch.MAX_CONSECUTIVE_FAILURES", 3), self.transport([
                {"id": "job_1", "status": "in_progress"},
                RuntimeError("secret-key"), (500, {}), RuntimeError("secret-key"),
                {"id": "job_1", "status": "in_progress"}]):
            tools["deep_research"].handler(query="研究问题")
            self.assertTrue(wait_until(lambda: self.notify.items))
            self.assertTrue(wait_until(lambda: "稍后用 deep_research_status 查询" in
                                       tools["deep_research_status"].handler("job_1")))
        text, _ = self.notify.items[0]
        self.assertIn("停止自动跟踪", text)
        self.assertNotIn("secret-key", text)
        self.assertEqual(len(self.notify.items), 1)

    def test_watch_lifetime_exceeded_tells_the_model_to_query_manually(self):
        tools = self.watched_tools()
        with mock.patch("xiaoyu.deepresearch.WATCH_MAX_SECONDS", 0), self.transport([
                {"id": "job_1", "status": "in_progress"}]):
            tools["deep_research"].handler(query="研究问题")
            self.assertTrue(wait_until(lambda: self.notify.items))
        self.assertIn("不再自动跟踪", self.notify.items[0][0])
        self.assertEqual(len(self.requests), 1)

    def test_cancel_stops_the_watcher_without_a_notification(self):
        tools = self.watched_tools()
        with self.transport([{"id": "job_1", "status": "in_progress"},
                             {"id": "job_1", "status": "cancelled"}]):
            tools["deep_research"].handler(query="研究问题")
            thread = next(t for t in threading.enumerate() if t.name.startswith("deep-research-job_1"))
            out = tools["deep_research_cancel"].handler("job_1")
            self.assertIn("cancelled", out)
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            #  取消结果已缓存：再查不打网络
            self.assertIn("cancelled", tools["deep_research_status"].handler("job_1"))
        self.assertEqual(self.notify.items, [])
        self.assertEqual(len(self.requests), 2)

    def test_agent_wires_the_notification_track(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, workspace=Path(directory), enable_web_search=False,
                             enable_x_search=False, enable_skills=False, enable_agents=False,
                             enable_hooks=False, enable_plugins=False, enable_mcp=False,
                             enable_explore=False)
            agent = Agent(config, registry=self.registry, sink=NullSink())
            with mock.patch("xiaoyu.deepresearch.POLL_SECONDS", 0.01), self.transport([
                    {"id": "job_1", "status": "in_progress"}, self.completed()]):
                out = agent.toolbox.get("deep_research").handler(query="研究问题")
                self.assertIn("自动通知", out)
                self.assertTrue(wait_until(lambda: agent.pending_notifications()))
            item = agent.pending_notifications()[0]
            self.assertTrue(item["wake"])
            self.assertEqual(item["key"], "deep-research-done-job_1")

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
