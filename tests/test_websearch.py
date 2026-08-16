"""web_search 工具：一次性调用厂商 Responses 内置搜索，后端可配。"""

from __future__ import annotations

import types
import unittest
from pathlib import Path

from xiaoyu.agent import Usage
from xiaoyu.config import Config
from xiaoyu.providers import Provider, Registry
from xiaoyu.render import NullSink
from xiaoyu.websearch import MAX_ANSWER_CHARS, make_web_search_tool


def _config(search_provider: str = "deepseek") -> Config:
    return Config(
        base_url="", model="m", workspace=Path("."), search_provider=search_provider
    )


def _response(
    text: str = "结论：X。（来源：example.com）",
    citations: tuple[str, ...] = (),
    top_citations: tuple[str, ...] = (),
    usage: tuple[int, int] | None = (100, 20),
):
    """拼一个 Responses 形态的假响应（防御式访问，缺字段也不该炸）。"""
    annotations = [
        types.SimpleNamespace(type="url_citation", url=url) for url in citations
    ]
    message = types.SimpleNamespace(
        type="message",
        content=[types.SimpleNamespace(annotations=annotations)],
    )
    return types.SimpleNamespace(
        output_text=text,
        output=[types.SimpleNamespace(type="web_search_call"), message],
        citations=list(top_citations),
        usage=(
            types.SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1])
            if usage
            else None
        ),
    )


def _registry(response=None, error: Exception | None = None, name: str = "deepseek") -> Registry:
    def create(**kwargs):
        if error is not None:
            raise error
        create.last_request = kwargs  # noqa: B023 - 测试记录用
        return response

    client = types.SimpleNamespace(responses=types.SimpleNamespace(create=create))
    return Registry(
        [Provider(name, "https://x/v1", "sk-test", ("m1",))],
        clients={name: client},
    )


def _tool(registry: Registry, usage: Usage | None = None, provider: str = "deepseek"):
    return make_web_search_tool(_config(provider), registry, usage or Usage(), NullSink())


class TestWebSearchTool(unittest.TestCase):
    def test_answer_and_usage_accounting(self):
        usage = Usage()
        out = _tool(_registry(_response()), usage).handler(query="X 是什么")
        self.assertIn("结论：X", out)
        self.assertIn("联网搜索结论", out)
        entry = usage.by_model["deepseek/deepseek-v4-flash"]
        self.assertEqual((entry.prompt_tokens, entry.completion_tokens, entry.calls), (100, 20, 1))

    def test_request_uses_flash_and_builtin_tool(self):
        registry = _registry(_response())
        _tool(registry).handler(query="q")
        request = registry.client("deepseek").responses.create.last_request
        self.assertEqual(request["model"], "deepseek-v4-flash")
        self.assertEqual(request["tools"], [{"type": "web_search"}])

    def test_xai_backend_switch(self):
        registry = _registry(_response(top_citations=("https://c.com",)), name="xai")
        usage = Usage()
        tool = _tool(registry, usage, provider="xai")
        self.assertTrue(tool.available())
        out = tool.handler(query="q")
        request = registry.client("xai").responses.create.last_request
        self.assertEqual(request["model"], "grok-4.6")
        self.assertIn("grok-4.6", out)
        self.assertIn("https://c.com", out)
        self.assertIn("xai/grok-4.6", usage.by_model)

    def test_unknown_backend_hidden_and_errors(self):
        tool = _tool(_registry(_response()), provider="bing")
        self.assertFalse(tool.available())
        out = tool.handler(query="q")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("deepseek", out)
        self.assertIn("xai", out)

    def test_backend_provider_not_registered(self):
        #  选了 xai 但 registry 里只有 deepseek：不可用，报错提示缺哪个 key
        tool = _tool(_registry(_response(), name="deepseek"), provider="xai")
        self.assertFalse(tool.available())
        out = tool.handler(query="q")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("XAI_API_KEY", out)

    def test_citations_merged_deduped(self):
        response = _response(
            citations=("https://a.com", "https://b.com"),
            top_citations=("https://a.com", "https://c.com"),
        )
        out = _tool(_registry(response)).handler(query="q")
        self.assertEqual(out.count("https://a.com"), 1)
        self.assertIn("https://b.com", out)
        self.assertIn("https://c.com", out)

    def test_api_error_returns_error_not_raises(self):
        out = _tool(_registry(error=RuntimeError("boom"))).handler(query="q")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("boom", out)

    def test_empty_answer_and_missing_usage(self):
        usage = Usage()
        out = _tool(_registry(_response(text="", usage=None)), usage).handler(query="q")
        self.assertNotIn("ERROR:", out)
        self.assertIn("没有返回内容", out)
        self.assertEqual(usage.by_model, {})

    def test_long_answer_truncated(self):
        out = _tool(_registry(_response(text="长" * (MAX_ANSWER_CHARS + 100)))).handler(query="q")
        self.assertIn("已截断", out)
        self.assertLess(len(out), MAX_ANSWER_CHARS + 200)

    def test_no_approval_required(self):
        self.assertFalse(_tool(_registry(_response())).requires_approval)


if __name__ == "__main__":
    unittest.main()
