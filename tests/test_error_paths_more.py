"""错误分类与模型层的补充路径：流中途断开、流内异常、超窗措辞、限流与额度之分、
辅助模型回退、出网前的工具名与调用 id 规整。不打外网——需要真实 SDK 行为时起本机假服务器。"""

from __future__ import annotations

import contextlib
import io
import socket
import ssl
import threading
import unittest
from unittest import mock

import httpx2
import openai

from xiaoyu import netproxy
from xiaoyu.errors import classify

from .test_agent_paths import AgentTestCase, chunk, usage_chunk


# ---------- 本机假服务器：回一个开了头就断的流 ----------


def _read_request(conn: socket.socket) -> None:
    """把请求读完整再回话：留着没读的请求体就关连接，对端看到的是 RST 而不是 EOF。"""
    data = b""
    while b"\r\n\r\n" not in data:
        piece = conn.recv(65536)
        if not piece:
            return
        data += piece
    head, _, body = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        if name.strip().lower() == b"content-length":
            length = int(value.strip())
    while len(body) < length:
        piece = conn.recv(65536)
        if not piece:
            return
        body += piece


def serve_once(
    test: unittest.TestCase, content_type: bytes, pieces: list[bytes], *, cut: bool
) -> str:
    """起一个只接一次请求的本机服务器：回 200 + 分块响应体。cut=True 时发完给定
    内容就断开（没有结束分块），否则正常收尾。返回 base URL。"""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(10)

    def run() -> None:
        try:
            conn, _ = server.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            try:
                _read_request(conn)
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\ncontent-type: " + content_type + b"\r\n"
                    b"transfer-encoding: chunked\r\n\r\n"
                )
                for piece in pieces:
                    conn.sendall(hex(len(piece))[2:].encode() + b"\r\n" + piece + b"\r\n")
                if not cut:
                    conn.sendall(b"0\r\n\r\n")
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    test.addCleanup(thread.join, 10)
    test.addCleanup(server.close)
    return f"http://127.0.0.1:{server.getsockname()[1]}"


def serve_cut_stream(test: unittest.TestCase, events: list[bytes]) -> str:
    """SSE 流开了头就断。"""
    return serve_once(test, b"text/event-stream", events, cut=True)


# ---------- 流中途的网络断开 ----------


class BareTransportErrorTest(unittest.TestCase):
    """两套 SDK 流迭代到一半断开时，网络异常都必须能恢复。"""

    def test_bare_transport_errors_are_transient(self):
        for exc in (
            httpx2.ReadError("peer closed"),
            httpx2.RemoteProtocolError("incomplete chunked read"),
            httpx2.ReadTimeout("timed out"),
        ):
            verdict = classify(exc)
            self.assertEqual(verdict.kind, "transient", exc)
            self.assertTrue(verdict.retryable, exc)
            self.assertFalse(verdict.should_compact, exc)

    def test_certificate_failure_is_still_fatal(self):
        """证书失败同样是传输层异常（ConnectError），但重试、换路由都没用。"""
        worded = httpx2.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "self-signed certificate in certificate chain (_ssl.c:1000)"
        )
        chained = httpx2.ConnectError("tls handshake failed")
        chained.__cause__ = ssl.SSLCertVerificationError(1, "unable to get local issuer")
        for exc in (worded, chained):
            verdict = classify(exc)
            self.assertEqual(verdict.kind, "fatal", exc)
            self.assertFalse(verdict.retryable, exc)
            self.assertIn("SSL_CERT_FILE", verdict.hint)

    def test_openai_sdk_mid_stream_disconnect(self):
        base = serve_cut_stream(
            self,
            [
                b'data: {"id":"c","object":"chat.completion.chunk","created":1,"model":"m",'
                b'"choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n'
            ],
        )
        client = openai.OpenAI(
            base_url=f"{base}/v1", api_key="k", max_retries=0, timeout=10,
            http_client=netproxy.http_client(),
        )
        self.addCleanup(client.close)
        seen: list[str] = []
        with self.assertRaises(Exception) as caught:
            for piece in client.chat.completions.create(
                model="m", messages=[{"role": "user", "content": "x"}], stream=True
            ):
                seen.append(piece.choices[0].delta.content or "")
        #  先确认真的是"流开了头才断"，再看分类
        self.assertEqual(seen, ["hi"])
        self.assertIsInstance(caught.exception, (httpx2.TransportError, openai.APIConnectionError))
        self.assertEqual(classify(caught.exception).kind, "transient")
        self.assertTrue(classify(caught.exception).retryable)

    def test_anthropic_sdk_mid_stream_disconnect(self):
        import anthropic

        base = serve_cut_stream(
            self,
            [
                b'event: message_start\ndata: {"type":"message_start","message":{"id":"m",'
                b'"type":"message","role":"assistant","model":"x","content":[],'
                b'"stop_reason":null,"stop_sequence":null,'
                b'"usage":{"input_tokens":1,"output_tokens":0}}}\n\n'
            ],
        )
        client = anthropic.Anthropic(
            base_url=base, api_key="k", max_retries=0, timeout=10,
            http_client=httpx2.Client(trust_env=False),
        )
        self.addCleanup(client.close)
        seen: list[str] = []
        with self.assertRaises(Exception) as caught:
            for event in client.messages.create(
                model="m", max_tokens=8, messages=[{"role": "user", "content": "x"}], stream=True
            ):
                seen.append(event.type)
        self.assertEqual(seen, ["message_start"])
        self.assertIsInstance(caught.exception, httpx2.TransportError)
        self.assertEqual(classify(caught.exception).kind, "transient")


class MidStreamDisconnectRecoveryTest(AgentTestCase):
    """主循环对流中途断开的处置：退避后原地重发，而不是整轮直接抛。"""

    def test_retries_in_place(self):
        def cut():
            yield chunk(content="半截")
            raise httpx2.ReadError("peer closed")

        agent = self.build([cut(), [chunk(content="完整回答"), usage_chunk(10, 3)]])
        with mock.patch("xiaoyu.agent.Agent._sleep") as fake_sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            agent.send("问")
        fake_sleep.assert_called_once()
        self.assertEqual(agent.last_assistant_text(), "完整回答")
        self.assertEqual(len(self.client.completions.calls), 2)


# ---------- Bedrock 的流内异常帧、4xx 里的瞬时状态码 ----------


def _event_stream_frame(headers: dict[str, str], payload: bytes) -> bytes:
    """按 AWS event stream 的二进制帧格式编一帧（头值一律字符串类型）。"""
    import binascii
    import struct

    head = b""
    for name, value in headers.items():
        raw_name, raw_value = name.encode(), value.encode()
        head += bytes([len(raw_name)]) + raw_name + b"\x07"
        head += struct.pack("!H", len(raw_value)) + raw_value
    prelude = struct.pack("!II", 16 + len(head) + len(payload), len(head))
    prelude += struct.pack("!I", binascii.crc32(prelude) & 0xFFFFFFFF)
    frame = prelude + head + payload
    return frame + struct.pack("!I", binascii.crc32(frame) & 0xFFFFFFFF)


def _bedrock_exception_frame(kind: str, message: str) -> bytes:
    import json

    return _event_stream_frame(
        {":message-type": "exception", ":exception-type": kind, ":content-type": "application/json"},
        json.dumps({"message": message}).encode(),
    )


def _bedrock_chunk_frame(event: dict) -> bytes:
    import base64
    import json

    inner = base64.b64encode(json.dumps(event).encode()).decode()
    return _event_stream_frame(
        {":message-type": "event", ":event-type": "chunk", ":content-type": "application/json"},
        json.dumps({"bytes": inner}).encode(),
    )


def _frame_error(kind: str, message: str = "boom") -> ValueError:
    """SDK 的 Bedrock 解码器对异常帧抛的那种 ValueError（文本照它的格式拼）。"""
    frame = {
        "status_code": 400,
        "headers": {
            ":message-type": "exception",
            ":exception-type": kind,
            ":content-type": "application/json",
        },
        "body": ('{"message": "%s"}' % message).encode(),
    }
    return ValueError(f"Bad response code, expected 200: {frame}")


def _raising(exc: Exception):
    """先给一个正常事件、再在取下一个事件时抛错的事件流。"""
    import types

    yield types.SimpleNamespace(
        type="content_block_delta",
        index=0,
        delta=types.SimpleNamespace(type="text_delta", text="半截"),
    )
    raise exc


class BedrockStreamExceptionTest(unittest.TestCase):
    """Bedrock 异常帧：兼容旧解码器和新版类型化流错误。"""

    def status_error(self, kind, message="stream broke", status=200):
        import anthropic

        body = {"type": "error", "error": {"type": kind, "message": message}}
        return anthropic.APIStatusError(
            str(body), body=body,
            response=httpx2.Response(status, request=httpx2.Request("POST", "http://unused")),
        )

    def test_typed_stream_errors_preserve_retry_and_validation_boundaries(self):
        from xiaoyu.errors import StreamFailed

        for kind in ("modelStreamErrorException", "internalServerException",
                     "modelTimeoutException", "serviceUnavailableException"):
            with self.subTest(kind=kind):
                texts, raised = self.consume(self.status_error(kind))
                self.assertEqual(texts, ["半截"])
                self.assertIsInstance(raised, StreamFailed)
                self.assertTrue(classify(raised).retryable)
        _, throttled = self.consume(self.status_error("throttlingException", "Too many requests"))
        self.assertEqual(classify(throttled).kind, "rate_limit")
        for exc in (self.status_error("validationException", "Malformed input request"),
                    self.status_error("unknownException"),
                    self.status_error("modelStreamErrorException", status=400)):
            with self.subTest(error=str(exc)):
                _, raised = self.consume(exc)
                self.assertIs(raised, exc)
                self.assertFalse(classify(raised).retryable)
        _, too_long = self.consume(self.status_error("validationException", "Input is too long"))
        self.assertEqual(classify(too_long).kind, "context_overflow")

    def consume(self, exc: Exception) -> tuple[list[str], Exception]:
        from xiaoyu import messages

        texts: list[str] = []
        with self.assertRaises(Exception) as caught:
            for piece in messages.stream_chunks(_raising(exc)):
                texts.extend(choice.delta.content or "" for choice in piece.choices)
        return texts, caught.exception

    def test_server_side_exceptions_become_retryable(self):
        from xiaoyu.errors import StreamFailed

        for kind in (
            "modelStreamErrorException",
            "internalServerException",
            "modelTimeoutException",
            "serviceUnavailableException",
        ):
            texts, raised = self.consume(_frame_error(kind))
            #  异常帧之前已经到的内容照常翻译出来
            self.assertEqual(texts, ["半截"], kind)
            self.assertIsInstance(raised, StreamFailed, kind)
            #  上游原文照带：排查时只有这一句话可看
            self.assertIn(kind, str(raised))
            verdict = classify(raised)
            self.assertEqual(verdict.kind, "transient", kind)
            self.assertTrue(verdict.retryable, kind)

    def test_wording_still_wins_over_the_fallback(self):
        _, throttled = self.consume(_frame_error("throttlingException", "Too many requests"))
        self.assertEqual(classify(throttled).kind, "rate_limit")
        _, too_long = self.consume(
            _frame_error("modelStreamErrorException", "Input is too long for requested model.")
        )
        verdict = classify(too_long)
        self.assertEqual(verdict.kind, "context_overflow")
        self.assertTrue(verdict.should_compact)

    def test_validation_exception_is_not_retried(self):
        """请求本身不合法：原样重发结果相同，不该被兜底成瞬时错误。"""
        _, raised = self.consume(_frame_error("validationException", "Malformed input request"))
        self.assertIsInstance(raised, ValueError)
        self.assertEqual(classify(raised).kind, "fatal")
        #  超窗也是以 validationException 报的，措辞判定照常生效
        _, too_long = self.consume(
            _frame_error("validationException", "Input is too long for requested model.")
        )
        self.assertEqual(classify(too_long).kind, "context_overflow")

    def test_unrelated_value_errors_pass_through(self):
        _, raised = self.consume(ValueError("something odd"))
        self.assertIs(type(raised), ValueError)
        self.assertEqual(classify(raised).kind, "fatal")

    def test_real_decoder_through_the_sdk(self):
        """整条真实链路：本机服务器回一帧正常事件 + 一帧异常，经 SDK 的解码器进翻译层。"""
        import importlib.util

        if importlib.util.find_spec("botocore") is None:
            self.skipTest("事件流解码依赖 botocore（bedrock extra）")
        import anthropic

        from xiaoyu import messages
        from xiaoyu.errors import StreamFailed

        start = {
            "type": "message_start",
            "message": {
                "id": "m", "type": "message", "role": "assistant", "model": "x", "content": [],
                "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 0},
            },
        }
        block = {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        }
        delta = {
            "type": "content_block_delta", "index": 0,
            "delta": {"type": "text_delta", "text": "半截"},
        }
        base = serve_once(
            self,
            b"application/vnd.amazon.eventstream",
            [
                _bedrock_chunk_frame(start),
                _bedrock_chunk_frame(block),
                _bedrock_chunk_frame(delta),
                _bedrock_exception_frame("modelStreamErrorException", "model stream broke"),
            ],
            cut=False,
        )
        client = anthropic.AnthropicBedrock(
            api_key="k", aws_region="us-east-1", base_url=base, max_retries=0, timeout=10,
            http_client=httpx2.Client(trust_env=False),
        )
        self.addCleanup(client.close)
        stream = client.messages.create(
            model="some.model-v1", max_tokens=8,
            messages=[{"role": "user", "content": "x"}], stream=True,
        )
        texts: list[str] = []
        with self.assertRaises(StreamFailed) as caught:
            for piece in messages.stream_chunks(stream):
                texts.extend(choice.delta.content or "" for choice in piece.choices)
        self.assertEqual(texts, ["半截"])
        self.assertIn("modelStreamErrorException", str(caught.exception))
        self.assertEqual(classify(caught.exception).kind, "transient")


class _StatusError(Exception):
    """SDK 状态码异常的最小鸭子型：classify 只取 status_code。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class TransientStatusTest(unittest.TestCase):
    def test_request_timeout_conflict_and_model_error_are_transient(self):
        for status in (408, 409, 424):
            verdict = classify(_StatusError("error", status))
            self.assertEqual(verdict.kind, "transient", status)
            self.assertTrue(verdict.retryable, status)

    def test_real_sdk_status_errors(self):
        response = httpx2.Response(409, request=httpx2.Request("POST", "http://unused"))
        self.assertEqual(
            classify(openai.ConflictError("lock timeout", response=response, body=None)).kind,
            "transient",
        )

    def test_other_client_errors_stay_fatal(self):
        #  413 不在这里：请求体过大按超限处理（见 test_errors 的 PayloadTooLargeTest）
        for status in (400, 404, 422):
            self.assertEqual(classify(_StatusError("error", status)).kind, "fatal", status)

    def test_wording_still_wins_over_the_status(self):
        verdict = classify(_StatusError("prompt is too long: 9 tokens > 8 maximum", 424))
        self.assertEqual(verdict.kind, "context_overflow")


# ---------- 超出上下文窗口的另几种措辞 ----------


class MoreOverflowWordingTest(unittest.TestCase):
    """漏判成 fatal 是整轮直接死、不压缩。"""

    WORDINGS = (
        #  Bedrock 上的 Claude
        "Input is too large: too many total text bytes: 9437184 > 9000000",
        "too many total text bytes",
        #  Bedrock Mantle，经网关转出
        "prompt tokens exceed model maximum of 200000",
        "requested tokens exceed customer model maximum",
        #  vLLM
        "The prompt (9000 tokens) exceeds the max_model_len of 8192",
        "The decoder prompt (length 9000) is longer than the maximum model length of 8192. "
        "Make sure that `max_model_len` is no smaller than the number of text tokens.",
    )

    def test_every_wording_compacts_whatever_carries_it(self):
        from xiaoyu.errors import StreamFailed

        for wording in self.WORDINGS:
            for exc in (
                RuntimeError(wording),
                StreamFailed(wording),
                _StatusError(wording, 400),
                openai.BadRequestError(
                    wording,
                    response=httpx2.Response(400, request=httpx2.Request("POST", "http://unused")),
                    body=None,
                ),
            ):
                with self.subTest(wording=wording, carrier=type(exc).__name__):
                    verdict = classify(exc)
                    self.assertEqual(verdict.kind, "context_overflow")
                    self.assertTrue(verdict.should_compact)
                    self.assertTrue(verdict.retryable)

    def test_token_worded_throttle_is_still_rate_limit(self):
        verdict = classify(RuntimeError("Too many tokens, please wait before trying again."))
        self.assertEqual(verdict.kind, "rate_limit")
        self.assertFalse(verdict.should_compact)

    def test_lookalike_request_errors_do_not_compact(self):
        """长得像、但说的不是输入超窗：误判会白做一次强制压缩。"""
        for wording in (
            "max_tokens: 100000 > 64000, which is the maximum allowed number of output tokens",
            "model maximum output tokens is 8192",
            "Unknown parameter: max_model_len",
            "too many images in request",
        ):
            verdict = classify(_StatusError(wording, 400))
            self.assertEqual(verdict.kind, "fatal", wording)
            self.assertFalse(verdict.should_compact, wording)


# ---------- 措辞像额度用尽、实为限流 ----------

#  Gemini 每分钟限流的 429。**文案出自对公开样本的记忆，没有对着真实 API 核对过**：
#  它与 OpenAI 余额用尽共用第一句话，区别在 RESOURCE_EXHAUSTED 状态与点名的等待时长
_GEMINI_SENTENCE = (
    "You exceeded your current quota, please check your plan and billing details. "
    "For more information on this error, head to: https://ai.google.dev/gemini-api/docs/rate-limits. "
    "* Quota exceeded for metric: generativelanguage.googleapis.com/generate_content_requests, "
    "limit: 10\nPlease retry in 35.2s."
)
_GEMINI_BODY = [
    {
        "error": {
            "code": 429,
            "message": _GEMINI_SENTENCE,
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "35s"}
            ],
        }
    }
]


def _rate_limited(message: str, body=None, headers=None) -> openai.RateLimitError:
    response = httpx2.Response(
        429, request=httpx2.Request("POST", "http://unused"), headers=headers
    )
    return openai.RateLimitError(message, response=response, body=body)


def _gemini_samples() -> list[Exception]:
    return [
        #  经 OpenAI 兼容端点：SDK 把整个错误体拼进异常文本
        _rate_limited(f"Error code: 429 - {_GEMINI_BODY}", body=_GEMINI_BODY),
        #  网关转写后只剩文本
        RuntimeError(f"429 RESOURCE_EXHAUSTED. {_GEMINI_BODY[0]}"),
        #  没有状态码、没有 gRPC 码，只剩那句话和等待时长
        RuntimeError(_GEMINI_SENTENCE),
    ]


class QuotaWordedThrottleTest(unittest.TestCase):
    def test_gemini_per_minute_limit_is_rate_limit(self):
        for exc in _gemini_samples():
            with self.subTest(carrier=type(exc).__name__, text=str(exc)[:40]):
                verdict = classify(exc)
                self.assertEqual(verdict.kind, "rate_limit")
                self.assertTrue(verdict.retryable)
                self.assertFalse(verdict.should_compact)

    def test_exhausted_balance_is_still_quota(self):
        """真·余额用尽：同一句话，但服务端没说等一等。"""
        sentence = "You exceeded your current quota, please check your plan and billing details."
        body = {"message": sentence, "type": "insufficient_quota", "code": "insufficient_quota"}
        for exc in (
            _rate_limited(sentence),
            _rate_limited(f"Error code: 429 - {{'error': {body}}}", body=body),
            RuntimeError(sentence),
            RuntimeError("insufficient_quota"),
        ):
            with self.subTest(carrier=type(exc).__name__, text=str(exc)[:40]):
                verdict = classify(exc)
                self.assertEqual(verdict.kind, "quota")
                self.assertFalse(verdict.retryable)

    def test_unambiguous_quota_signals_do_not_yield(self):
        """结构化错误码与点名余额/预算的措辞：即使带着 gRPC 限流码或等待时长也是额度。"""
        coded = _rate_limited(
            "RESOURCE_EXHAUSTED: please retry in 5s",
            body={"code": "insufficient_quota", "message": "x"},
        )
        for exc in (
            coded,
            RuntimeError("RESOURCE_EXHAUSTED: insufficient_quota, retry in 5s"),
            RuntimeError("Budget has been exceeded! Max budget: 10.0. retryDelay: 30s"),
        ):
            with self.subTest(text=str(exc)[:40]):
                self.assertEqual(classify(exc).kind, "quota")


class BodyRetryDelayTest(unittest.TestCase):
    def test_delay_is_read_from_the_message(self):
        from xiaoyu.errors import retry_after_asked

        first, second, third = _gemini_samples()
        self.assertEqual(retry_after_asked(first), 35.2)
        self.assertEqual(retry_after_asked(second), 35.2)
        self.assertEqual(retry_after_asked(third), 35.2)
        #  只有 RetryInfo 没有那句话时认 retryDelay
        for text in (
            "RESOURCE_EXHAUSTED {'retryDelay': '12s'}",
            'RESOURCE_EXHAUSTED {"retryDelay": "12s"}',
            'RESOURCE_EXHAUSTED {\\"retryDelay\\": \\"12s\\"}',
        ):
            self.assertEqual(retry_after_asked(RuntimeError(text)), 12.0, text)

    def test_nothing_parseable_means_nothing_passed_on(self):
        from xiaoyu.errors import retry_after_asked

        for text in (
            "rate limited",
            "please retry in a moment",
            "Please retry in 1m30s.",
            "retry in 0s",
            "retryDelay: soon",
        ):
            self.assertIsNone(retry_after_asked(RuntimeError(text)), text)

    def test_header_wins_over_the_message(self):
        from xiaoyu.errors import retry_after_asked

        exc = _rate_limited("Please retry in 35.2s.", headers={"retry-after": "7"})
        self.assertEqual(retry_after_asked(exc), 7.0)


class QuotaWordedThrottleRecoveryTest(AgentTestCase):
    """主循环：按服务端在正文里点名的时长等，然后原地重发。"""

    def test_waits_as_long_as_the_message_says(self):
        agent = self.build(
            [_gemini_samples()[0], [chunk(content="好了"), usage_chunk(10, 2)]]
        )
        with mock.patch("xiaoyu.agent.Agent._sleep") as fake_sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            agent.send("问")
        (waited,) = [call.args[0] for call in fake_sleep.call_args_list]
        #  只往上抖：早于服务端说的时刻醒来必然再挨一次
        self.assertGreaterEqual(waited, 35.2)
        self.assertLessEqual(waited, 35.2 * 1.1 + 1e-9)
        self.assertEqual(agent.last_assistant_text(), "好了")


# ---------- 辅助模型没有 provider 能接时退回主模型 ----------


class AuxiliaryModelFallbackTest(AgentTestCase):
    """只配了一家、主模型换成这家的型号，摘要与检索模型的默认值还落在别家。"""

    def build_single_provider(self, script: list):
        from xiaoyu.agent import Agent
        from xiaoyu.providers import Provider, Registry
        from xiaoyu.tools import Toolbox

        from .test_agent_paths import FakeClient

        self.client = FakeClient(script)
        #  非通配：只认 main-model，cheap-model（摘要 / 检索模型）没人接
        registry = Registry(
            [Provider("solo", "", "", ("main-model",), "solo")], clients={"solo": self.client}
        )
        return Agent(self.config, Toolbox(self.config), registry=registry)

    def test_summary_chain_falls_to_the_main_model(self):
        agent = self.build_single_provider([])
        self.assertEqual(
            [route.qualified for route in agent.summary_models()], ["solo/main-model"]
        )

    def test_summarize_really_produces_a_summary(self):
        from .test_agent_paths import GOOD_SUMMARY, text_response

        agent = self.build_single_provider([text_response(GOOD_SUMMARY)])
        with contextlib.redirect_stdout(io.StringIO()):
            summary = agent._summarize("一段历史")
        self.assertEqual(summary, GOOD_SUMMARY)
        self.assertEqual(self.client.completions.calls[0]["model"], "main-model")

    def test_unresolvable_main_model_still_raises(self):
        from xiaoyu.providers import UnknownModel

        agent = self.build_single_provider([])
        agent.config.model = "ghost-model"
        with self.assertRaises(UnknownModel):
            agent.summary_models()
        #  摘要模型与主模型是同一个名字时也一样
        agent.config.summary_model = "ghost-model"
        with self.assertRaises(UnknownModel):
            agent.summary_models()

    def test_request_chain_still_raises_on_its_leading_name(self):
        """主请求链的语义不变：打头的名字解析不了必须立刻看见。"""
        from xiaoyu.providers import UnknownModel

        agent = self.build_single_provider([])
        with self.assertRaises(UnknownModel):
            agent._routes(["cheap-model", "main-model"])
        #  排在后面的备用名字解析不了则跳过
        self.assertEqual(
            [route.qualified for route in agent._routes(["main-model", "cheap-model"])],
            ["solo/main-model"],
        )

    def test_explore_runs_on_the_main_model_and_says_so_once(self):
        agent = self.build_single_provider(
            [[chunk(content="子：第一次的结论")], [chunk(content="子：第二次的结论")]]
        )
        tool = agent.toolbox.get("explore")
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            first = tool.handler(question="add 定义在哪")
            second = tool.handler(question="谁调用了 add")
        self.assertIn("子：第一次的结论", first)
        self.assertIn("子：第二次的结论", second)
        self.assertNotIn("ERROR", first)
        self.assertIn("由 main-model 只读检索", first)
        self.assertEqual(
            [call["model"] for call in self.client.completions.calls],
            ["main-model", "main-model"],
        )
        self.assertEqual(shown.getvalue().count("改用主模型 main-model"), 1)

    def test_resolvable_explore_model_is_used_without_any_notice(self):
        agent = self.build([[chunk(content="子：结论")]])
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            answer = agent.toolbox.get("explore").handler(question="add 定义在哪")
        self.assertIn("由 cheap-model 只读检索", answer)
        self.assertEqual(self.client.completions.calls[0]["model"], "cheap-model")
        self.assertNotIn("改用主模型", shown.getvalue())


# ---------- 本地补的调用 id、出网前的工具名与调用 id 规整 ----------


class LocalCallIdTest(AgentTestCase):
    """流里一路都没给 id 的工具调用由本地补 id：跨轮不能重复。"""

    def test_ids_do_not_repeat_across_turns(self):
        import json

        from .test_agent_paths import call_fragment

        def idless_call():
            arguments = json.dumps({"path": "calc.py"})
            return [chunk(tool_calls=[call_fragment(0, None, "read_file", arguments)]),
                    usage_chunk(10, 5)]

        agent = self.build(
            [idless_call(), idless_call(), [chunk(content="读完了"), usage_chunk(10, 2)]]
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("读两遍")
        issued = [
            call["id"]
            for message in agent.messages
            if message["role"] == "assistant"
            for call in message.get("tool_calls") or []
        ]
        answered = [m["tool_call_id"] for m in agent.messages if m["role"] == "tool"]
        self.assertEqual(len(issued), 2)
        self.assertEqual(len(set(issued)), 2, issued)
        self.assertTrue(all(call_id.startswith("call_local_") for call_id in issued), issued)
        #  每个结果仍挂在自己那次调用上
        self.assertEqual(answered, issued)


_VALID_NAME = r"[a-zA-Z0-9_-]{1,64}"


def _history_with_odd_names() -> list[dict]:
    names = ["multi_tool_use.parallel", "bash run", "x" * 65, "read_file"]
    calls = [
        {"id": f"c{index}", "type": "function", "function": {"name": name, "arguments": "{}"}}
        for index, name in enumerate(names)
    ]
    results = [
        {"role": "tool", "tool_call_id": call["id"], "content": "ERROR: 未知工具"}
        for call in calls
    ]
    return [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": None, "tool_calls": calls},
        *results,
        {"role": "user", "content": "继续"},
    ]


class ToolNameOnTheWireTest(unittest.TestCase):
    """模型写出的不合规工具名留在历史里，三条协议出网时都换成占位，历史不动。"""

    def check(self, sent: list[str]) -> None:
        from xiaoyu import responses

        for name in sent:
            self.assertRegex(name, f"^{_VALID_NAME}$")
        self.assertEqual(sent[:3], [responses.MALFORMED_TOOL] * 3)
        #  合规的名字一个字节不动
        self.assertEqual(sent[3], "read_file")

    def test_chat(self):
        from xiaoyu.responses import Transport

        from .test_responses import FakeClient, FakeResponses

        history = _history_with_odd_names()
        inner = FakeClient(FakeResponses())
        Transport(inner).chat.completions.create(model="m", messages=history)
        sent = inner.chat.completions.calls[0]["messages"]
        self.check([call["function"]["name"] for call in sent[1]["tool_calls"]])
        self.assertEqual(history, _history_with_odd_names())

    def test_responses(self):
        from .test_responses import FakeResponses, responses_transport

        history = _history_with_odd_names()
        api = FakeResponses()
        responses_transport(api).chat.completions.create(model="m", messages=history)
        items = api.calls[0]["input"]
        self.check([item["name"] for item in items if item.get("type") == "function_call"])
        self.assertEqual(history, _history_with_odd_names())

    def test_messages(self):
        from .test_messages import FakeMessagesAPI, anthropic_transport

        history = _history_with_odd_names()
        api = FakeMessagesAPI()
        transport, _ = anthropic_transport(api)
        transport.chat.completions.create(model="m", messages=history)
        blocks = api.calls[0]["messages"][1]["content"]
        self.check([block["name"] for block in blocks if block["type"] == "tool_use"])
        self.assertEqual(history, _history_with_odd_names())

    def test_a_name_in_this_requests_tool_table_is_left_alone(self):
        """宿主注册的工具名即使带点号，调用与工具表也得对得上。"""
        from xiaoyu import responses

        history = _history_with_odd_names()
        tools = [
            {"type": "function",
             "function": {"name": "multi_tool_use.parallel", "parameters": {"type": "object"}}}
        ]
        repaired = responses.repair_tool_arguments(history, tools)
        names = [call["function"]["name"] for call in repaired[1]["tool_calls"]]
        self.assertEqual(
            names,
            ["multi_tool_use.parallel", responses.MALFORMED_TOOL, responses.MALFORMED_TOOL,
             "read_file"],
        )


class MessagesCallIdTest(unittest.TestCase):
    """Messages 一路：别家产的调用 id 带 `.`、`:` 时做确定性规整，调用与结果两头同值。"""

    IDS = ("functions.read_file:0", "a.b", "a:b", "toolu_01AbC-xyz")

    def history(self) -> list[dict]:
        calls = [
            {"id": call_id, "type": "function",
             "function": {"name": "read_file", "arguments": "{}"}}
            for call_id in self.IDS
        ]
        return [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": calls},
            *[{"role": "tool", "tool_call_id": call_id, "content": "ok"} for call_id in self.IDS],
        ]

    def convert(self) -> tuple[list[str], list[str]]:
        from xiaoyu import messages

        history = self.history()
        request = messages.to_request("m", history, None, False, {})
        self.assertEqual(history, self.history())
        _, assistant, results = request["messages"]
        used = [block["id"] for block in assistant["content"] if block["type"] == "tool_use"]
        answered = [
            block["tool_use_id"] for block in results["content"] if block["type"] == "tool_result"
        ]
        return used, answered

    def test_both_ends_get_the_same_compliant_id(self):
        used, answered = self.convert()
        self.assertEqual(used, answered)
        for call_id in used:
            self.assertRegex(call_id, r"^[a-zA-Z0-9_-]+$")
        #  只差在被替换字符上的两个 id 不会撞成一个
        self.assertEqual(len(set(used)), len(self.IDS))
        #  合规的 id 一个字节不动
        self.assertEqual(used[3], "toolu_01AbC-xyz")

    def test_replacement_is_stable_across_requests(self):
        self.assertEqual(self.convert(), self.convert())
