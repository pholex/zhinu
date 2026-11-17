"""Bedrock Mantle 搜索 client：Bearer 或 AWS 凭证链的 SigV4 鉴权。

与 bedrock-runtime 上的模型调用分开；不新增运行期依赖，IAM 签名复用
可选 [bedrock] 里的 botocore。client 每次工具调用后由调用方关闭。
"""

from __future__ import annotations

import importlib.util

import httpx2

from . import netproxy
from .config import MissingConfig


class _MantleAuth(httpx2.Auth):
    requires_request_body = True

    def __init__(self, region: str):
        if importlib.util.find_spec("botocore") is None:
            raise MissingConfig("Bedrock 搜索的 IAM 鉴权需要可选依赖：pip install 'xiaoyu-agent[bedrock]'。")
        from botocore.session import get_session

        self.region = region
        self.credentials = get_session().get_credentials()
        if self.credentials is None:
            raise MissingConfig("未找到 AWS 凭证；请配置 AWS 凭证链或 AWS_BEARER_TOKEN_BEDROCK。")

    def auth_flow(self, request):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        #  OpenAI SDK 的占位 Bearer 头不能进入签名；临时凭证逐次冻结以支持自动刷新。
        request.headers.pop("Authorization", None)
        signed = AWSRequest(method=request.method, url=str(request.url),
                            data=request.content, headers=dict(request.headers))
        SigV4Auth(self.credentials.get_frozen_credentials(), "bedrock-mantle", self.region).add_auth(signed)
        request.headers.update(dict(signed.headers))
        yield request


def client(region: str, api_key: str | None, timeout: float | httpx2.Timeout):
    """SDK 出网仍走统一代理判定；有 AWS bearer token 时无需 botocore。"""
    from openai import OpenAI

    auth = None if api_key else _MantleAuth(region)
    http_client = netproxy.http_client()
    http_client.auth = auth
    #  固定服务端点不跟重定向，避免临时凭证头被转发到其它主机。
    http_client.follow_redirects = False
    try:
        return OpenAI(
            base_url=f"https://bedrock-mantle.{region}.api.aws/openai/v1",
            api_key=api_key or "aws-iam",
            timeout=timeout,
            max_retries=0,
            http_client=http_client,
        )
    except Exception:
        http_client.close()
        raise
