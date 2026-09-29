"""API 错误分类器（按小羽体量收敛）。

把"这个错能不能重试 / 该不该先压缩 / 是不是配置问题"的判断收敛到一处，
主循环拿着结论行动，不在各处散落 try/except 猜错误类型。
"""

from __future__ import annotations

import re
import ssl
import sys
from dataclasses import dataclass
from typing import Callable

#  httpx 是 openai / anthropic 两个 SDK 共同的硬传递依赖，顶层 import 安全
import httpx


#  打断时挂在异常上的属性名：批量工具把已收束各项的报告放在这里再上抛，主循环
#  把它作为这次工具调用的结果写进历史。打断语义不变（异常照样往上走），只是已经
#  花掉的那些子 agent 的结论和续跑句柄不再跟着一起丢。
PARTIAL_OUTPUT = "partial_output"


def attach_partial(exc: BaseException, build: "Callable[[], str]") -> None:
    """给正在上抛的异常挂上部分报告。生成报告本身出错不能盖掉原来的异常。"""
    try:
        setattr(exc, PARTIAL_OUTPUT, build())
    except Exception:  # noqa: BLE001
        pass


class Interrupted(Exception):
    """`Agent.interrupt()` 触发的打断——不是 OS 信号，是宿主线程/协程主动请求的。

    刻意**不**继承 `KeyboardInterrupt`（最初这么写过，被 async 场景的测试炸出来
    才改掉）：`asyncio.Task` 对 `(KeyboardInterrupt, SystemExit)` 有特殊处理——
    不会把它们收进 Task 的结果里正常传播，而是直接原样捅穿事件循环，效果等同于
    "整个进程被 Ctrl-C 了"。库层嵌入场景下 `interrupt()` 跑在 `asyncio.to_thread`
    包着的工作线程里，这个特殊处理会导致 `await async_agent.send(...)` 直接把
    宿主的整个事件循环带崩，而不是像一次普通异常那样被 `try/except` 接住。

    `_stream_once` 的收尾分支同时捕获 `(KeyboardInterrupt, Interrupted)`——两条
    触发路径共用同一段"半截话入历史、残缺 tool_calls 丢弃"的逻辑，但只有真的
    OS 信号才会被顶层特殊对待。

    定义在 errors 而不是 agent：工具层（前台命令的等待循环）也要抛它，
    不能反过来依赖 agent。`xiaoyu.agent.Interrupted` 仍是同一个类。
    """


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


class StreamFailed(RuntimeError):
    """流已经开始、中途收到服务端的错误事件（HTTP 层是 200，没有状态码可判）。

    上游原文照带，`classify` 先照常按文本判（额度耗尽、鉴权失败这类仍各归各位），
    **只有全都没命中时才兜底成 transient 而不是 fatal**：流都跑起来了才炸，
    绝大多数是容量/过载一类的瞬时故障（xAI 的 "at capacity"、Anthropic 的
    overloaded_error 都走这条路）。判 fatal 会让一次抖动直接打死整轮——既不退避
    也不换路由，而重试预算本来就是全链共享且有界的，兜底成 transient 代价可控。
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
    #  漏网会很贵：判成 fatal 整轮直接死、不压缩；判成 transient 更糟——同一个
    #  超大请求带退避原样重发，烧光重试预算也不会触发压缩
    "context window",  # "exceeds the context window of this model"
    "context limit",  # "input length and max_tokens exceed context limit"
    "maximum number of tokens allowed",  # "input token count … exceeds the maximum …"
    "range of input length should be",
    "token length exceed",  # "total message token length exceed model limit"
)

#  结构化错误码里的超限：码比措辞稳（文案会改、会被网关转写，码不会）
_CONTEXT_CODES = (
    "context_length_exceeded",
    "context_window_exceeded",
    "model_context_window_exceeded",
)

#  厂商业务码：这几家的错误体是 `{"code": "1302", "message": "中文文案"}`，流内错误
#  事件里有时连结构化 code 都没有，只剩 `[1302][文案][request_id]` 形态的 message。
#  措辞兜底全是英文，认不出中文文案——按码分类。只收语义确定的几个，拿不准的宁缺。
_VENDOR_CODES = {
    "1261": "context_overflow",  # 输入超长
    "1113": "quota",  # 账户欠费 / 余额不足
    "1304": "quota",  # 当日调用次数用尽：等多久都没用，换路由
    "1302": "rate_limit",  # 并发过高
    "1303": "rate_limit",  # 频率过高
    "1305": "rate_limit",  # 触发流量限制
}
_BRACKETED_CODE = re.compile(r"^\s*\[(\d{3,6})\]\[")

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

#  服务端瞬时故障的文案兜底。**为什么必须有**：流内错误事件（SSE `event: error`）
#  在 HTTP 层是 200——openai SDK 抛的是没有状态码的裸 `APIError`，anthropic SDK 抛的
#  是 status_code=200 的 APIStatusError，两者都躲开了"状态码 >= 500"那条判据。
#  于是最常见的一类瞬时故障（Anthropic 的 overloaded_error、xAI 的容量错误）会被
#  判成 fatal：不退避、不换路由，一次抖动打死整轮。按措辞兜住它们。
#  只收**明确说了是服务端侧临时问题**的词，别泛化——太宽会把真正的请求错误也拖进重试。
_TRANSIENT_MARKERS = (
    "overloaded",
    "service unavailable",
    "service_unavailable",
    "internal server error",
    "internal_error",
    "bad gateway",
    "gateway timeout",
    "upstream connect",
    "provider returned error",
    #  "临时没容量，等会儿再来"：xAI 容量错误、各家网关排队满的标准措辞
    "at capacity",
    "try again later",
    "try again in",
)

#  gRPC 系（Gemini / Vertex）的限流码。Google 对"每分钟配额用尽"也报这个，
#  按限流处理正合适——退避等一等就过去了，和 _QUOTA_MARKERS 那种充值才能解的不同
_EXHAUSTED_MARKERS = ("resource exhausted", "resource_exhausted")

#  结构化错误码（exc.code / body.error.code）的精确值与后缀：流式里冒出来的
#  错误常常只有 body，str(exc) 里不一定带码
_QUOTA_CODES = ("insufficient_quota", "credit_balance_exhausted", "billing_hard_limit_reached")
_QUOTA_CODE_SUFFIXES = ("_spend_limit_exceeded", "_usage_limit_exceeded")


def _is_openai(exc: Exception, *names: str) -> bool:
    """`exc` 是不是 openai SDK 的这几类异常之一。

    不 import openai：它一次 import 约 0.25 秒，而分类器被 CLI 启动路径间接
    拖起。进程里还没人 import 过 openai，就不可能存在它的异常实例，直接判否。
    """
    module = sys.modules.get("openai")
    if module is None:
        return False
    kinds = tuple(
        kind for name in names if isinstance(kind := getattr(module, name, None), type)
    )
    return isinstance(exc, kinds)


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
    if not isinstance(code, (str, int)) or isinstance(code, bool) or code in ("", None):
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            inner = body.get("error")
            code = body.get("code") or (inner.get("code") if isinstance(inner, dict) else None)
    if isinstance(code, int) and not isinstance(code, bool):
        #  业务码有的厂商给数字、有的给数字串
        code = str(code)
    return code.lower() if isinstance(code, str) else ""


def error_code(exc: BaseException) -> str:
    """异常携带的结构化错误码（公开入口，留痕用）。"""
    return _error_code(exc)  # type: ignore[arg-type]


def _vendor_kind(exc: Exception) -> str:
    """厂商业务码对应的分类，认不出返回空串。结构化码优先，其次从
    `[码][文案][request_id]` 形态的 message 开头取。"""
    code = _error_code(exc)
    if code not in _VENDOR_CODES:
        match = _BRACKETED_CODE.match(str(exc))
        code = match.group(1) if match else ""
    return _VENDOR_CODES.get(code, "")


def _certificate_failure(exc: BaseException) -> bool:
    """底因链上有没有证书校验失败。SDK 把它包成连接错误再抛，得顺着链找。"""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ssl.SSLCertVerificationError):
            return True
        if "certificate verify failed" in str(current).lower():
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_quota(exc: Exception, text: str, status: int | None) -> bool:
    #  402 Payment Required 无论文案都是额度问题
    if status == 402:
        return True
    code = _error_code(exc)
    if code and (code in _QUOTA_CODES or code.endswith(_QUOTA_CODE_SUFFIXES)):
        return True
    if _vendor_kind(exc) == "quota":
        return True
    if any(marker in text for marker in _QUOTA_MARKERS):
        return True
    return (
        _BILLING_MARKER in text
        and status != 429
        and not _is_openai(exc, "RateLimitError")
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

    if _certificate_failure(exc):
        #  先于瞬时判定：SDK 把它包成连接错误，按瞬时处理就是重试几次再换路由——
        #  而证书问题（公司代理做 TLS 中间人最常见）换哪条路都一样败，用户看到的
        #  却只是"网络瞬时错误"
        return Verdict(
            "fatal", False, False,
            "TLS 证书校验失败（重试无用）。走公司代理的话，把代理的根证书路径设给 "
            "SSL_CERT_FILE；不是的话检查系统时间与端点地址",
        )

    if _is_openai(exc, "AuthenticationError", "PermissionDeniedError") or status in (401, 403):
        return Verdict("auth", False, False, "鉴权失败，请检查 XIAOYU_API_KEY 和端点")

    if _is_quota(exc, text, status):
        #  额度耗尽：同一路重试无解，但降级链上换一家值得试（agent.py 放行）
        return Verdict("quota", False, False, "额度/配额已用尽（重试无用，充值或 /model 换路由）")

    if any(marker in text for marker in _THROTTLE_NOT_OVERFLOW):
        #  Bedrock 式"tokens 措辞的限流"：先于超限判定拦下，绝不触发压缩
        return Verdict("rate_limit", True, False, "限流")

    vendor = _vendor_kind(exc)
    if (
        vendor == "context_overflow"
        or _error_code(exc) in _CONTEXT_CODES
        or any(marker in text for marker in _CONTEXT_MARKERS)
    ):
        #  上下文超限：压缩后值得立刻重试
        return Verdict("context_overflow", True, True, "上下文超限，压缩后重试")

    if (
        _is_openai(exc, "RateLimitError")
        or status == 429
        or "rate limit" in text
        or "throttl" in text  # AWS 系措辞：ThrottlingException / throttled
        or "429" in text
        or vendor == "rate_limit"
        or any(marker in text for marker in _EXHAUSTED_MARKERS)
    ):
        return Verdict("rate_limit", True, False, "限流")

    if (
        _is_openai(exc, "APITimeoutError", "APIConnectionError", "InternalServerError")
        #  >=500 覆盖非 openai SDK 的服务端错误（含 anthropic 529 overloaded）
        or (status is not None and status >= 500)
        #  anthropic 的连接/超时异常是 `raise ... from <httpx 异常>`，认底因即可
        or isinstance(exc.__cause__, httpx.HTTPError)
        #  没有状态码可判的流内错误事件，只剩措辞可认（见 _TRANSIENT_MARKERS）
        or any(marker in text for marker in _TRANSIENT_MARKERS)
    ):
        return Verdict("transient", True, False, f"网络/服务端瞬时错误（{type(exc).__name__}）")

    if isinstance(exc, StreamFailed):
        #  流跑起来了才炸、且措辞没落进上面任何一类：兜底成 transient（理由见类注释）
        return Verdict("transient", True, False, f"流中途失败（{exc}）")

    if status is None and _is_openai(exc, "APIError"):
        #  chat 一路的同一种情况：流内错误事件在 HTTP 层是 200，SDK 抛的是没有状态码
        #  的裸 APIError（带状态码的请求错误都是 APIStatusError，走不到这里）。措辞
        #  兜底只认英文，厂商的中文文案一条都命不中——与 StreamFailed 同样兜底
        return Verdict("transient", True, False, f"流中途失败（{exc}）")

    return Verdict("fatal", False, False, f"{type(exc).__name__}: {exc}")


#  Retry-After 的采信上限（秒）：服务端偶尔会给出几小时后的值，
#  交互场景等不了，超过就按上限退避。
RETRY_AFTER_CAP = 60.0


_REQUEST_ID_HEADERS = ("x-request-id", "request-id", "x-amzn-requestid", "cf-ray")


def request_id(exc: BaseException) -> str:
    """上游给这次请求的编号：找厂商排查时对方第一句就是问它。取不到返回空串。"""
    direct = getattr(exc, "request_id", None)
    if isinstance(direct, str) and direct:
        return direct
    headers = getattr(getattr(exc, "response", None), "headers", None)
    if headers is None:
        return ""
    for name in _REQUEST_ID_HEADERS:
        try:
            value = headers.get(name)
        except Exception:  # noqa: BLE001 - headers 形状不对就当没有
            return ""
        if isinstance(value, str) and value:
            return value
    return ""


def retry_after_seconds(exc: Exception) -> float | None:
    """从异常里挖 Retry-After 头（秒）。挖不到或不是数字返回 None。

    服务端明确说了等多久，就该听它的而不是盲目指数退避——
    限流窗口没过去之前，早重试只是白挨一次 429。

    `retry-after-ms` 优先于 `retry-after`：OpenAI 系两个头一起发，秒级那个是向下
    取整的（"等 1.4 秒"会写成 `retry-after: 1`），照它重试仍在窗口内、白挨一次。
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    try:
        raw_ms = headers.get("retry-after-ms")
        raw = headers.get("retry-after")
    except Exception:  # noqa: BLE001 - headers 形状不对就当没有
        return None
    #  除以 1000 而不是乘 0.001：后者 1400 会算出 1.4000000000000001
    for value, divisor in ((raw_ms, 1000.0), (raw, 1.0)):
        if not value:
            continue
        try:
            seconds = float(value) / divisor
        except (TypeError, ValueError):
            #  HTTP-date 形式的 Retry-After 极少见，不为它引入日期解析；
            #  解析不了就当这个头没有，接着看下一个候选
            continue
        if seconds > 0:
            return min(seconds, RETRY_AFTER_CAP)
    return None
