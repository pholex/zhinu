"""web_fetch 工具：请求协议、URL 校验、错误语义、挂载与来源边界；不打网络。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

import httpx2

from xiaoyu.agent import Agent, PLAN_MODE_TOOLS
from xiaoyu.config import Config
from xiaoyu.providers import PRESETS, Provider, Registry
from xiaoyu.render import NullSink
from xiaoyu.webfetch import (MAX_PAGE_CHARS, fetch_endpoint, make_web_fetch_tool,
                             unescape_markdown, validate_url)


def _registry(name: str = "deepseek") -> Registry:
    return Registry([Provider(name, PRESETS[name].base_url, "sk-secret-key", ("m",))])


class TestWebFetch(unittest.TestCase):
    def setUp(self):
        self.config = Config(base_url="", model="m", workspace=Path("."))
        self.requests: list[httpx2.Request] = []

    def tool(self, replies, registry=None):
        """replies：每次请求依次取一项——异常直接抛，(code, json) 或 json（200）。"""
        replies = iter(replies)

        def respond(request):
            self.requests.append(request)
            reply = next(replies)
            if isinstance(reply, Exception):
                raise reply
            code, data = reply if isinstance(reply, tuple) else (200, reply)
            return httpx2.Response(code, json=data)

        patcher = mock.patch("xiaoyu.webfetch.netproxy.http_client",
                             side_effect=lambda: httpx2.Client(transport=httpx2.MockTransport(respond)))
        patcher.start()
        self.addCleanup(patcher.stop)
        return make_web_fetch_tool(self.config, registry or _registry(), NullSink())

    def test_request_shape_and_result(self):
        tool = self.tool([{"title": "示例页", "content": "# 正文\n\n第一段。"}])
        out = tool.handler(url=" https://example.com/doc ")
        request = self.requests[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(str(request.url), "https://api.deepseek.com/v1/tools/fetch")
        self.assertEqual(request.headers["Authorization"], "Bearer sk-secret-key")
        self.assertEqual(json.loads(request.content), {"url": "https://example.com/doc"})
        self.assertTrue(out.startswith("[网页抓取"))
        self.assertIn("URL：https://example.com/doc", out)
        self.assertIn("标题：示例页", out)
        self.assertIn("第一段。", out)
        self.assertTrue(tool.untrusted)
        self.assertFalse(tool.requires_approval)
        self.assertEqual(tool.output_limit, MAX_PAGE_CHARS)
        self.assertTrue(tool.available())

    def test_server_escaping_is_reversed(self):
        #  服务端实测形态：标点全转义、真实反斜杠成 `\\`、字母数字前不转义
        escaped = "\\#\\# Date\\: 2026\\-10\\-17\n\ncurl https\\:\\/\\/a\\.example \\\\\n  \\-d \\'\\{\\\"q\\\"\\}\\'\nre\\.sub\\(r\\\"\\\\d\\+\\\"\\)"
        self.assertEqual(unescape_markdown(escaped),
                         '## Date: 2026-10-17\n\ncurl https://a.example \\\n  -d \'{"q"}\'\nre.sub(r"\\d+")')
        self.assertEqual(unescape_markdown("plain text 中文 \\n stays"), "plain text 中文 \\n stays")
        tool = self.tool([{"title": "t", "content": "\\# 标题\\."}])
        self.assertIn("\n\n# 标题.", tool.handler(url="https://a.example/"))

    def test_missing_title_and_empty_content(self):
        tool = self.tool([{"title": "", "content": "只有正文"}, {"title": "空页", "content": "  "},
                          {"title": "坏形态"}, [1, 2]])
        self.assertNotIn("标题：", tool.handler(url="https://a.example/"))
        for _ in range(3):
            self.assertIn("ERROR:", tool.handler(url="https://a.example/"))
        self.assertEqual(len(self.requests), 4)

    def test_invalid_urls_do_not_call_api(self):
        tool = self.tool([])
        for url in ("", "   ", 42, "ftp://a.example/x", "example.com/x", "https://",
                    "https://user:pw@a.example/x", "https://a.example/" + "x" * 9000):
            with self.subTest(url=url):
                out = tool.handler(url=url)
                self.assertTrue(out.startswith("ERROR:"), out)
        self.assertEqual(self.requests, [])
        self.assertIsNone(validate_url("http://a.example/path?q=1#frag"))

    def test_status_code_semantics(self):
        envelope = {"error": {"message": "blocked by policy", "type": "forbidden", "code": "x"}}
        tool = self.tool([(402, envelope), (403, envelope), (404, {}), (429, envelope),
                          (503, {"error": {"message": "upstream Bearer sk-live-token down"}}),
                          (418, envelope)])
        cases = [("余额不足", "不要原样重试"), ("安全策略", "不要原样重试"),
                 ("没有可用正文", "不要原样重试"), ("过于频繁", "再试一次"),
                 ("暂时不可用", "再试一次"), ("HTTP 418", "不要原样重试")]
        for expected, retry in cases:
            with self.subTest(expected=expected):
                out = tool.handler(url="https://a.example/")
                self.assertTrue(out.startswith("ERROR:"))
                self.assertIn(expected, out)
                self.assertIn(retry, out)
        self.assertEqual(len(self.requests), 6)

    def test_server_detail_is_redacted(self):
        tool = self.tool([(500, {"error": {"message": "proxy Bearer sk-live-token failed"}})])
        out = tool.handler(url="https://a.example/")
        self.assertIn("服务端说明", out)
        self.assertNotIn("sk-live-token", out)

    def test_transport_error_redacts_credentials(self):
        tool = self.tool([httpx2.ConnectError(
            "https://opaque_secret@gateway.example/fetch Authorization: Bearer private-token")])
        out = tool.handler(url="https://a.example/")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("ConnectError", out)
        self.assertNotIn("opaque_secret", out)
        self.assertNotIn("private-token", out)

    def test_missing_provider_and_disabled(self):
        tool = make_web_fetch_tool(self.config, _registry("xai"), NullSink())
        self.assertFalse(tool.available())
        self.assertIn("DEEPSEEK_API_KEY", tool.handler(url="https://a.example/"))
        tool = self.tool([{"title": "t", "content": "c"}])
        self.config.enable_web_fetch = False
        self.assertFalse(tool.available())
        self.assertIn("已禁用", tool.handler(url="https://a.example/"))
        self.assertEqual(self.requests, [])

    def test_endpoint_derivation(self):
        self.assertEqual(fetch_endpoint("https://api.deepseek.com/v1"), "https://api.deepseek.com/v1/tools/fetch")
        self.assertEqual(fetch_endpoint("https://api.deepseek.com/v1/"), "https://api.deepseek.com/v1/tools/fetch")
        self.assertEqual(fetch_endpoint("https://api.deepseek.com"), "https://api.deepseek.com/v1/tools/fetch")

    def test_config_switch(self):
        for value, expected in (("0", False), ("false", False), ("off", False), ("1", True)):
            with mock.patch.dict(os.environ, {"XIAOYU_ENABLE_WEB_FETCH": value}, clear=True):
                self.assertEqual(Config.from_env(workspace=Path(".")).enable_web_fetch, expected)

    def test_agent_mounts_tool_independent_of_search(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(self.config, workspace=Path(directory), enable_web_search=False,
                             enable_x_search=False, enable_deep_research=False, enable_skills=False,
                             enable_agents=False, enable_hooks=False, enable_plugins=False,
                             enable_mcp=False, enable_explore=False, enable_browser=False)
            agent = Agent(config, registry=_registry(), sink=NullSink())
            names = [schema["function"]["name"] for schema in agent.toolbox.schemas()]
            self.assertIn("web_fetch", names)
            self.assertNotIn("web_search", names)
            self.assertTrue(agent.toolbox.get("web_fetch").untrusted)
            self.assertIn("web_fetch", PLAN_MODE_TOOLS)
            agent = Agent(replace(config, enable_web_fetch=False), registry=_registry(), sink=NullSink())
            self.assertIsNone(agent.toolbox.get("web_fetch"))
            #  没配 DeepSeek：工具注册但不进 schemas
            agent = Agent(config, registry=_registry("xai"), sink=NullSink())
            self.assertNotIn("web_fetch", [s["function"]["name"] for s in agent.toolbox.schemas()])


if __name__ == "__main__":
    unittest.main()
