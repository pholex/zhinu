"""API 错误分类器（按小羽体量收敛）。

把"这个错能不能重试 / 该不该先压缩 / 是不是配置问题"的判断收敛到一处，
主循环拿着结论行动，不在各处散落 try/except 猜错误类型。
"""

from __future__ import annotations

from dataclasses import dataclass

#  httpx 是 openai / anthropic 两个 SDK 共同的硬传递依赖，顶层 import 安全
import httpx
import openai


class ContentFiltered(RuntimeError):
    """服务端内容过滤/安全分类器拒答（HTTP 200、没有可用内容）。

    三种协议的形态各异（chat 的 finish_reason=content_filter、Responses 的
    incomplete reason、Anthropic 的 stop_reason=refusal），传输层与流消费层
    统一抛这一个类型。它不是故障：同一请求原样重发或换模型，结果多半相同，
    还会把拒答放大成连环请求——分类为 fatal，直接报给用户。
    """


class StreamTruncated(RuntimeError):
    """流在工具调用参数写到一半时结束，且没有任何收尾信号（finish_reason / usage）。

    这是断流不是模型输出：执行残缺调用只会换来"参数不是合法 JSON"白费一步。
    分类为 transient，复用可重试网络错误的预算与退避原地重发。
    """


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
    #  余额类：DeepSeek 402 的 "Insufficient Balance"、Anthropic 400 的
    #  "Your credit balance is too low"、HTTP 402 的标准原因短语
    "insufficient balance",
    "credit balance is too low",
    "payment required",
    #  OpenAI 系结构化错误码（兼容聚合端点也照抄）：余额耗尽与组织/项目级
    #  spend/usage 上限。码在异常文本里也常原样出现，按子串兜一次
    "credit_balance_exhausted",
    "_spend_limit_exceeded",
    "_usage_limit_exceeded",
    "billing_hard_limit_reached",
)

#  "billing" 单词太宽：限流文案里常附一句"去 billing 页面提额"之类的链接。
#  只在文案不像限流时才认它是额度问题（限流等得起，误判成 quota 会跳过退避
#  直接放弃这一路）
_BILLING_MARKER = "billing"
_THROTTLE_WORDS = ("rate limit", "rate_limit", "throttl", "too many requests")

#  结构化错误码（exc.code / body.error.code）的精确值与后缀：流式里冒出来的
#  错误常常只有 body，str(exc) 里不一定带码
_QUOTA_CODES = ("insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached")
_QUOTA_CODE_SUFFIXES = ("_spend_limit_exceeded", "_usage_limit_exceeded")


def _status_code(exc: Exception) -> int | None:
    """SDK 异常上的 HTTP 状态码。openai 和 anthropic（同为 Stainless 生成）的
    APIStatusError 都带 `.status_code`——鸭子取值，errors.py 就不必 import
    anthropic（它是 messages.py 才需要的依赖，分类器不该反向耦合协议层）。"""
    status = getattr(exc, "status_code", None)
    return status if isinstance(status, int) else None


def _error_code(exc: Exception) -> str:
    """异常携带的结构化错误码（小写），取不到返回空串。

    openai 的 APIError 把 body 里的 code 挂在 `.code` 上；鸭子型或转写过的
    异常只剩 `.body`，按 `{"code": …}` 与 `{"error": {"code": …}}` 两种形状各取一次。
    """
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            inner = body.get("error")
            code = body.get("code") or (inner.get("code") if isinstance(inner, dict) else None)
    return code.lower() if isinstance(code, str) else ""


def _is_quota(exc: Exception, text: str, status: int | None) -> bool:
    #  402 Payment Required 无论文案都是额度问题
    if status == 402:
        return True
    code = _error_code(exc)
    if code and (code in _QUOTA_CODES or code.endswith(_QUOTA_CODE_SUFFIXES)):
        return True
    if any(marker in text for marker in _QUOTA_MARKERS):
        return True
    return (
        _BILLING_MARKER in text
        and status != 429
        and not isinstance(exc, openai.RateLimitError)
        and not any(word in text for word in _THROTTLE_WORDS)
    )


def classify(exc: Exception) -> Verdict:
    if isinstance(exc, ContentFiltered):
        return Verdict(
            "fatal", False, False,
            f"{exc}（重发或换模型多半同样被拦，请调整请求内容）",
        )
    if isinstance(exc, StreamTruncated):
        #  先于文本判定：断流描述里的措辞不该撞上任何 marker
        return Verdict("transient", True, False, "流在工具参数写到一半时断开")
    text = str(exc).lower()
    status = _status_code(exc)

    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError) or status in (
        401,
        403,
    ):
        return Verdict("auth", False, False, "鉴权失败，请检查 XIAOYU_API_KEY 和端点")

    if _is_quota(exc, text, status):
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
