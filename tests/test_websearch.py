"""web_search 工具：厂商 Responses / Messages 内置搜索，后端可配。"""

from __future__ import annotations

import types
import contextlib
import io
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from xiaoyu.agent import Usage
from xiaoyu.config import Config
from xiaoyu.providers import Provider, Registry
from xiaoyu.render import NullSink
from xiaoyu.websearch import MAX_ANSWER_CHARS, make_web_search_tool, search_command
from xiaoyu import bedrock_search, messages
from xiaoyu.providers import IAM_PLACEHOLDER_KEY


def _config(search_provider: str = "xai") -> Config:
    return Config(
        base_url="", model="m", workspace=Path("."), search_provider=search_provider
    )


class TestSearchCommand(unittest.TestCase):
    def test_switch_makes_existing_tool_available_and_uses_new_backend(self):
        config = _config("deepseek")
        registry = _registry(_response(top_citations=("https://example.com",)))
        usage = Usage()
        tool = make_web_search_tool(config, registry, usage, NullSink())
        self.assertFalse(tool.available())
        self.assertIn("已切换", search_command(config, registry, "xai"))
        self.assertTrue(tool.available())
        self.assertIn("grok-4.7", tool.handler(query="问题"))
        self.assertEqual(config.model, "m")
        self.assertIn("xai/grok-4.7", usage.by_model)

    def test_invalid_or_unconfigured_backend_preserves_selection(self):
        config = _config()
        registry = _registry()
        for args in ("unknown", "xai extra", "deepseek", "bedrock"):
            with self.subTest(args=args):
                self.assertIn("未改变", search_command(config, registry, args))
                self.assertEqual(config.search_provider, "xai")

    def test_disabled_search_stays_disabled(self):
        config = _config("deepseek")
        config.enable_web_search = False
        self.assertIn("已禁用", search_command(config, _registry(), "xai"))
        self.assertEqual(config.search_provider, "deepseek")

    def test_cli_and_acp_share_command_without_calling_model(self):
        from xiaoyu.cli import handle_slash, SLASH_COMMANDS
        from xiaoyu.acp import match_command, available_commands

        agent = types.SimpleNamespace(config=_config(), registry=_registry())
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertFalse(handle_slash(agent, "/search"))
        self.assertIn("当前搜索后端：xai", buffer.getvalue())
        self.assertIn("deepseek · deepseek-flash · 未配置", buffer.getvalue())
        self.assertIn("xai · grok-4.7 · 已配置", buffer.getvalue())
        self.assertIn("/search", SLASH_COMMANDS)
        self.assertIn("search", [entry["name"] for entry in available_commands()])
        command, args = match_command("/search xai")
        self.assertIn("已切换", command.run(agent, args))


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


def _registry(response=None, error: Exception | None = None, name: str = "xai") -> Registry:
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


def _tool(registry: Registry, usage: Usage | None = None, provider: str = "xai"):
    return make_web_search_tool(_config(provider), registry, usage or Usage(), NullSink())


class TestWebSearchTool(unittest.TestCase):
    def test_answer_and_usage_accounting(self):
        usage = Usage()
        out = _tool(_registry(_response()), usage).handler(query="X 是什么")
        self.assertIn("结论：X", out)
        self.assertIn("联网搜索结论", out)
        entry = usage.by_model["xai/grok-4.7"]
        self.assertEqual((entry.prompt_tokens, entry.completion_tokens, entry.calls), (100, 20, 1))

    def test_request_uses_builtin_tool(self):
        registry = _registry(_response())
        _tool(registry).handler(query="q")
        request = registry.client("xai").responses.create.last_request
        self.assertEqual(request["model"], "grok-4.7")
        self.assertEqual(request["tools"], [{"type": "web_search"}])

    def test_xai_backend_switch(self):
        registry = _registry(_response(top_citations=("https://c.com",)), name="xai")
        usage = Usage()
        tool = _tool(registry, usage, provider="xai")
        self.assertTrue(tool.available())
        out = tool.handler(query="q")
        request = registry.client("xai").responses.create.last_request
        self.assertEqual(request["model"], "grok-4.7")
        self.assertIn("grok-4.7", out)
        self.assertIn("https://c.com", out)
        self.assertIn("xai/grok-4.7", usage.by_model)

    def test_default_backend_is_deepseek_and_hidden_without_key(self):
        default = Config(base_url="", model="m", workspace=Path("."))
        self.assertEqual(default.search_provider, "deepseek")
        tool = make_web_search_tool(default, _registry(_response()), Usage(), NullSink())
        self.assertFalse(tool.available())
        self.assertIn("DEEPSEEK_API_KEY", tool.handler(query="q"))

    def test_unknown_backend_hidden_and_errors(self):
        tool = _tool(_registry(_response()), provider="bing")
        self.assertFalse(tool.available())
        out = tool.handler(query="q")
        self.assertTrue(out.startswith("ERROR:"))
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

    def test_api_error_redacts_credentials(self):
        out = _tool(_registry(error=RuntimeError(
            "https://opaque_secret@gateway.example/search Authorization: Bearer private-token"
        ))).handler(query="q")
        self.assertIn("RuntimeError", out)
        self.assertNotIn("opaque_secret", out)
        self.assertNotIn("private-token", out)

    def test_disabled_or_invalid_query_never_calls_provider(self):
        registry = _registry(_response())
        config = _config()
        tool = make_web_search_tool(config, registry, Usage(), NullSink())
        for query in (None, [], "", "   "):
            self.assertTrue(tool.handler(query=query).startswith("ERROR:"))
        config.enable_web_search = False
        self.assertFalse(tool.available())
        self.assertIn("已禁用", tool.handler(query="q"))
        self.assertFalse(hasattr(registry.client("xai").responses.create, "last_request"))

    def test_empty_answer_and_missing_usage(self):
        usage = Usage()
        out = _tool(_registry(_response(text="", usage=None)), usage).handler(query="q")
        self.assertNotIn("ERROR:", out)
        self.assertIn("没有返回内容", out)
        self.assertEqual(usage.by_model, {})

    def test_long_answer_is_left_whole_for_the_toolbox_to_bound(self):
        """结论不在工具里硬切：交给工具箱落盘并留头尾预览（见 test_answer_overflow）。"""
        tool = _tool(_registry(_response(text="长" * (MAX_ANSWER_CHARS + 100) + "结尾的来源")))
        out = tool.handler(query="q")
        self.assertIn("结尾的来源", out)
        self.assertEqual(tool.output_limit, MAX_ANSWER_CHARS + 500)

    def test_no_approval_required(self):
        self.assertFalse(_tool(_registry(_response())).requires_approval)


class TestDeepSeekSearch(unittest.TestCase):
    def setUp(self):
        self.registry = _registry(name="deepseek")
        self.usage = Usage()
        self.client = mock.MagicMock()
        self.client.__enter__.return_value = self.client
        patcher = mock.patch.object(messages, "client", return_value=self.client)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.tool = _tool(self.registry, self.usage, provider="deepseek")

    def response(self, text="天气结论", results=None, stop_reason="end_turn"):
        return types.SimpleNamespace(
            content=[
                types.SimpleNamespace(type="thinking", thinking="不能回灌的思考"),
                types.SimpleNamespace(type="server_tool_use", name="web_search", input={"query": "q"}),
                types.SimpleNamespace(type="web_search_tool_result", content=(
                    [{"type": "web_search_result", "url": "https://official.example", "encrypted_content": "不回灌"}]
                    if results is None else results
                )),
                types.SimpleNamespace(type="text", text=text),
            ],
            stop_reason=stop_reason,
            usage=types.SimpleNamespace(input_tokens=100, output_tokens=20,
                                        cache_read_input_tokens=30, cache_creation_input_tokens=10),
        )

    def call(self, response):
        self.client.messages.create.return_value = response
        return self.tool.handler(query="今天南通天气")

    def test_messages_endpoint_builtin_search_and_usage(self):
        self.assertTrue(self.tool.available())
        out = self.call(self.response())
        self.assertIn("天气结论", out)
        self.assertIn("deepseek-flash", out)
        self.assertIn("https://official.example", out)
        self.assertNotIn("不能回灌", out)
        self.assertNotIn("不回灌", out)
        args = self.factory.call_args.args
        self.assertEqual(args[:2], ("https://x/anthropic", "sk-test"))
        self.assertEqual(args[2].connect, 15.0)
        self.assertEqual(args[2].read, _config("deepseek").request_timeout)
        request = self.client.messages.create.call_args.kwargs
        self.assertEqual(request["model"], "deepseek-flash")
        self.assertEqual(request["tools"], [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}])
        self.assertEqual(request["messages"], [{"role": "user", "content": "今天南通天气"}])
        self.assertIn("当前日期", request["system"])
        self.assertIn("优先官方", request["system"])
        self.assertEqual(self.client.__exit__.call_count, 1)
        entry = self.usage.by_model["deepseek/deepseek-flash"]
        self.assertEqual((entry.prompt_tokens, entry.completion_tokens, entry.calls), (140, 20, 1))

    def test_messages_endpoint_not_duplicated(self):
        self.registry.providers[0] = replace(self.registry.providers[0], base_url="https://x/anthropic/v1/")
        self.call(self.response())
        self.assertEqual(self.factory.call_args.args[0], "https://x/anthropic")

    def test_error_only_result_is_not_a_verified_conclusion(self):
        for content in (
            {"type": "web_search_tool_result_error", "error_code": "unavailable"},
            [{"type": "web_search_tool_result_error", "error_code": "unavailable"}],
        ):
            with self.subTest(content=content):
                out = self.call(self.response(results=content))
                self.assertTrue(out.startswith("ERROR:"))
                self.assertIn("unavailable", out)
                self.assertNotIn("联网搜索结论", out)

    def test_success_with_partial_error_is_marked_and_sources_deduplicated(self):
        result = types.SimpleNamespace(type="web_search_result", url="https://official.example")
        out = self.call(self.response(results=[result, result,
            {"type": "web_search_tool_result_error", "error_code": "max_uses_exceeded"}]))
        self.assertIn("部分搜索失败", out)
        self.assertIn("max_uses_exceeded", out)
        self.assertEqual(out.count("https://official.example"), 1)

    def test_text_without_search_results_is_rejected(self):
        out = self.call(self.response(results=[]))
        self.assertTrue(out.startswith("ERROR:"))
        self.assertNotIn("天气结论", out)

    def test_incomplete_search_is_rejected_but_accounted(self):
        for reason in ("pause_turn", "max_tokens"):
            with self.subTest(reason=reason):
                out = self.call(self.response(stop_reason=reason))
                self.assertTrue(out.startswith("ERROR:"))
                self.assertIn("尚未完成", out)
        self.assertEqual(self.usage.by_model["deepseek/deepseek-flash"].calls, 2)

    def test_empty_answer_does_not_become_a_warning_only_conclusion(self):
        out = self.call(self.response(text="", results=[
            {"type": "web_search_result", "url": "https://official.example"},
            {"type": "web_search_tool_result_error", "error_code": "unavailable"},
        ]))
        self.assertIn("没有返回内容", out)
        self.assertNotIn("联网搜索结论", out)

    def test_transport_error_closes_client_and_returns_error(self):
        self.client.messages.create.side_effect = RuntimeError("boom")
        out = self.tool.handler(query="q")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertEqual(self.client.__exit__.call_count, 1)
        self.assertEqual(self.usage.by_model, {})


class TestBedrockSearch(unittest.TestCase):
    def setUp(self):
        self.registry = Registry([Provider("bedrock", "https://bedrock-runtime.us-west-2.amazonaws.com",
                                          IAM_PLACEHOLDER_KEY, (), aws_region="us-west-2")])
        self.usage = Usage()
        self.client = mock.MagicMock()
        self.client.__enter__.return_value = self.client
        self.client.responses.create.return_value = _response(citations=("https://official.example",))
        patcher = mock.patch.object(bedrock_search, "client", return_value=self.client)
        self.factory = patcher.start()
        self.addCleanup(patcher.stop)
        self.tool = _tool(self.registry, self.usage, provider="bedrock")

    def test_mantle_request_index_only_and_iam(self):
        self.assertTrue(self.tool.available())
        out = self.tool.handler(query="q")
        self.assertIn("https://official.example", out)
        self.assertIn("openai.gpt-5.6-luna", out)
        self.assertEqual(self.factory.call_args.args[:2], ("us-west-2", None))
        request = self.client.responses.create.call_args.kwargs
        self.assertEqual(request["tools"], [{"type": "web_search", "external_web_access": False}])
        self.assertEqual(request["max_output_tokens"], 4096)
        self.assertEqual(request["model"], "openai.gpt-5.6-luna")
        self.assertEqual(self.client.__exit__.call_count, 1)
        entry = self.usage.by_model["bedrock/openai.gpt-5.6-luna"]
        self.assertEqual((entry.prompt_tokens, entry.completion_tokens, entry.calls), (100, 20, 1))

    def test_bearer_token_passed_to_search_client(self):
        self.registry.providers[0] = replace(self.registry.providers[0], api_key="test-bearer")
        self.tool.handler(query="q")
        self.assertEqual(self.factory.call_args.args[:2], ("us-west-2", "test-bearer"))

    def test_incomplete_or_ungrounded_response_rejected(self):
        for reason in ("incomplete", "failed", "missing_sources", "missing_call"):
            with self.subTest(reason=reason):
                response = _response(citations=("https://official.example",))
                if reason == "missing_sources":
                    response = _response()
                elif reason == "missing_call":
                    response.output = response.output[1:]
                else:
                    response.status = reason
                self.client.responses.create.return_value = response
                self.assertTrue(self.tool.handler(query="q").startswith("ERROR:"))

    def test_missing_provider_explains_iam_activation(self):
        tool = _tool(_registry(), provider="bedrock")
        self.assertFalse(tool.available())
        out = tool.handler(query="q")
        self.assertIn("XIAOYU_BEDROCK_REGION", out)
        self.assertIn("AWS_BEARER_TOKEN_BEDROCK", out)

    def test_access_denied_does_not_fallback_or_raise(self):
        self.client.responses.create.side_effect = RuntimeError("AccessDenied")
        out = self.tool.handler(query="q")
        self.assertTrue(out.startswith("ERROR:"))
        self.assertIn("AccessDenied", out)
        self.assertEqual(self.client.__exit__.call_count, 1)


if __name__ == "__main__":
    unittest.main()
