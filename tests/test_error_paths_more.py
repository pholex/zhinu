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

import httpx
import openai

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


def serve_cut_stream(test: unittest.TestCase, events: list[bytes]) -> str:
    """起一个只接一次请求的本机服务器：回 200 + 分块 SSE，发完给定事件就断开
    （没有结束分块）。返回 base URL。"""
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
                    b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\n"
                    b"transfer-encoding: chunked\r\n\r\n"
                )
                for event in events:
                    conn.sendall(hex(len(event))[2:].encode() + b"\r\n" + event + b"\r\n")
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    test.addCleanup(thread.join, 10)
    test.addCleanup(server.close)
    return f"http://127.0.0.1:{server.getsockname()[1]}"


# ---------- 流中途的网络断开 ----------


class BareTransportErrorTest(unittest.TestCase):
    """流迭代到一半断开时，SDK 抛的是没有包装的 httpx 传输层异常。"""

    def test_bare_transport_errors_are_transient(self):
        for exc in (
            httpx.ReadError("peer closed"),
            httpx.RemoteProtocolError("incomplete chunked read"),
            httpx.ReadTimeout("timed out"),
        ):
            verdict = classify(exc)
            self.assertEqual(verdict.kind, "transient", exc)
            self.assertTrue(verdict.retryable, exc)
            self.assertFalse(verdict.should_compact, exc)

    def test_certificate_failure_is_still_fatal(self):
        """证书失败同样是传输层异常（ConnectError），但重试、换路由都没用。"""
        worded = httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: "
            "self-signed certificate in certificate chain (_ssl.c:1000)"
        )
        chained = httpx.ConnectError("tls handshake failed")
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
            http_client=httpx.Client(trust_env=False),
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
        self.assertIsInstance(caught.exception, httpx.TransportError)
        self.assertEqual(classify(caught.exception).kind, "transient")

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
            http_client=httpx.Client(trust_env=False),
        )
        self.addCleanup(client.close)
        seen: list[str] = []
        with self.assertRaises(Exception) as caught:
            for event in client.messages.create(
                model="m", max_tokens=8, messages=[{"role": "user", "content": "x"}], stream=True
            ):
                seen.append(event.type)
        self.assertEqual(seen, ["message_start"])
        self.assertIsInstance(caught.exception, httpx.TransportError)
        self.assertEqual(classify(caught.exception).kind, "transient")


class MidStreamDisconnectRecoveryTest(AgentTestCase):
    """主循环对流中途断开的处置：退避后原地重发，而不是整轮直接抛。"""

    def test_retries_in_place(self):
        def cut():
            yield chunk(content="半截")
            raise httpx.ReadError("peer closed")

        agent = self.build([cut(), [chunk(content="完整回答"), usage_chunk(10, 3)]])
        with mock.patch("xiaoyu.agent.Agent._sleep") as fake_sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            agent.send("问")
        fake_sleep.assert_called_once()
        self.assertEqual(agent.last_assistant_text(), "完整回答")
        self.assertEqual(len(self.client.completions.calls), 2)
