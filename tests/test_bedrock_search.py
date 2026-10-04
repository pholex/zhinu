"""Mantle 搜索鉴权：真实 SDK 请求走内存传输，不调用 AWS。"""

from __future__ import annotations

import importlib.util
import unittest
from unittest import mock

import httpx2

from xiaoyu import bedrock_search
from xiaoyu.config import MissingConfig


class TestMantleClient(unittest.TestCase):
    def setUp(self):
        self.requests = []

        def handle(request):
            self.requests.append(request)
            return httpx2.Response(200, json={"id": "resp_test", "object": "response",
                                             "status": "completed", "output": []})

        self.http_client = httpx2.Client(transport=httpx2.MockTransport(handle))
        self.addCleanup(self.http_client.close)
        patcher = mock.patch.object(bedrock_search.netproxy, "http_client", return_value=self.http_client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_bearer_request_endpoint_and_client_closed(self):
        with mock.patch.object(bedrock_search, "_MantleAuth", side_effect=AssertionError("不该取 IAM 凭证")):
            with bedrock_search.client("us-east-1", "test-bearer", 10) as client:
                client.responses.create(model="openai.gpt-5.6-luna", input="q")
        request, = self.requests
        self.assertEqual(str(request.url), "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses")
        self.assertEqual(request.headers["Authorization"], "Bearer test-bearer")
        self.assertTrue(self.http_client.is_closed)

    def test_redirect_is_not_followed(self):
        attempts = []

        def redirect(request):
            attempts.append(str(request.url))
            return httpx2.Response(302, headers={"Location": "https://other.example/responses"})

        http_client = httpx2.Client(transport=httpx2.MockTransport(redirect), follow_redirects=True)
        with mock.patch.object(bedrock_search.netproxy, "http_client", return_value=http_client):
            with bedrock_search.client("us-east-1", "test-bearer", 10) as client:
                with self.assertRaises(Exception):
                    client.responses.create(model="openai.gpt-5.6-luna", input="q")
        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0].startswith("https://bedrock-mantle.us-east-1.api.aws/"))

    @unittest.skipUnless(importlib.util.find_spec("botocore"), "未安装可选 botocore")
    def test_sigv4_body_session_token_and_refresh(self):
        from botocore.credentials import Credentials

        credentials = mock.Mock()
        credentials.get_frozen_credentials.side_effect = [
            Credentials("test-access-1", "test-secret", "test-session-1").get_frozen_credentials(),
            Credentials("test-access-2", "test-secret", "test-session-2").get_frozen_credentials(),
        ]
        with mock.patch("botocore.session.get_session") as session:
            session.return_value.get_credentials.return_value = credentials
            with bedrock_search.client("us-west-2", None, 10) as client:
                for _ in range(2):
                    client.responses.create(model="openai.gpt-5.6-luna", input="中文查询")
        for index, request in enumerate(self.requests, start=1):
            auth = request.headers["Authorization"]
            self.assertTrue(auth.startswith("AWS4-HMAC-SHA256 "))
            self.assertIn(f"Credential=test-access-{index}/", auth)
            self.assertIn("/us-west-2/bedrock-mantle/aws4_request", auth)
            self.assertEqual(request.headers["X-Amz-Security-Token"], f"test-session-{index}")
            self.assertIn("x-amz-security-token", auth)
            self.assertTrue(request.content)
        self.assertEqual(credentials.get_frozen_credentials.call_count, 2)

    def test_missing_optional_dependency_is_actionable(self):
        with mock.patch.object(bedrock_search.importlib.util, "find_spec", return_value=None):
            with self.assertRaisesRegex(MissingConfig, r"xiaoyu-agent\[bedrock\]"):
                bedrock_search.client("us-east-1", None, 10)

    @unittest.skipUnless(importlib.util.find_spec("botocore"), "未安装可选 botocore")
    def test_missing_credentials_is_actionable(self):
        with mock.patch("botocore.session.get_session") as session:
            session.return_value.get_credentials.return_value = None
            with self.assertRaisesRegex(MissingConfig, "未找到 AWS 凭证"):
                bedrock_search.client("us-east-1", None, 10)

    def test_failed_sdk_construction_closes_http_client(self):
        with mock.patch("openai.OpenAI", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                bedrock_search.client("us-east-1", "test-bearer", 10)
        self.assertTrue(self.http_client.is_closed)


if __name__ == "__main__":
    unittest.main()
