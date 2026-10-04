"""X Search 的请求协议、筛选校验、工具挂载与来源边界；不打网络。"""

import tempfile
import json
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from xiaoyu.agent import Agent, Usage, PLAN_MODE_TOOLS
from xiaoyu.config import Config
from xiaoyu.providers import Provider, Registry
from xiaoyu.render import NullSink
from xiaoyu.xsearch import make_x_search_tool
from tests.test_websearch import _config, _registry, _response


class TestXSearch(unittest.TestCase):
    def tool(self, response=None, error=None):
        self.registry = _registry(response if response is not None else _response(
            top_citations=("https://x.com/example/status/1",)), error=error)
        self.usage = Usage()
        return make_x_search_tool(_config("deepseek"), self.registry, self.usage, NullSink())

    def test_request_filters_and_usage(self):
        tool = self.tool()
        out = tool.handler(query="动态", allowed_x_handles=["@example"],
                           from_date="2026-10-01", to_date="2026-10-03",
                           enable_image_understanding=True, enable_video_understanding=True)
        request = self.registry.client("xai").responses.create.last_request
        self.assertEqual(request["tools"], [{"type": "x_search",
                         "allowed_x_handles": ["example"], "from_date": "2026-10-01",
                         "to_date": "2026-10-03", "enable_image_understanding": True,
                         "enable_video_understanding": True}])
        self.assertEqual(request["model"], "grok-4.7")
        self.assertIn("https://x.com/example/status/1", out)
        self.assertIn("作者说法", out)
        entry = self.usage.by_model["xai/grok-4.7"]
        self.assertEqual((entry.prompt_tokens, entry.completion_tokens), (100, 20))
        self.assertTrue(tool.untrusted)
        self.assertFalse(tool.requires_approval)

    def test_default_request_and_exclusion(self):
        tool = self.tool()
        tool.handler(query="讨论")
        create = self.registry.client("xai").responses.create
        self.assertEqual(create.last_request["tools"], [{"type": "x_search"}])
        tool.handler(query="讨论", excluded_x_handles=["example"])
        self.assertEqual(create.last_request["tools"],
                         [{"type": "x_search", "excluded_x_handles": ["example"]}])

    def test_invalid_filters_do_not_call_api(self):
        cases = [dict(allowed_x_handles=["a"], excluded_x_handles=["b"]),
                 dict(allowed_x_handles=["a"] * 21), dict(allowed_x_handles=[]),
                 dict(allowed_x_handles="a"), dict(allowed_x_handles=["https://x.com/a"]),
                 dict(from_date="20261001"), dict(to_date="2026-02-30"),
                 dict(from_date="2026-10-03", to_date="2026-10-01"),
                 dict(enable_video_understanding="true")]
        for options in cases:
            with self.subTest(options=options):
                tool = self.tool()
                self.assertTrue(tool.handler(query="讨论", **options).startswith("ERROR:"))
                self.assertFalse(hasattr(self.registry.client("xai").responses.create, "last_request"))
                self.assertEqual(self.usage.turns, 0)

    def test_missing_provider_and_disabled(self):
        config = _config()
        tool = make_x_search_tool(config, _registry(name="deepseek"), Usage(), NullSink())
        self.assertFalse(tool.available())
        self.assertIn("XAI_API_KEY", tool.handler(query="讨论"))
        config.enable_x_search = False
        self.assertIn("已禁用", tool.handler(query="讨论"))

    def test_failures_and_unverified_answers(self):
        self.assertIn("ERROR:", self.tool(error=RuntimeError("offline")).handler(query="讨论"))
        for response in (_response(), _response(text="", top_citations=("https://x.com/a",))):
            self.assertIn("ERROR:", self.tool(response).handler(query="讨论"))
            self.assertEqual(self.usage.turns, 1)
        response = _response(top_citations=("https://x.com/a",))
        response.status = "incomplete"
        self.assertIn("尚未完成", self.tool(response).handler(query="讨论"))

    def test_transport_error_redacts_credentials(self):
        out = self.tool(error=RuntimeError(
            "https://opaque_secret@gateway.example/search Authorization: Bearer private-token"
        )).handler(query="讨论")
        self.assertIn("RuntimeError", out)
        self.assertNotIn("opaque_secret", out)
        self.assertNotIn("private-token", out)

    def test_config_switch(self):
        for value in ("0", "false", "no", "off"):
            with mock.patch.dict("os.environ", {"XIAOYU_ENABLE_X_SEARCH": value}):
                self.assertFalse(Config.from_env(workspace=Path(".")).enable_x_search)

    def test_real_sdk_serializes_x_search_and_parses_citations(self):
        import httpx2
        from openai import OpenAI

        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx2.Response(200, json={
                "id": "resp_test", "object": "response", "status": "completed",
                "output": [{"type": "message", "id": "msg_test", "role": "assistant",
                            "status": "completed", "content": [{"type": "output_text",
                            "text": "找到一条帖子", "annotations": [{"type": "url_citation",
                            "url": "https://x.com/example/status/1", "title": "原帖",
                            "start_index": 0, "end_index": 6}]}]}],
                "usage": {"input_tokens": 12, "output_tokens": 5, "total_tokens": 17},
            })

        with OpenAI(api_key="test-key", base_url="https://x.invalid/v1", max_retries=0,
                    http_client=httpx2.Client(transport=httpx2.MockTransport(respond))) as client:
            registry = Registry([Provider("xai", "https://x.invalid/v1", "test-key", ("m",))],
                                clients={"xai": client})
            tool = make_x_search_tool(_config(), registry, Usage(), NullSink())
            result = tool.handler(query="帖子", allowed_x_handles=["example"])
        self.assertIn("找到一条帖子", result)
        self.assertIn("https://x.com/example/status/1", result)
        self.assertEqual(requests[0]["tools"], [{"type": "x_search", "allowed_x_handles": ["example"]}])

    def test_agent_mounts_search_independently_of_web_backend(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(_config("deepseek"), workspace=Path(directory),
                             enable_skills=False, enable_agents=False, enable_hooks=False,
                             enable_plugins=False, enable_mcp=False, enable_explore=False)
            agent = Agent(config, registry=_registry(), sink=NullSink())
            names = [schema["function"]["name"] for schema in agent.toolbox.schemas()]
            self.assertIn("x_search", names)
            self.assertNotIn("web_search", names)
            self.assertIn("x_search", PLAN_MODE_TOOLS)
            config.enable_x_search = False
            agent = Agent(config, registry=_registry(), sink=NullSink())
            self.assertIsNone(agent.toolbox.get("x_search"))
