"""错误分类器与主循环恢复路径的测试。不打网络。"""

from __future__ import annotations

import unittest
from unittest import mock

import httpx
import openai

from xiaoyu.errors import ALL_KINDS, RETRY_AFTER_CAP, classify, retry_after_seconds

from .test_agent_paths import AgentTestCase, chunk, usage_chunk


def _response(status: int, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, request=httpx.Request("POST", "http://unused"), headers=headers
    )


def rate_limit_error(retry_after: str | None = None) -> openai.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after else None
    return openai.RateLimitError(
        "rate limited", response=_response(429, headers), body=None
    )


class ClassifyTest(unittest.TestCase):
    def test_rate_limit_is_retryable(self):
        verdict = classify(rate_limit_error())
        self.assertEqual(verdict.kind, "rate_limit")
        self.assertTrue(verdict.retryable)
        self.assertFalse(verdict.should_compact)

    def test_rate_limit_by_message_fallback(self):
        #  LiteLLM 转写后可能不是 openai 的异常类型，按文本兜底
        verdict = classify(RuntimeError("Provider said: rate limit exceeded"))
        self.assertEqual(verdict.kind, "rate_limit")

    def test_timeout_and_connection_are_transient(self):
        for exc in (
            openai.APITimeoutError(request=httpx.Request("POST", "http://unused")),
            openai.APIConnectionError(request=httpx.Request("POST", "http://unused")),
        ):
            verdict = classify(exc)
            self.assertEqual(verdict.kind, "transient", exc)
            self.assertTrue(verdict.retryable)

    def test_context_overflow_wants_compaction(self):
        verdict = classify(
            openai.BadRequestError(
                "This model's maximum context length is 128000 tokens",
                response=_response(400),
                body=None,
            )
        )
        self.assertEqual(verdict.kind, "context_overflow")
        self.assertTrue(verdict.retryable)
        self.assertTrue(verdict.should_compact)

    def test_auth_is_fatal_with_hint(self):
        verdict = classify(
            openai.AuthenticationError("bad key", response=_response(401), body=None)
        )
        self.assertFalse(verdict.retryable)
        self.assertIn("XIAOYU_API_KEY", verdict.hint)

    def test_unknown_error_is_fatal(self):
        verdict = classify(ValueError("something odd"))
        self.assertEqual(verdict.kind, "fatal")
        self.assertFalse(verdict.retryable)

    def test_bedrock_throttle_text_is_rate_limit_not_overflow(self):
        """Bedrock 限流原文带 "Too many tokens" 字样，撞 _CONTEXT_MARKERS——
        误判成超限会触发一次无意义的压缩。钉住：这是限流，不许压缩。"""
        for exc in (
            RuntimeError("ThrottlingException: Too many tokens, please wait before trying again."),
            openai.RateLimitError(
                "Too many tokens, please wait before trying again.",
                response=_response(429),
                body=None,
            ),
        ):
            verdict = classify(exc)
            self.assertEqual(verdict.kind, "rate_limit", exc)
            self.assertFalse(verdict.should_compact, exc)

    def test_throttling_wording_is_rate_limit(self):
        #  AWS 系措辞不带 "rate limit"/"429"，靠 throttl 词根兜底
        verdict = classify(RuntimeError("ThrottlingException: Rate exceeded"))
        self.assertEqual(verdict.kind, "rate_limit")

    def test_quota_exhaustion_is_not_retryable(self):
        """额度用尽常披着 429/RateLimitError 的皮——限流等得起，额度等不来。
        误判成可重试限流会无限退避白挨。钉住：不可重试、不压缩。"""
        for exc in (
            openai.RateLimitError(
                "You exceeded your current quota, please check your plan and billing details.",
                response=_response(429),
                body=None,
            ),
            RuntimeError("Budget has been exceeded! Current cost: 10.4; Max budget: 10.0"),
            RuntimeError("insufficient_quota"),
        ):
            verdict = classify(exc)
            self.assertEqual(verdict.kind, "quota", exc)
            self.assertFalse(verdict.retryable, exc)
            self.assertFalse(verdict.should_compact, exc)


class _DuckStatusError(Exception):
    """anthropic SDK 异常的最小鸭子型：同为 Stainless 生成，带 status_code 与
    response.headers。classify 刻意不 import anthropic，所以测试也按鸭子造。"""

    def __init__(self, message: str, status_code: int, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = _response(status_code, headers)


class AnthropicShapedTest(unittest.TestCase):
    """Messages 协议分支抛的是 anthropic SDK 的异常——分类靠鸭子 status_code。"""

    def test_status_codes_classify(self):
        cases = {
            401: "auth",
            403: "auth",
            429: "rate_limit",
            500: "transient",
            529: "transient",  # anthropic 的 overloaded_error
        }
        for status, kind in cases.items():
            with self.subTest(status=status):
                self.assertEqual(classify(_DuckStatusError("err", status)).kind, kind)

    def test_prompt_too_long_is_context_overflow(self):
        """Anthropic 超限 400 的原文措辞，已有 marker 覆盖——这条钉住不许回归。"""
        verdict = classify(
            _DuckStatusError("prompt is too long: 210015 tokens > 200000 maximum", 400)
        )
        self.assertEqual(verdict.kind, "context_overflow")
        self.assertTrue(verdict.should_compact)

    def test_httpx_cause_is_transient(self):
        """anthropic 的连接/超时异常是 `raise ... from <httpx 异常>`，认底因。"""
        exc = RuntimeError("Connection error.")
        exc.__cause__ = httpx.ConnectError("boom")
        self.assertEqual(classify(exc).kind, "transient")

    def test_retry_after_header_is_read_from_duck_response(self):
        """retry_after_seconds 本来就是鸭子取值，anthropic 异常零改动可用。"""
        self.assertEqual(
            retry_after_seconds(_DuckStatusError("err", 429, {"retry-after": "7"})), 7.0
        )


class RetryAfterTest(unittest.TestCase):
    def test_numeric_header(self):
        self.assertEqual(retry_after_seconds(rate_limit_error("7")), 7.0)

    def test_missing_header_or_response(self):
        self.assertIsNone(retry_after_seconds(rate_limit_error()))
        self.assertIsNone(retry_after_seconds(ValueError("没有 response")))

    def test_garbage_and_nonpositive_values(self):
        #  HTTP-date 形式的 Retry-After 不解析（极少见，不为它引入日期解析）
        self.assertIsNone(retry_after_seconds(rate_limit_error("Wed, 21 Oct 2026 07:28:00 GMT")))
        self.assertIsNone(retry_after_seconds(rate_limit_error("0")))

    def test_capped(self):
        #  服务端偶尔给几小时后的值，交互场景等不了
        self.assertEqual(retry_after_seconds(rate_limit_error("7200")), RETRY_AFTER_CAP)


class AllKindsTest(unittest.TestCase):
    """遍历全部错误分类（错误枚举配 all_errors() + 遍历测试）：
    每一类都要有可构造的样本、hint 非空。新增分类忘了配样本，这里当场失败。"""

    def sample_errors(self) -> dict[str, Exception]:
        return {
            "rate_limit": rate_limit_error(),
            "transient": openai.APITimeoutError(request=httpx.Request("POST", "http://unused")),
            "context_overflow": openai.BadRequestError(
                "prompt is too long", response=_response(400), body=None
            ),
            "quota": openai.RateLimitError(
                "You exceeded your current quota, please check your plan and billing details.",
                response=_response(429),
                body=None,
            ),
            "auth": openai.AuthenticationError("bad key", response=_response(401), body=None),
            "fatal": ValueError("奇怪的错"),
        }

    def test_every_kind_has_sample_and_nonempty_hint(self):
        samples = self.sample_errors()
        self.assertEqual(set(samples), set(ALL_KINDS), "ALL_KINDS 与样本清单必须同步")
        for kind, exc in samples.items():
            verdict = classify(exc)
            self.assertEqual(verdict.kind, kind, exc)
            self.assertTrue(verdict.hint.strip(), f"{kind} 的 hint 不能为空")
            #  展示路径不炸：重试提示就是拿 hint 直接格式化的
            self.assertIn(verdict.hint, f"[{verdict.hint}，1.0s 后重试（1/2）]")


class RecoveryLoopTest(AgentTestCase):
    """主循环的重试行为：用假 client 脚本注入错误。"""

    def test_retries_rate_limit_then_succeeds(self):
        script = [
            rate_limit_error(),
            [chunk(content="恢复了"), usage_chunk(100, 10)],
        ]
        agent = self.build(script)
        with mock.patch("xiaoyu.agent.time.sleep") as fake_sleep:
            agent.send("hi")
        self.assertEqual(agent.last_assistant_text(), "恢复了")
        fake_sleep.assert_called_once()

    def test_fatal_error_raises_immediately(self):
        script = [ValueError("boom")]
        agent = self.build(script)
        with self.assertRaises(ValueError):
            agent.send("hi")
        #  脚本只有一项且被消费，说明没有多余重试
        self.assertEqual(len(self.client.completions.script), 0)

    def test_gives_up_after_max_attempts(self):
        script = [rate_limit_error(), rate_limit_error(), rate_limit_error()]
        agent = self.build(script)
        with mock.patch("xiaoyu.agent.time.sleep"), self.assertRaises(openai.RateLimitError):
            agent.send("hi")
        self.assertEqual(len(self.client.completions.script), 0, "应该重试满 3 次")

    def test_retry_after_header_overrides_backoff(self):
        script = [
            rate_limit_error("30"),
            [chunk(content="恢复了"), usage_chunk(100, 10)],
        ]
        agent = self.build(script)
        with mock.patch("xiaoyu.agent.time.sleep") as fake_sleep, mock.patch(
            "xiaoyu.agent.random.uniform", return_value=1.0
        ):
            agent.send("hi")
        #  服务端说等 30s，就不该按默认 2s 退避
        fake_sleep.assert_called_once_with(30.0)

    def test_backoff_has_jitter_within_bounds(self):
        script = [
            rate_limit_error(),
            rate_limit_error(),
            [chunk(content="恢复了"), usage_chunk(100, 10)],
        ]
        agent = self.build(script)
        with mock.patch("xiaoyu.agent.time.sleep") as fake_sleep:
            agent.send("hi")
        waits = [call.args[0] for call in fake_sleep.call_args_list]
        #  基准 2s、4s，jitter ±25%
        self.assertEqual(len(waits), 2)
        self.assertTrue(2 * 0.75 <= waits[0] <= 2 * 1.25, waits)
        self.assertTrue(4 * 0.75 <= waits[1] <= 4 * 1.25, waits)

    def test_sdk_level_retries_disabled(self):
        """重试必须只有 _stream_with_recovery 一层：SDK 层叠加会出现 3×3=9 次假账。"""
        from xiaoyu.agent import Agent
        from xiaoyu.config import Config

        #  client 的构造点搬到了 providers.Registry.client（按 provider 缓存），
        #  这条约定跟着搬，断言的仍是同一件事。
        with mock.patch("xiaoyu.providers.OpenAI") as fake_openai, mock.patch(
            "xiaoyu.providers.find_api_key", return_value="k"
        ):
            Agent(self.config).registry.client("gateway")
        self.assertEqual(fake_openai.call_args.kwargs.get("max_retries"), 0)

    def test_context_overflow_triggers_forced_compaction(self):
        overflow = openai.BadRequestError(
            "maximum context length exceeded",
            response=_response(400),
            body=None,
        )
        script = [overflow, [chunk(content="ok"), usage_chunk(100, 5)]]
        agent = self.build(script)
        with mock.patch("xiaoyu.agent.time.sleep"), mock.patch.object(
            agent, "maybe_compact"
        ) as fake_compact:
            agent.send("hi")
        fake_compact.assert_any_call(force=True)
        self.assertEqual(agent.last_assistant_text(), "ok")


class FallbackChainTest(AgentTestCase):
    """备用模型降级链：retry 内层耗尽后外层切模型，同一份会话原样继续。"""

    def test_switches_after_retries_exhausted(self):
        self.config.fallback_models = ["backup-model"]
        script = [
            rate_limit_error(),
            rate_limit_error(),
            rate_limit_error(),
            [chunk(content="备用模型顶上"), usage_chunk(100, 10)],
        ]
        agent = self.build(script)
        with mock.patch("xiaoyu.agent.time.sleep"):
            agent.send("hi")
        self.assertEqual(agent.last_assistant_text(), "备用模型顶上")
        models = [call["model"] for call in self.client.completions.calls]
        self.assertEqual(models, ["main-model"] * 3 + ["backup-model"])
        #  粘性切换：主模型宕机期间不必每次请求都白等一轮退避；/model 可切回
        self.assertEqual(agent.config.model, "backup-model")
        #  换模型后的用量记到备用路由名下（按路由分账不能混：同名模型可能跑在两家上）
        self.assertIn("gateway/backup-model", agent.usage.by_model)

    def test_fatal_error_does_not_switch(self):
        self.config.fallback_models = ["backup-model"]
        script = [ValueError("boom")]
        agent = self.build(script)
        with self.assertRaises(ValueError):
            agent.send("hi")
        self.assertEqual(len(self.client.completions.script), 0, "配置类错误不该尝试备用模型")
        self.assertEqual(agent.config.model, "main-model")

    def test_all_models_exhausted_raises_last_error(self):
        self.config.fallback_models = ["backup-model"]
        script = [rate_limit_error() for _ in range(6)]
        agent = self.build(script)
        with mock.patch("xiaoyu.agent.time.sleep"), self.assertRaises(openai.RateLimitError):
            agent.send("hi")
        #  主 3 次 + 备用 3 次，全部试完
        self.assertEqual(len(self.client.completions.script), 0)

    def test_no_fallback_configured_keeps_old_behavior(self):
        script = [rate_limit_error(), rate_limit_error(), rate_limit_error()]
        agent = self.build(script)
        with mock.patch("xiaoyu.agent.time.sleep"), self.assertRaises(openai.RateLimitError):
            agent.send("hi")
        self.assertEqual(agent.config.model, "main-model")

    def test_quota_does_not_switch_within_same_provider(self):
        """额度是账户级的：同一家换个模型名照样没额度，空转一轮还误导用户。
        （换**另一家**该顶上——那条在 test_providers.py 的网关兜底套件里。）"""
        self.config.fallback_models = ["backup-model"]
        quota = openai.RateLimitError(
            "You exceeded your current quota, please check your plan and billing details.",
            response=_response(429),
            body=None,
        )
        script = [quota]
        agent = self.build(script)
        with self.assertRaises(openai.RateLimitError):
            agent.send("hi")
        #  一次都不该重试、也不该碰备用模型
        self.assertEqual(len(self.client.completions.script), 0)
        self.assertEqual(agent.config.model, "main-model")


if __name__ == "__main__":
    unittest.main()
