"""错误分类器与主循环恢复路径的测试。不打网络。"""

from __future__ import annotations

import contextlib
import io
import types
import unittest
from unittest import mock

import httpx
import openai

from xiaoyu.errors import (
    ALL_KINDS,
    RETRY_AFTER_CAP,
    ContentFiltered,
    StreamTruncated,
    classify,
    retry_after_seconds,
)

from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk


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

    def test_balance_and_spend_limit_are_quota(self):
        """余额/消费上限耗尽：以前掉进 fatal，降级链不放行、不切网关兜底。"""
        request = httpx.Request("POST", "http://unused")
        samples = [
            #  DeepSeek 余额不足：HTTP 402
            openai.APIStatusError("Insufficient Balance", response=_response(402), body=None),
            #  Anthropic 余额不足是 400 invalid_request_error
            _DuckStatusError(
                "Your credit balance is too low to access the Anthropic API. "
                "Please go to Plans & Billing to upgrade or purchase credits.",
                400,
            ),
            RuntimeError("402 Payment Required"),
            RuntimeError("billing_not_active: Your account is not active, please check your billing details"),
        ]
        #  流式里冒出来的错误只带 body，文本里没有码
        for code in (
            "credit_balance_exhausted",
            "organization_spend_limit_exceeded",
            "project_spend_limit_exceeded",
            "organization_usage_limit_exceeded",
        ):
            samples.append(
                openai.APIError("request rejected", request, body={"code": code, "message": "x"})
            )
            duck = RuntimeError("request rejected")
            duck.body = {"error": {"code": code}}
            samples.append(duck)
        for exc in samples:
            with self.subTest(exc=exc):
                verdict = classify(exc)
                self.assertEqual(verdict.kind, "quota")
                self.assertFalse(verdict.retryable)

    def test_billing_link_in_rate_limit_text_stays_rate_limit(self):
        """限流文案里顺手附的 billing 链接不能把限流误判成额度耗尽（那会跳过退避）。"""
        for exc in (
            openai.RateLimitError(
                "Rate limit reached for requests. Visit https://platform.openai.com/account/billing "
                "to increase your limits.",
                response=_response(429),
                body=None,
            ),
            RuntimeError("Rate limit exceeded; see the billing page to raise limits"),
        ):
            with self.subTest(exc=exc):
                self.assertEqual(classify(exc).kind, "rate_limit")


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
            402: "quota",  # Payment Required：无论文案都是额度问题
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


def switch_notices(agent):
    return [m for m in agent.messages if "模型已切换" in str(m.get("content"))]


class ModelSwitchNoticeTest(AgentTestCase):
    """换了模型要告诉新模型：以上 assistant 回复不是它说的——否则它会把前任的
    自述（"当前模型看不了图"之类）当成关于自己的事实。"""

    def test_notice_rides_the_first_request_after_switch(self):
        agent = self.build([
            [chunk(content="甲"), usage_chunk(10, 1)],
            [chunk(content="乙"), usage_chunk(10, 1)],
            [chunk(content="丙"), usage_chunk(10, 1)],
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("一")
            agent.switch_model("other-model")
            agent.send("二")
            agent.send("三")
        notices = switch_notices(agent)
        self.assertEqual(len(notices), 1)
        text = notices[0]["content"]
        self.assertIn("main-model", text)
        self.assertIn("other-model", text)
        #  同一家：只写型号，不写 provider 前缀
        self.assertNotIn("/", text)
        #  发给新模型的那次请求里就带着（不是下一次才补）
        self.assertIn("模型已切换", str(self.client.completions.calls[1]["messages"][-1]["content"]))
        #  落在第二轮回复之前；注入不算真用户原话
        position = agent.messages.index(notices[0])
        self.assertEqual(agent.messages[position + 1]["content"], "乙")
        self.assertEqual(agent.last_user_text(), "三")

    def test_failed_request_leaves_no_notice(self):
        agent = self.build([
            [chunk(content="甲"), usage_chunk(10, 1)],
            ValueError("boom"),
            [chunk(content="乙"), usage_chunk(10, 1)],
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("一")
            agent.switch_model("other-model")
            with self.assertRaises(ValueError):
                agent.send("二")
            self.assertEqual(switch_notices(agent), [])
            agent.send("三")
        self.assertEqual(len(switch_notices(agent)), 1)

    def test_fallback_switch_is_announced(self):
        self.config.fallback_models = ["backup-model"]
        agent = self.build([
            [chunk(content="主模型答"), usage_chunk(10, 1)],
            rate_limit_error(),
            rate_limit_error(),
            rate_limit_error(),
            [chunk(content="备用模型顶上"), usage_chunk(10, 1)],
        ])
        with mock.patch("xiaoyu.agent.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            agent.send("一")
            agent.send("二")
        notices = switch_notices(agent)
        self.assertEqual(len(notices), 1)
        self.assertIn("backup-model", notices[0]["content"])

    def test_first_request_has_no_notice(self):
        agent = self.build([[chunk(content="甲"), usage_chunk(10, 1)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("一")
        self.assertEqual(switch_notices(agent), [])


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


def filtered_chunk():
    """chat 流里内容过滤的收尾 chunk：delta 为空，finish_reason=content_filter。"""
    delta = types.SimpleNamespace(content=None, tool_calls=None)
    choice = types.SimpleNamespace(delta=delta, finish_reason="content_filter")
    return types.SimpleNamespace(choices=[choice], usage=None)


def length_chunk():
    """输出撞 max_tokens 的收尾 chunk：finish_reason=length。"""
    delta = types.SimpleNamespace(content=None, tool_calls=None)
    choice = types.SimpleNamespace(delta=delta, finish_reason="length")
    return types.SimpleNamespace(choices=[choice], usage=None)


class LengthTruncationTest(AgentTestCase):
    """被长度上限截断：残缺工具调用不执行、不当空补全重发，轮末告诉用户与模型。"""

    def test_truncated_tool_call_dropped_and_turn_ends(self):
        agent = self.build([[
            chunk(content="我来写文件"),
            chunk(tool_calls=[call_fragment(0, "c1", "write_file", '{"path": "a.py", "content": "def')]),
            length_chunk(),
            usage_chunk(100, 50),
        ]])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send("写个文件")
        self.assertEqual(len(self.client.completions.calls), 1)
        self.assertFalse((self.root / "a.py").exists())
        last = agent.messages[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertFalse(last.get("tool_calls"))
        self.assertIn("长度上限", last["content"])
        self.assertIn("长度上限", buffer.getvalue())

    def test_complete_calls_before_the_cut_still_run(self):
        agent = self.build([
            [
                chunk(tool_calls=[call_fragment(0, "c1", "read_file", '{"path": "calc.py"}')]),
                chunk(tool_calls=[call_fragment(1, "c2", "write_file", '{"path": "b.py", "con')]),
                length_chunk(),
            ],
            [chunk(content="读完了"), usage_chunk(100, 5)],
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("读再写")
        calls = [m for m in agent.messages if m.get("tool_calls")]
        self.assertEqual([c["id"] for c in calls[0]["tool_calls"]], ["c1"])
        self.assertEqual(agent.last_assistant_text(), "读完了")

    def test_empty_truncated_completion_not_retried(self):
        agent = self.build([[length_chunk(), usage_chunk(100, 4096)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertEqual(len(self.client.completions.calls), 1)


class ContentFilterTest(AgentTestCase):
    """内容过滤是拒答不是断流：不能被当成空补全原样重发、也不该换模型。"""

    def test_classified_fatal(self):
        verdict = classify(ContentFiltered("被拦了"))
        self.assertEqual(verdict.kind, "fatal")
        self.assertFalse(verdict.retryable)
        self.assertIn("被拦了", verdict.hint)

    def test_empty_filtered_completion_raises_without_retry(self):
        self.config.fallback_models = ["backup-model"]
        agent = self.build([[filtered_chunk(), usage_chunk(100, 0)]])
        with mock.patch("xiaoyu.agent.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(ContentFiltered):
                agent.send("hi")
        #  只打了一次：没有空补全重发，也没有落到备用模型
        self.assertEqual(len(self.client.completions.calls), 1)

    def test_partial_text_kept_with_warning(self):
        agent = self.build([[chunk(content="前半段"), filtered_chunk(), usage_chunk(100, 5)]])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send("hi")
        self.assertEqual(agent.last_assistant_text(), "前半段")
        self.assertIn("内容过滤截断", buffer.getvalue())


def finish_chunk(reason: str = "tool_calls"):
    delta = types.SimpleNamespace(content=None, tool_calls=None)
    choice = types.SimpleNamespace(delta=delta, finish_reason=reason)
    return types.SimpleNamespace(choices=[choice], usage=None)


HALF_WRITE = '{"path": "a.py", "content": "de'


class StreamTruncationTest(AgentTestCase):
    """流在工具参数写到一半时结束、没有任何收尾信号：断流，原地重发而不是执行残缺调用。"""

    def test_classified_transient(self):
        verdict = classify(StreamTruncated("断了"))
        self.assertEqual(verdict.kind, "transient")
        self.assertTrue(verdict.retryable)

    def test_half_arguments_without_finish_or_usage_are_retried(self):
        agent = self.build([
            [chunk(tool_calls=[call_fragment(0, "c1", "write_file", HALF_WRITE)])],
            [
                chunk(tool_calls=[call_fragment(0, "c2", "write_file", '{"path": "a.py", "content": "done"}')]),
                usage_chunk(100, 10),
            ],
            [chunk(content="写好了"), usage_chunk(100, 5)],
        ])
        with mock.patch("xiaoyu.agent.time.sleep") as fake_sleep, contextlib.redirect_stdout(io.StringIO()):
            agent.send("写个文件")
        self.assertEqual(len(self.client.completions.calls), 3)
        #  走的是可重试错误的退避路径
        fake_sleep.assert_called_once()
        self.assertEqual((self.root / "a.py").read_text(encoding="utf-8"), "done")
        #  残缺调用没进历史，也没换来一条"参数不是合法 JSON"
        ids = [c["id"] for m in agent.messages for c in m.get("tool_calls") or []]
        self.assertEqual(ids, ["c2"])
        self.assertFalse(any("不是合法 JSON" in str(m.get("content")) for m in agent.messages))

    def test_half_arguments_with_usage_are_not_retried(self):
        """收到过 usage 就是正常结束（有的端点不发 finish_reason）：坏 JSON 走原报错路径。"""
        agent = self.build([
            [chunk(tool_calls=[call_fragment(0, "c1", "write_file", HALF_WRITE)]), usage_chunk(100, 10)],
            [chunk(content="我重试"), usage_chunk(100, 5)],
        ])
        with mock.patch("xiaoyu.agent.time.sleep") as fake_sleep, contextlib.redirect_stdout(io.StringIO()):
            agent.send("写个文件")
        self.assertEqual(len(self.client.completions.calls), 2)
        fake_sleep.assert_not_called()
        self.assertIn("不是合法 JSON", agent.messages[3]["content"])

    def test_half_arguments_with_finish_reason_are_not_retried(self):
        agent = self.build([
            [chunk(tool_calls=[call_fragment(0, "c1", "write_file", HALF_WRITE)]), finish_chunk()],
            [chunk(content="我重试")],
        ])
        with mock.patch("xiaoyu.agent.time.sleep") as fake_sleep, contextlib.redirect_stdout(io.StringIO()):
            agent.send("写个文件")
        self.assertEqual(len(self.client.completions.calls), 2)
        fake_sleep.assert_not_called()
        self.assertIn("不是合法 JSON", agent.messages[3]["content"])

    def test_complete_arguments_without_finish_still_run(self):
        """参数完整的调用哪怕没有收尾信号也照常执行（只拦残缺的）。"""
        agent = self.build([
            [chunk(tool_calls=[call_fragment(0, "c1", "read_file", '{"path": "calc.py"}')])],
            [chunk(content="读完了")],
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("读")
        self.assertEqual(len(self.client.completions.calls), 2)
        self.assertIn("def add", agent.trace[0]["output"])

    def test_retries_exhausted_raise_stream_truncated(self):
        agent = self.build([
            [chunk(tool_calls=[call_fragment(0, f"c{n}", "write_file", HALF_WRITE)])] for n in range(3)
        ])
        with mock.patch("xiaoyu.agent.time.sleep"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(StreamTruncated):
                agent.send("写个文件")
        self.assertFalse((self.root / "a.py").exists())
