"""API 错误分类器（按小羽体量收敛）。

把"这个错能不能重试 / 该不该先压缩 / 是不是配置问题"的判断收敛到一处，
主循环拿着结论行动，不在各处散落 try/except 猜错误类型。
"""

from __future__ import annotations

from dataclasses import dataclass

#  httpx 是 openai / anthropic 两个 SDK 共同的硬传递依赖，顶层 import 安全
import httpx
import openai


@dataclass(frozen=True)
class Verdict:
    kind: str  # 取值必须在 ALL_KINDS 里
    retryable: bool
    should_compact: bool
    hint: str  # 一句话给用户看


#  classify 可能给出的全部分类。新增分类必须同步进这里——
#  tests/test_errors.py 会遍历断言每一类都可构造、hint 非空、展示路径不炸。
ALL_KINDS = ("rate_limit", "transient", "context_overflow", "quota", "auth", "fatal")


#  上下文超限的报错各家措辞不一（LiteLLM 还会转写），按关键词兜底识别
_CONTEXT_MARKERS = (
    "context length",
    "context_length",
    "maximum context",
    "too many tokens",
    "prompt is too long",
    "input is too long",
)

#  长得像上下文超限、实为限流的文案：Bedrock 的 ThrottlingException 原文是
#  "Too many tokens, please wait before trying again"，会撞上 _CONTEXT_MARKERS
#  的 "too many tokens"——误判成超限会触发一次无意义的压缩。这类必须先于
#  超限判定按限流处理。
_THROTTLE_NOT_OVERFLOW = ("too many tokens, please wait",)

#  额度/配额耗尽：多以 429 或 RateLimitError 的皮出现，但和限流本质不同——
#  限流等一等就过去，额度用尽等多久都没用，重试只是白挨。得先于限流判定拦下。
#  措辞来源：openai 的 insufficient_quota、LiteLLM 的 budget 系（llm 网关按
#  $/月 限额，这条链路真实存在）。按实测补充，别泛化到裸 "quota"/"budget"
#  单词——太宽会把正常业务文案误伤进来。
_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "exceeded your current quota",
    "monthly usage limit",
    "budget has been exceeded",
    "out of budget",
)


def _status_code(exc: Exception) -> int | None:
    """SDK 异常上的 HTTP 状态码。openai 和 anthropic（同为 Stainless 生成）的
    APIStatusError 都带 `.status_code`——鸭子取值，errors.py 就不必 import
    anthropic（它是 messages.py 才需要的依赖，分类器不该反向耦合协议层）。"""
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def classify(exc: Exception) -> Verdict:
    text = str(exc).lower()
    status = _status_code(exc)

    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError) or status in (
        401,
        403,
    ):
        return Verdict("auth", False, False, "鉴权失败，请检查 XIAOYU_API_KEY 和端点")

    if any(marker in text for marker in _QUOTA_MARKERS):
        #  额度耗尽：同一路重试无解，但降级链上换一家值得试（agent.py 放行）
        return Verdict("quota", False, False, "额度/配额已用尽（重试无用，充值或 /model 换路由）")

    if any(marker in text for marker in _THROTTLE_NOT_OVERFLOW):
        #  Bedrock 式"tokens 措辞的限流"：先于超限判定拦下，绝不触发压缩
        return Verdict("rate_limit", True, False, "限流")

    if any(marker in text for marker in _CONTEXT_MARKERS):
        #  上下文超限：压缩后值得立刻重试
        return Verdict("context_overflow", True, True, "上下文超限，压缩后重试")

    if (
        isinstance(exc, openai.RateLimitError)
        or status == 429
        or "rate limit" in text
        or "throttl" in text  # AWS 系措辞：ThrottlingException / throttled
        or "429" in text
    ):
        return Verdict("rate_limit", True, False, "限流")

    if (
        isinstance(
            exc,
            openai.APITimeoutError | openai.APIConnectionError | openai.InternalServerError,
        )
        #  >=500 覆盖非 openai SDK 的服务端错误（含 anthropic 529 overloaded）
        or (status is not None and status >= 500)
        #  anthropic 的连接/超时异常是 `raise ... from <httpx 异常>`，认底因即可
        or isinstance(exc.__cause__, httpx.HTTPError)
    ):
        return Verdict("transient", True, False, f"网络/服务端瞬时错误（{type(exc).__name__}）")

    return Verdict("fatal", False, False, f"{type(exc).__name__}: {exc}")


#  Retry-After 的采信上限（秒）：服务端偶尔会给出几小时后的值，
#  交互场景等不了，超过就按上限退避。
RETRY_AFTER_CAP = 60.0


def retry_after_seconds(exc: Exception) -> float | None:
    """从异常里挖 Retry-After 头（秒）。挖不到或不是数字返回 None。

    服务端明确说了等多久，就该听它的而不是盲目指数退避——
    限流窗口没过去之前，早重试只是白挨一次 429。
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:  # noqa: BLE001 - headers 形状不对就当没有
        return None
    if not raw:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        #  HTTP-date 形式的 Retry-After 极少见，不为它引入日期解析
        return None
    if seconds <= 0:
        return None
    return min(seconds, RETRY_AFTER_CAP)
