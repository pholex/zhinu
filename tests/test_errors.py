"""错误分类器与主循环恢复路径的测试。不打网络。"""

from __future__ import annotations

import contextlib
import io
import types
import unittest
from unittest import mock

import httpx2
import openai

from xiaoyu.errors import (
    ALL_KINDS,
    RETRY_AFTER_CAP,
    ContentFiltered,
    StreamFailed,
    StreamTruncated,
    classify,
    retry_after_seconds,
)

from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk


def _response(status: int, headers: dict[str, str] | None = None) -> httpx2.Response:
    return httpx2.Response(
        status, request=httpx2.Request("POST", "http://unused"), headers=headers
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
            openai.APITimeoutError(request=httpx2.Request("POST", "http://unused")),
            openai.APIConnectionError(request=httpx2.Request("POST", "http://unused")),
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
        request = httpx2.Request("POST", "http://unused")
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
        """anthropic 的连接/超时异常是 `raise ... from <httpx2 异常>`，认底因。"""
        exc = RuntimeError("Connection error.")
        exc.__cause__ = httpx2.ConnectError("boom")
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
        self.assertIsNone(retry_after_seconds(rate_limit_error("in a while")))
        self.assertIsNone(retry_after_seconds(rate_limit_error("0")))
        #  已经过去的时刻：不用等
        self.assertIsNone(retry_after_seconds(rate_limit_error("Wed, 21 Oct 2015 07:28:00 GMT")))

    def test_http_date(self):
        from datetime import datetime, timedelta, timezone
        from email.utils import format_datetime

        from xiaoyu.errors import retry_after_asked

        moment = datetime.now(timezone.utc) + timedelta(seconds=30)
        asked = retry_after_asked(rate_limit_error(format_datetime(moment, usegmt=True)))
        self.assertIsNotNone(asked)
        self.assertTrue(25 < asked <= 30, asked)

    def test_asked_is_not_capped(self):
        from xiaoyu.errors import retry_after_asked

        self.assertEqual(retry_after_asked(rate_limit_error("7200")), 7200.0)

    def test_told_not_to_retry(self):
        from xiaoyu.errors import told_not_to_retry

        self.assertTrue(told_not_to_retry(_DuckStatusError("err", 429, {"x-should-retry": "false"})))
        self.assertFalse(told_not_to_retry(_DuckStatusError("err", 429, {"x-should-retry": "true"})))
        self.assertFalse(told_not_to_retry(rate_limit_error("7")))
        self.assertFalse(told_not_to_retry(ValueError("没有 response")))

    def test_capped(self):
        #  服务端偶尔给几小时后的值，交互场景等不了
        self.assertEqual(retry_after_seconds(rate_limit_error("7200")), RETRY_AFTER_CAP)

    def test_millisecond_header_wins(self):
        """OpenAI 系两个头一起发，秒级那个向下取整——照它重试仍在窗口内。"""
        exc = _DuckStatusError("err", 429, {"retry-after": "1", "retry-after-ms": "1400"})
        self.assertEqual(retry_after_seconds(exc), 1.4)

    def test_millisecond_header_alone(self):
        self.assertEqual(
            retry_after_seconds(_DuckStatusError("err", 429, {"retry-after-ms": "2500"})), 2.5
        )

    def test_falls_back_to_seconds_when_ms_unusable(self):
        #  毫秒头是垃圾/非正数时不该连秒级那个一起丢掉
        for ms in ("", "abc", "0"):
            exc = _DuckStatusError("err", 429, {"retry-after": "7", "retry-after-ms": ms})
            self.assertEqual(retry_after_seconds(exc), 7.0, ms)

    def test_millisecond_header_capped(self):
        exc = _DuckStatusError("err", 429, {"retry-after-ms": "7200000"})
        self.assertEqual(retry_after_seconds(exc), RETRY_AFTER_CAP)


class AllKindsTest(unittest.TestCase):
    """遍历全部错误分类（错误枚举配 all_errors() + 遍历测试）：
    每一类都要有可构造的样本、hint 非空。新增分类忘了配样本，这里当场失败。"""

    def sample_errors(self) -> dict[str, Exception]:
        return {
            "rate_limit": rate_limit_error(),
            "transient": openai.APITimeoutError(request=httpx2.Request("POST", "http://unused")),
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
        with mock.patch("xiaoyu.agent.Agent._sleep") as fake_sleep:
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
        with mock.patch("xiaoyu.agent.Agent._sleep"), self.assertRaises(openai.RateLimitError):
            agent.send("hi")
        self.assertEqual(len(self.client.completions.script), 0, "应该重试满 3 次")

    def test_retry_after_header_overrides_backoff(self):
        script = [
            rate_limit_error("30"),
            [chunk(content="恢复了"), usage_chunk(100, 10)],
        ]
        agent = self.build(script)
        with mock.patch("xiaoyu.agent.Agent._sleep") as fake_sleep, mock.patch(
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
        with mock.patch("xiaoyu.agent.Agent._sleep") as fake_sleep:
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
        agent.messages += [
            {"role": "user", "content": "早先的任务"},
            {"role": "assistant", "content": "阿" * 4000},
        ]

        def compacting(force: bool = False) -> None:
            #  重发的前提是历史真的变小了：这里替压缩把那条长回答换掉
            if force:
                agent.messages[2] = {"role": "assistant", "content": "（已压缩）"}

        with mock.patch("xiaoyu.agent.Agent._sleep"), mock.patch.object(
            agent, "maybe_compact", side_effect=compacting
        ) as fake_compact:
            agent.send("hi")
        fake_compact.assert_any_call(force=True)
        self.assertEqual(agent.last_assistant_text(), "ok")


def switch_notices(agent):
    return [m for m in agent.messages if "模型已切换" in str(m.get("content"))]


class ThinkingRejectedTest(AgentTestCase):
    """服务端拒收回传的 thinking 块：不修的话每次请求都是同一个 400，会话卡死。"""

    REJECTION = "messages.1.content.0: Invalid `signature` in `thinking` block"

    def rejected(self, message: str = REJECTION, status: int = 400):
        return openai.BadRequestError(message, response=_response(status), body=None)

    def with_thinking(self, agent):
        agent.messages.append({"role": "user", "content": "先前的问题"})
        agent.messages.append({
            "role": "assistant", "content": "先前的回答",
            "_reasoning": {"items": [
                {"type": "thinking", "thinking": "想了想", "signature": "sig"},
                {"type": "redacted_thinking", "data": "xx"},
            ]},
        })

    def test_recognition_is_narrow(self):
        from xiaoyu.errors import thinking_rejected

        self.assertTrue(thinking_rejected(self.rejected()))
        self.assertTrue(thinking_rejected(self.rejected(
            "`thinking` or `redacted_thinking` blocks in the latest assistant message "
            "cannot be modified")))
        self.assertFalse(thinking_rejected(self.rejected("max_tokens must be greater than thinking.budget_tokens")))
        self.assertFalse(thinking_rejected(self.rejected("invalid tool schema")))
        self.assertFalse(thinking_rejected(self.rejected(status=429)))

    def test_blocks_are_dropped_and_the_request_resent_once(self):
        agent = self.build([self.rejected(), [chunk(content="好了"), usage_chunk(10, 2)]])
        self.with_thinking(agent)
        buffer = io.StringIO()
        with mock.patch("xiaoyu.agent.Agent._sleep") as sleep, contextlib.redirect_stdout(buffer):
            agent.send("继续")
        sleep.assert_not_called()
        self.assertEqual(agent.last_assistant_text(), "好了")
        self.assertEqual(len(self.client.completions.calls), 2)
        self.assertFalse(any("_reasoning" in m for m in agent.messages))
        self.assertIn("拒收了历史里的推理内容", buffer.getvalue())

    def test_nothing_left_to_drop_means_the_error_stands(self):
        agent = self.build([self.rejected()])
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(openai.BadRequestError):
            agent.send("继续")
        self.assertEqual(len(self.client.completions.calls), 1)

    def test_a_second_rejection_does_not_loop(self):
        agent = self.build([self.rejected(), self.rejected()])
        self.with_thinking(agent)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(openai.BadRequestError):
            agent.send("继续")
        self.assertEqual(len(self.client.completions.calls), 2)


class RequestLogTest(AgentTestCase):
    """每次模型请求（含每次重试）在会话日志里留一条事实。"""

    class Log:
        def __init__(self):
            self.events = []

        def event(self, kind, **fields):
            self.events.append((kind, fields))

        def append(self, message):
            pass

    def requests(self, agent):
        return [fields for kind, fields in agent.session_log.events if kind == "request"]

    def run_with_log(self, script):
        agent = self.build(script)
        agent.session_log = self.Log()
        agent._sleep = lambda seconds: None
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        return agent

    def test_failed_attempt_and_its_retry_are_both_recorded(self):
        agent = self.run_with_log([rate_limit_error(), [chunk(content="好了"), usage_chunk(120, 5)]])
        first, second = self.requests(agent)
        self.assertEqual((first["attempt"], first["outcome"]), (1, "error"))
        self.assertEqual(first["error_kind"], "rate_limit")
        self.assertEqual(first["status"], 429)
        self.assertEqual(first["error"], "RateLimitError")
        self.assertGreater(first["wait_s"], 0)
        self.assertEqual((second["attempt"], second["outcome"]), (2, "ok"))
        self.assertEqual((second["prompt_tokens"], second["completion_tokens"]), (120, 5))
        for record in (first, second):
            self.assertEqual((record["provider"], record["model"]), ("gateway", "main-model"))
            self.assertIn("total_ms", record)

    def test_request_id_and_code_are_kept_when_the_upstream_gives_them(self):
        response = httpx2.Response(
            429, request=httpx2.Request("POST", "http://unused"),
            headers={"x-request-id": "req-abc"},
        )
        exc = openai.RateLimitError("慢一点", response=response, body={"error": {"code": "1302"}})
        agent = self.run_with_log([exc, [chunk(content="好了"), usage_chunk(10, 1)]])
        first = self.requests(agent)[0]
        self.assertEqual(first["request_id"], "req-abc")
        self.assertEqual(first["code"], "1302")

    def test_truncated_reply_is_marked(self):
        agent = self.run_with_log([
            [chunk(content="说到一半"), length_chunk(), usage_chunk(100, 50)],
            [chunk(content="说完了"), usage_chunk(100, 5)],
        ])
        first, second = self.requests(agent)
        self.assertEqual(first["finish"], "length")
        self.assertNotIn("finish", second)

    def test_a_broken_log_never_breaks_the_request(self):
        agent = self.build([[chunk(content="好了"), usage_chunk(10, 1)]])

        class Broken(self.Log):
            def event(self, kind, **fields):
                if kind == "request":
                    raise OSError("磁盘满了")
                super().event(kind, **fields)

        agent.session_log = Broken()
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertEqual(agent.last_assistant_text(), "好了")


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
        with mock.patch("xiaoyu.agent.Agent._sleep"), contextlib.redirect_stdout(io.StringIO()):
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
        with mock.patch("xiaoyu.agent.Agent._sleep"):
            agent.send("hi")
        self.assertEqual(agent.last_assistant_text(), "备用模型顶上")
        models = [call["model"] for call in self.client.completions.calls]
        self.assertEqual(models, ["main-model"] * 3 + ["backup-model"])
        #  粘性切换：主模型宕机期间不必每次请求都白等一轮退避；/model 可切回
        self.assertEqual(agent.config.model, "backup-model")
        #  换模型后的用量记到备用路由名下（按路由分账不能混：同名模型可能跑在两家上）
        self.assertIn("gateway/backup-model", agent.usage.by_model)

    def test_long_retry_after_switches_route_instead_of_waiting(self):
        """服务端要求等 120 秒：原地等到上限再发必然再挨一次，白耗预算。"""
        self.config.fallback_models = ["backup-model"]
        agent = self.build([rate_limit_error("120"), [chunk(content="备用顶上"), usage_chunk(10, 2)]])
        buffer = io.StringIO()
        with mock.patch("xiaoyu.agent.Agent._sleep") as sleep, contextlib.redirect_stdout(buffer):
            agent.send("hi")
        sleep.assert_not_called()
        models = [call["model"] for call in self.client.completions.calls]
        self.assertEqual(models, ["main-model", "backup-model"])
        self.assertIn("要求等 120s", buffer.getvalue())

    def test_long_retry_after_is_honoured_when_there_is_nowhere_else_to_go(self):
        agent = self.build([rate_limit_error("120"), [chunk(content="等到了"), usage_chunk(10, 2)]])
        with mock.patch("xiaoyu.agent.Agent._sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        (waited,) = [call.args[0] for call in sleep.call_args_list]
        #  照它说的等、只往上抖：早于它说的时刻醒来必然再挨一次
        self.assertTrue(120 <= waited <= 132, waited)
        self.assertEqual(agent.last_assistant_text(), "等到了")

    def test_hours_long_retry_after_is_reported_not_waited(self):
        agent = self.build([rate_limit_error("7200")])
        buffer = io.StringIO()
        with mock.patch("xiaoyu.agent.Agent._sleep") as sleep, contextlib.redirect_stdout(buffer), \
                self.assertRaises(openai.RateLimitError):
            agent.send("hi")
        sleep.assert_not_called()
        self.assertEqual(len(self.client.completions.calls), 1)
        self.assertIn("7200s", buffer.getvalue())

    def test_short_retry_after_is_waited_in_place(self):
        self.config.fallback_models = ["backup-model"]
        agent = self.build([rate_limit_error("7"), [chunk(content="好了"), usage_chunk(10, 2)]])
        with mock.patch("xiaoyu.agent.Agent._sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        (waited,) = [call.args[0] for call in sleep.call_args_list]
        self.assertTrue(7 <= waited <= 7.7, waited)
        self.assertEqual([c["model"] for c in self.client.completions.calls], ["main-model"] * 2)

    def test_server_saying_do_not_retry_is_obeyed(self):
        self.config.fallback_models = ["backup-model"]
        told = openai.InternalServerError(
            "别重试", response=_response(500, {"x-should-retry": "false"}), body=None
        )
        agent = self.build([told, [chunk(content="备用顶上"), usage_chunk(10, 2)]])
        with mock.patch("xiaoyu.agent.Agent._sleep") as sleep, contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        sleep.assert_not_called()
        self.assertEqual(
            [c["model"] for c in self.client.completions.calls], ["main-model", "backup-model"]
        )

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
        with mock.patch("xiaoyu.agent.Agent._sleep"), self.assertRaises(openai.RateLimitError):
            agent.send("hi")
        #  主 3 次 + 备用 3 次，全部试完
        self.assertEqual(len(self.client.completions.script), 0)

    def test_no_fallback_configured_keeps_old_behavior(self):
        script = [rate_limit_error(), rate_limit_error(), rate_limit_error()]
        agent = self.build(script)
        with mock.patch("xiaoyu.agent.Agent._sleep"), self.assertRaises(openai.RateLimitError):
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

    CUT_WRITE = [
        chunk(content="我来写文件"),
        chunk(tool_calls=[call_fragment(0, "c1", "write_file", '{"path": "a.py", "content": "def')]),
        length_chunk(),
        usage_chunk(100, 50),
    ]

    def test_truncated_tool_call_is_dropped_then_the_model_is_asked_to_continue(self):
        """「发继续可接着写」只对坐在终端前的人成立：无人值守的路径上没人会发。"""
        from xiaoyu.agent import TRUNCATED_CONTINUE_NUDGE

        agent = self.build([
            self.CUT_WRITE,
            [chunk(tool_calls=[call_fragment(
                0, "c2", "write_file", '{"path": "a.py", "content": "def f(): pass\\n"}')]),
             usage_chunk(100, 20)],
            [chunk(content="写好了"), usage_chunk(100, 5)],
        ])
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send("写个文件")
        self.assertEqual(len(self.client.completions.calls), 3)
        self.assertTrue((self.root / "a.py").exists())
        cut = next(m for m in agent.messages if m["role"] == "assistant")
        self.assertFalse(cut.get("tool_calls"))
        self.assertIn("长度上限", cut["content"])
        nudges = [m for m in agent.messages if m.get("content") == TRUNCATED_CONTINUE_NUDGE]
        self.assertEqual(len(nudges), 1)
        self.assertIn("拆成", TRUNCATED_CONTINUE_NUDGE)
        self.assertIn("接着写（1/3）", buffer.getvalue())
        self.assertEqual(agent.last_stop, "done")
        self.assertEqual(agent.last_assistant_text(), "写好了")

    def test_gives_up_after_three_continuations_and_says_so(self):
        agent = self.build([self.CUT_WRITE] * 4)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent.send("写个文件")
        self.assertEqual(len(self.client.completions.calls), 4)
        self.assertEqual(agent.last_stop, "truncated")
        self.assertIn("不再自动续写", buffer.getvalue())
        self.assertFalse((self.root / "a.py").exists())
        self.assertEqual(agent.messages[-1]["role"], "assistant")

    def test_continuation_nudge_is_not_mistaken_for_the_users_words(self):
        from xiaoyu.agent import SYNTHETIC_USER_TEXTS, TRUNCATED_CONTINUE_NUDGE

        self.assertIn(TRUNCATED_CONTINUE_NUDGE, SYNTHETIC_USER_TEXTS)

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
        #  被丢弃的那个调用还得重来：工具结果之后同样提醒拆小，且顺序是
        #  assistant(调用) → tool 结果 → 提醒
        from xiaoyu.agent import TRUNCATED_CONTINUE_NUDGE

        roles = [m["role"] for m in agent.messages[1:]]
        self.assertEqual(roles, ["user", "assistant", "tool", "user", "assistant"])
        self.assertEqual(agent.messages[4]["content"], TRUNCATED_CONTINUE_NUDGE)

    def test_empty_truncated_completion_not_retried(self):
        """什么都没吐出来就撞了上限（输出额度被推理吃光）：没有断点可接。"""
        agent = self.build([[length_chunk(), usage_chunk(100, 4096)]])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("hi")
        self.assertEqual(len(self.client.completions.calls), 1)
        self.assertEqual(agent.last_stop, "truncated")

    def test_truncated_text_reply_is_continued(self):
        agent = self.build([
            [chunk(content="第一段，说到一半"), length_chunk(), usage_chunk(100, 50)],
            [chunk(content="接着说完了"), usage_chunk(100, 5)],
        ])
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("讲讲")
        self.assertEqual(len(self.client.completions.calls), 2)
        self.assertEqual(agent.last_stop, "done")
        self.assertEqual(agent.last_assistant_text(), "接着说完了")


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
        with mock.patch("xiaoyu.agent.Agent._sleep"), contextlib.redirect_stdout(io.StringIO()):
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
        with mock.patch("xiaoyu.agent.Agent._sleep") as fake_sleep, contextlib.redirect_stdout(io.StringIO()):
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
        with mock.patch("xiaoyu.agent.Agent._sleep") as fake_sleep, contextlib.redirect_stdout(io.StringIO()):
            agent.send("写个文件")
        self.assertEqual(len(self.client.completions.calls), 2)
        fake_sleep.assert_not_called()
        self.assertIn("不是合法 JSON", agent.messages[3]["content"])

    def test_half_arguments_with_finish_reason_are_not_retried(self):
        agent = self.build([
            [chunk(tool_calls=[call_fragment(0, "c1", "write_file", HALF_WRITE)]), finish_chunk()],
            [chunk(content="我重试")],
        ])
        with mock.patch("xiaoyu.agent.Agent._sleep") as fake_sleep, contextlib.redirect_stdout(io.StringIO()):
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
        with mock.patch("xiaoyu.agent.Agent._sleep"), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(StreamTruncated):
                agent.send("写个文件")
        self.assertFalse((self.root / "a.py").exists())


def _stream_error(message: str, body=None) -> openai.APIError:
    """流内错误事件的样子：没有状态码的裸 APIError。"""
    return openai.APIError(message, request=httpx2.Request("POST", "http://unused"), body=body)


class ContextOverflowWordingTest(unittest.TestCase):
    """超限漏判的代价：fatal 是整轮直接死、不压缩；transient 是同一个超大请求
    带退避原样重发，烧光重试预算也不触发压缩。"""

    WORDINGS = (
        "input length and max_tokens exceed context limit: 188240 + 21333 > 200000",
        "Your input exceeds the context window of this model",
        "The input token count (1200000) exceeds the maximum number of tokens allowed (1048576)",
        "Range of input length should be [1, 98304]",
        "total message token length exceed model limit",
        "prompt is too long: 215000 tokens > 200000 maximum",
    )

    def test_every_wording_compacts_whatever_carries_it(self):
        for wording in self.WORDINGS:
            for exc in (RuntimeError(wording), StreamFailed(wording), _stream_error(wording)):
                verdict = classify(exc)
                self.assertEqual(verdict.kind, "context_overflow", (type(exc).__name__, wording))
                self.assertTrue(verdict.should_compact)

    def test_structured_code_wins_over_unfamiliar_wording(self):
        for code in ("context_length_exceeded", "context_window_exceeded",
                     "model_context_window_exceeded"):
            exc = _stream_error("请求被拒绝", body={"error": {"code": code}})
            self.assertEqual(classify(exc).kind, "context_overflow", code)

    def test_throttle_wording_about_tokens_is_still_not_overflow(self):
        verdict = classify(RuntimeError("Too many tokens, please wait before trying again."))
        self.assertEqual(verdict.kind, "rate_limit")


class VendorCodeTest(unittest.TestCase):
    """错误体是 {"code": "1302", "message": "中文文案"} 的厂商：措辞兜底全是英文，
    只能按码分。"""

    def test_codes_map_to_their_kind(self):
        for code, kind in (("1261", "context_overflow"), ("1113", "quota"), ("1304", "quota"),
                           ("1302", "rate_limit"), ("1303", "rate_limit"), ("1305", "rate_limit")):
            for body in ({"error": {"code": code}}, {"code": code}, {"code": int(code)}):
                exc = _stream_error("您的请求未能完成", body=body)
                self.assertEqual(classify(exc).kind, kind, (code, body))

    def test_code_embedded_in_bracketed_message(self):
        exc = _stream_error("[1302][您的账户已达到速率限制][req-1]")
        self.assertEqual(classify(exc).kind, "rate_limit")
        self.assertEqual(classify(StreamFailed("[1261][输入超长][req-2]")).kind, "context_overflow")

    def test_balance_exhausted_with_429_is_not_retried_as_throttling(self):
        exc = _DuckStatusError("余额不足", 429)
        exc.body = {"error": {"code": "1113"}}
        verdict = classify(exc)
        self.assertEqual(verdict.kind, "quota")
        self.assertFalse(verdict.retryable)

    def test_numbers_inside_ordinary_text_are_not_codes(self):
        self.assertEqual(classify(RuntimeError("line 1302: unexpected token")).kind, "fatal")


class BareStreamErrorTest(unittest.TestCase):
    def test_unrecognised_stream_error_is_transient_not_fatal(self):
        """chat 一路的流内错误没有状态码；判 fatal 就是不退避不换路由打死整轮。"""
        verdict = classify(_stream_error("服务繁忙，请稍后再试"))
        self.assertEqual(verdict.kind, "transient")
        self.assertTrue(verdict.retryable)

    def test_request_errors_with_a_status_stay_fatal(self):
        exc = openai.BadRequestError(
            "invalid tool schema",
            response=httpx2.Response(400, request=httpx2.Request("POST", "http://unused")),
            body=None,
        )
        self.assertEqual(classify(exc).kind, "fatal")

    def test_plain_exceptions_stay_fatal(self):
        self.assertEqual(classify(RuntimeError("服务繁忙")).kind, "fatal")


class CertificateFailureTest(unittest.TestCase):
    """证书校验失败被 SDK 包成连接错误：按瞬时处理就是重试几次再换路由，
    而换哪条路都一样败。"""

    def wrapped(self) -> Exception:
        import ssl

        try:
            try:
                raise ssl.SSLCertVerificationError(
                    1, "certificate verify failed: self-signed certificate in certificate chain"
                )
            except ssl.SSLCertVerificationError as inner:
                try:
                    raise httpx2.ConnectError("tls failed") from inner
                except httpx2.ConnectError as middle:
                    raise openai.APIConnectionError(
                        message="Connection error.",
                        request=httpx2.Request("POST", "http://unused"),
                    ) from middle
        except openai.APIConnectionError as exc:
            return exc

    def test_is_fatal_and_points_at_the_ca_bundle(self):
        verdict = classify(self.wrapped())
        self.assertEqual(verdict.kind, "fatal")
        self.assertFalse(verdict.retryable)
        self.assertIn("SSL_CERT_FILE", verdict.hint)

    def test_wording_only_is_enough(self):
        exc = RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed")
        self.assertEqual(classify(exc).kind, "fatal")
        self.assertIn("证书", classify(exc).hint)

    def test_ordinary_connection_errors_stay_transient(self):
        exc = openai.APIConnectionError(request=httpx2.Request("POST", "http://unused"))
        self.assertEqual(classify(exc).kind, "transient")


class StreamFailedTest(unittest.TestCase):
    """流跑起来了才收到服务端错误事件：HTTP 层是 200，没有状态码可判。

    判 fatal 会让一次容量抖动不退避不换路由地打死整轮——这类错误绝大多数是瞬时的。
    """

    def test_unrecognised_message_falls_back_to_transient(self):
        verdict = classify(StreamFailed("Responses 流失败：upstream said no"))
        self.assertEqual(verdict.kind, "transient")
        self.assertTrue(verdict.retryable)
        self.assertFalse(verdict.should_compact)

    def test_known_categories_still_win_over_the_fallback(self):
        """兜底只在没别的判据时生效：额度耗尽仍是 quota，重试无用。"""
        verdict = classify(StreamFailed("insufficient_quota: 余额不足"))
        self.assertEqual(verdict.kind, "quota")
        self.assertFalse(verdict.retryable)

    def test_overflow_in_stream_still_compacts(self):
        verdict = classify(StreamFailed("prompt is too long"))
        self.assertEqual(verdict.kind, "context_overflow")
        self.assertTrue(verdict.should_compact)


class TransientMarkerTest(unittest.TestCase):
    """流内错误事件在两个 SDK 里都躲开了「状态码 >= 500」那条判据：

    openai 抛没有状态码的裸 APIError，anthropic 抛 status_code=200 的 APIStatusError。
    最常见的一类瞬时故障（overloaded / 容量不足）只剩措辞可认。
    """

    def test_bare_api_error_without_status(self):
        exc = openai.APIError(
            "Overloaded", request=httpx2.Request("POST", "http://unused"), body=None
        )
        self.assertIsNone(getattr(exc, "status_code", None))
        self.assertEqual(classify(exc).kind, "transient")

    def test_two_hundred_status_error_from_stream(self):
        #  anthropic 的流内 error 事件带的是那条 200 响应
        exc = _DuckStatusError(
            '{"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}', 200
        )
        self.assertEqual(classify(exc).kind, "transient")

    def test_capacity_wordings(self):
        for text in (
            "xai: the model is currently at capacity, please try again later",
            "upstream connect error or disconnect/reset before headers",
            "provider returned error",
            "503 service unavailable",
        ):
            self.assertEqual(classify(RuntimeError(text)).kind, "transient", text)

    def test_grpc_resource_exhausted_is_rate_limit(self):
        #  Google 对「每分钟配额用尽」也报这个：退避等一等就过去，不是充值才能解的 quota
        verdict = classify(RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded for model"))
        self.assertEqual(verdict.kind, "rate_limit")
        self.assertTrue(verdict.retryable)

    def test_markers_do_not_swallow_real_request_errors(self):
        for text in (
            "invalid_request_error: messages.3: unexpected role",
            "model not found",
        ):
            self.assertEqual(classify(RuntimeError(text)).kind, "fatal", text)
