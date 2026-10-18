"""web_fetch 工具：抓取一个网页，拿回标题与 Markdown 正文。

为什么单独做一个工具而不是让 web_search 顺带读页：
- web_search 回来的是模型写好的结论，链接只是脚注；模型要核对原文、读文档的
  某一节、看报错对应的 issue 全文时，需要一条"给我 URL、还我正文"的直路；
- browser 工具能读页但依赖可选的 playwright，起浏览器也重；绝大多数"读一页"
  的需求用服务端抓取就够了。

抓取发生在 DeepSeek 服务端（`/v1/tools/fetch`，纯 JSON 接口，与模型调用同一把
key），本机不直接访问目标站点：没有本地出网面，也不会把内网地址暴露给模型。
每次成功抓取按次计费（不是 token），所以失败不自动重试，由模型决定要不要再试。
正文是外部内容，回灌模型前由 agent 包进不可信标记。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx2

from . import netproxy, ui
from .config import Config
from .events import Notice, UISink
from .mcp import _redact
from .providers import PRESETS, Registry, request_timeout
from .render import PlainSink
from .tools import Tool

#  抓取走哪家：目前只有 DeepSeek 开放了独立的抓取接口；日后别家开放再抽成表。
FETCH_PROVIDER = "deepseek"
FETCH_PATH = "/v1/tools/fetch"
#  官方限制：URL 最长 8192 字符、只认 http/https、不能内嵌用户名密码
MAX_URL_CHARS = 8192
#  一页正文留在上下文里的上限：比 web_search 的结论（4000）宽，因为它存在的
#  理由就是读原文；再长的部分落盘可召回（见 tools.py 的 output_limit 语义）
MAX_PAGE_CHARS = 12_000

#  官方状态码语义（guides/search）：4xx 里只有 408/429 值得稍后再试
_STATUS_HINTS = {
    400: "请求无效（URL 格式不被接受）",
    401: "DEEPSEEK_API_KEY 无效",
    402: "DeepSeek 账户余额不足，无法抓取",
    403: "目标页面被 DeepSeek 的安全策略拒绝抓取",
    404: "目标页面没有可用正文（可能需要登录、是二进制文件或已失效）",
    408: "抓取超时，可稍后重试一次",
    413: "目标页面过大，无法抓取",
    429: "抓取请求过于频繁（限 5 QPS），请稍后重试",
}


#  服务端返回的正文把**所有**非字母数字字符都加了反斜杠（`\#`、`\-`、`\'`、`\/`），
#  真实的反斜杠则成了 `\\`——2026-10-18 抓官方文档与 Python re 文档实测如此。
#  这种全量转义是精确可逆的，原样回灌只会让模型读得费劲、还多付约两成 token。
_ESCAPED_PUNCT = re.compile(r"\\([^A-Za-z0-9\s])")


def unescape_markdown(text: str) -> str:
    """去掉服务端的全量标点转义；`\\\\` 还原成一个反斜杠，字母数字前的反斜杠不动。"""
    return _ESCAPED_PUNCT.sub(r"\1", text)


def validate_url(url) -> str | None:
    """本地先按官方限制校验，不合规的请求不发出去（发出去也只会 400）。"""
    if not isinstance(url, str) or not url.strip():
        return "url 必须是非空字符串。"
    url = url.strip()
    if len(url) > MAX_URL_CHARS:
        return f"url 超过 {MAX_URL_CHARS} 字符。"
    try:
        parts = urlsplit(url)
    except ValueError:
        return "url 不是合法的 URL。"
    if parts.scheme not in ("http", "https"):
        return "url 只支持 http / https。"
    if not parts.hostname:
        return "url 缺少主机名。"
    if parts.username is not None or parts.password is not None:
        return "url 不能内嵌用户名或密码。"
    return None


def fetch_endpoint(base_url: str) -> str:
    """从 provider 的 base_url（…/v1）推出抓取端点；网关形态的 base_url 也照此拼。"""
    base = base_url.rstrip("/").removesuffix("/v1")
    return base + FETCH_PATH


def make_web_fetch_tool(config: Config, registry: Registry, sink: UISink | None = None) -> Tool:
    """造一个 web_fetch 工具挂到主 agent 上。每次调用新建 HTTP client、用完关闭。"""
    sink = sink or PlainSink()

    def web_fetch(url: str) -> str:
        if not config.enable_web_fetch:
            return "ERROR: 网页抓取已禁用（XIAOYU_ENABLE_WEB_FETCH）。"
        provider = registry.get(FETCH_PROVIDER)
        if provider is None:
            key = PRESETS[FETCH_PROVIDER].key_envs[0]
            return f"ERROR: 未配置 {FETCH_PROVIDER} 直连（缺 {key}），网页抓取不可用。请改用其它途径，或提示用户配置。"
        if problem := validate_url(url):
            return f"ERROR: {problem}"
        url = url.strip()
        sink.emit(Notice(f"  🌐 web_fetch：{ui.preview(url, 90)}"))

        try:
            with netproxy.http_client() as client:
                #  固定服务端点不跟重定向：Authorization 头不该被转发到别的主机
                client.follow_redirects = False
                response = client.post(
                    fetch_endpoint(provider.base_url),
                    json={"url": url},
                    headers={"Authorization": f"Bearer {provider.api_key}",
                             "Content-Type": "application/json"},
                    timeout=request_timeout(config.request_timeout),
                )
        except Exception as exc:  # noqa: BLE001 - 抓取失败不该打断主流程
            return (
                f"ERROR: 网页抓取失败（{type(exc).__name__}: {_redact(str(exc))}）。"
                "可以稍后重试一次；再失败就基于已有信息继续，并向用户说明。"
            )

        if response.status_code != 200:
            return f"ERROR: {_status_message(response)}"
        try:
            body = response.json()
        except ValueError:
            return "ERROR: 网页抓取返回了无法解析的响应，请稍后重试。"
        if not isinstance(body, dict):
            return "ERROR: 网页抓取返回了意外的响应形态，请稍后重试。"
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return "ERROR: 目标页面没有返回正文。请换一个 URL，或基于已有信息继续。"
        title = body.get("title")
        title = title.strip() if isinstance(title, str) else ""

        header = f"[网页抓取 · 由 DeepSeek 服务端抓取，正文代表该页面作者的说法]\nURL：{url}"
        if title:
            header += f"\n标题：{title}"
        return f"{header}\n\n{unescape_markdown(content).strip()}"

    return Tool(
        name="web_fetch",
        output_limit=MAX_PAGE_CHARS,
        description=(
            "抓取一个网页，返回标题与正文（Markdown）。适合：读 web_search 结论里"
            "引用的原文、看文档或 issue 的具体内容、核对某个页面现在写了什么。"
            "只读公开网页，不访问本地文件；需要登录或交互的页面抓不到。"
            "按次计费，确有需要再调。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "要抓取的完整 http/https 地址",
                }
            },
            "required": ["url"],
        },
        handler=web_fetch,
        untrusted=True,
        #  只读互联网、不改本地；请求发往用户自己配了 key 的厂商官方端点，
        #  信任级别与 web_search 相同，逐次确认只会让模型不用它。
        requires_approval=False,
        #  没配 DeepSeek 直连就不进 schemas（handler 里再兜一次底，错误文案更具体）
        check_fn=lambda: config.enable_web_fetch and registry.get(FETCH_PROVIDER) is not None,
    )


def _status_message(response: httpx2.Response) -> str:
    """非 200 的报错：优先官方语义，附服务端 message（已脱敏、截短）。"""
    code = response.status_code
    hint = _STATUS_HINTS.get(code)
    if hint is None:
        hint = "DeepSeek 抓取服务暂时不可用，可稍后重试" if code >= 500 else f"抓取请求被拒绝（HTTP {code}）"
    detail = ""
    try:
        error = response.json().get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            detail = error["message"]
    except (ValueError, AttributeError):
        detail = response.text
    detail = _redact(detail.strip())[:200] if isinstance(detail, str) else ""
    message = f"{hint}（HTTP {code}）。"
    if detail:
        message += f" 服务端说明：{detail}"
    if code in (408, 429) or code >= 500:
        message += " 这类错误可以等一会再试一次。"
    else:
        message += " 不要原样重试；请换 URL 或改用其它途径。"
    return message

