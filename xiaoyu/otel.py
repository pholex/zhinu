"""OpenTelemetry 导出：把每轮 / 每次模型调用 / 每次工具调用按 GenAI 语义约定打成 span，
推到用户**自己配置**的 collector。

这是运维可观测性，不是遥测：目的地只能是用户用标准变量
（OTEL_EXPORTER_OTLP_ENDPOINT 等）指定的地址；一个变量都没设就一个字节不发，
连 opentelemetry 包都不 import——这段 import 的成本不该落在没开它的人头上。

接法：它是 agent 事件流的一个旁观消费者（`Agent.emit` 在把事件交给前端 sink 之前
先过这里），再加轮边界两个显式调用（`begin_turn` / `end_turn`，事件流里没有轮
开始的事件）。所以 TUI、明文 REPL、`-p`、serve、ACP、嵌入 SDK 统统覆盖——它们
都经过同一个 `Agent.emit`。

span 树（一轮）：
    invoke_agent xiaoyu                  一轮 send()
    ├── chat gpt-x                       每次模型请求（含重试，各一个）
    ├── execute_tool bash                每次工具调用（被拒的也有，status 标明）
    │   └── invoke_agent explore         子 agent 的轮挂在触发它的工具 span 下
    │       ├── chat …
    │       └── execute_tool …
    └── chat gpt-x

属性名严格按 OTel GenAI 语义约定现版（gen_ai.*）；小羽特有的信息用 `xiaoyu.` 前缀。
内容（用户提示、工具参数、工具结果）默认**不**采：按约定只有
OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true 才写进属性，且先过凭据
脱敏、再截到 4KB——span 走的是 collector 那条线，不该成为凭据外泄的新出口。

线程模型：span 的父子关系全部靠显式 context 传递（`start_span(context=…)`），
不依赖线程上的全局 context——agent 的事件在工作线程里发出，serve 下多会话并发、
七襄的子 agent 跑在线程池里，依赖"当前线程的活动 span"会串线。导出走
BatchSpanProcessor：发出 span 只是入队，永远不阻塞主循环；进程退出时 shutdown
封顶 2 秒，collector 不在不能让退出挂住。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import sys
import threading
import time
import weakref

from typing import Any

#  ---------- 激活规则（标准变量，全部可在 .env 里设） ----------

ENV_SDK_DISABLED = "OTEL_SDK_DISABLED"
ENV_TRACES_EXPORTER = "OTEL_TRACES_EXPORTER"
ENV_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
ENV_TRACES_ENDPOINT = "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"
ENV_PROTOCOL = "OTEL_EXPORTER_OTLP_PROTOCOL"
ENV_TRACES_PROTOCOL = "OTEL_EXPORTER_OTLP_TRACES_PROTOCOL"
ENV_SERVICE_NAME = "OTEL_SERVICE_NAME"
ENV_CAPTURE_CONTENT = "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"

DEFAULT_SERVICE_NAME = "xiaoyu"
#  通用 endpoint 自动拼信号路径（OTLP/HTTP 规范）；信号专用 endpoint 原样用
TRACES_PATH = "/v1/traces"
#  进程退出 / 会话收尾时最多等导出这么久：collector 不在时退出不能挂住
SHUTDOWN_TIMEOUT_S = 2.0
#  开了内容采集后每个内容属性的上限（字符）
CONTENT_LIMIT = 4096
INSTALL_HINT = 'pip install "xiaoyu-agent[otel]"'

#  小羽 provider 名 → 约定里的 gen_ai.provider.name 已知值；不在表里的（网关、
#  自定义厂商）原样写——约定允许未列出的值，编一个反而误导
PROVIDER_NAMES = {
    "anthropic": "anthropic",
    "openai": "openai",
    "xai": "x_ai",
    "deepseek": "deepseek",
    "gemini": "gcp.gemini",
    "bedrock": "aws.bedrock",
    "moonshot": "moonshot_ai",
}


def _flag(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def decide(env: dict[str, str] | None = None) -> tuple[str, str]:
    """按标准变量判定导出方式：返回 (mode, target)。

    mode ∈ {"off", "console", "otlp"}；otlp 时 target 是 traces endpoint 的完整 URL。
    纯函数，便于测试逐条锁定规则：
    - OTEL_SDK_DISABLED=true 总关；
    - OTEL_TRACES_EXPORTER=none 关、console 打到 stderr、otlp（或未设）走 OTLP；
    - OTLP 的 endpoint 取 OTEL_EXPORTER_OTLP_TRACES_ENDPOINT，否则
      OTEL_EXPORTER_OTLP_ENDPOINT 拼 /v1/traces；两者都没设 → 不激活。
    """
    env = os.environ if env is None else env
    if _flag(env.get(ENV_SDK_DISABLED)):
        return "off", ""
    exporter = (env.get(ENV_TRACES_EXPORTER) or "").strip().lower()
    if exporter == "none":
        return "off", ""
    if exporter == "console":
        return "console", ""
    if exporter not in ("", "otlp"):
        #  jaeger / zipkin 这类老 exporter 名：不装它们的包，也不假装发出去了
        return "off", ""
    traces = (env.get(ENV_TRACES_ENDPOINT) or "").strip()
    if traces:
        return "otlp", traces
    base = (env.get(ENV_ENDPOINT) or "").strip()
    if base:
        return "otlp", base.rstrip("/") + TRACES_PATH
    return "off", ""


def _protocol(env: dict[str, str]) -> str:
    return (env.get(ENV_TRACES_PROTOCOL) or env.get(ENV_PROTOCOL) or "http/protobuf").strip().lower()


def capture_content(env: dict[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return _flag(env.get(ENV_CAPTURE_CONTENT))


#  ---------- 进程级单例：TracerProvider ----------

#  可重入：持锁装配 provider 的路上要 _say_once（缺包提示），同一把锁再进一次
_lock = threading.RLock()
_provider: Any = None
_tracer: Any = None
_mode = ""
#  测试注入：预先放一个 provider（InMemorySpanExporter），激活检查照常走环境变量
_preset_provider: Any = None
#  只说一次的那几句（装包提示、grpc 不支持、导出失败）
_said: set[str] = set()


def _say_once(key: str, text: str) -> None:
    with _lock:
        if key in _said:
            return
        _said.add(key)
    print(f"[otel] {text}", file=sys.stderr)


class _OnceHandler(logging.Handler):
    """OTel SDK 自己用 logging 报导出失败——每批一条，collector 不在时刷屏。
    收口成第一次报一句、之后静默：导出失败不影响任何功能，不值得反复提醒。"""

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        _say_once("export", f"导出告警：{message[:300]}（之后同类告警不再重复）")


def _quiet_sdk_logging() -> None:
    logger = logging.getLogger("opentelemetry")
    if any(isinstance(handler, _OnceHandler) for handler in logger.handlers):
        return
    logger.addHandler(_OnceHandler())
    #  不往 root 传：没配 logging 的进程会走 lastResort 再打一遍原文
    logger.propagate = False


def _build_provider(mode: str, target: str) -> Any:
    """真正 import opentelemetry 并装配 provider。缺包 / 协议不支持返回 None（已提示）。"""
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    except ImportError:
        _say_once("install", f"配了 OpenTelemetry 导出但没装包，本次不导出。安装：{INSTALL_HINT}")
        return None
    from . import __version__

    if mode == "console":
        exporter: Any = ConsoleSpanExporter(out=sys.stderr)
    else:
        protocol = _protocol(os.environ)
        if protocol != "http/protobuf":
            _say_once(
                "protocol",
                f"OTLP 协议 {protocol} 不支持（只做 http/protobuf），本次不导出。"
                "去掉 OTEL_EXPORTER_OTLP_PROTOCOL 或设为 http/protobuf",
            )
            return None
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        except ImportError:
            _say_once("install", f"配了 OTLP endpoint 但没装 exporter，本次不导出。安装：{INSTALL_HINT}")
            return None
        #  headers / timeout / 压缩等由 exporter 自己读标准变量；endpoint 这里已经
        #  按"信号专用优先、通用拼路径"算好，显式传进去
        exporter = OTLPSpanExporter(endpoint=target)
    #  Resource.create 会把 OTEL_SERVICE_NAME / OTEL_RESOURCE_ATTRIBUTES 叠在这些默认值之上
    resource = Resource.create(
        {"service.name": DEFAULT_SERVICE_NAME, "service.version": __version__}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _quiet_sdk_logging()
    return provider


def _get_tracer() -> Any:
    """按激活规则取进程级 tracer；不激活返回 None（此路径不 import opentelemetry）。"""
    global _provider, _tracer, _mode
    mode, target = decide()
    if mode == "off":
        return None
    with _lock:
        if _tracer is not None:
            return _tracer
        provider = _preset_provider or _build_provider(mode, target)
        if provider is None:
            return None
        _provider, _mode = provider, mode
        _tracer = provider.get_tracer("xiaoyu")
        atexit.register(shutdown)
        return _tracer


def flush(timeout_s: float = SHUTDOWN_TIMEOUT_S) -> None:
    """把队列里的 span 推出去，最多等 timeout_s。"""
    provider = _provider
    if provider is None:
        return
    try:
        provider.force_flush(timeout_millis=int(timeout_s * 1000))
    except Exception:  # noqa: BLE001
        pass


def shutdown(timeout_s: float = SHUTDOWN_TIMEOUT_S) -> None:
    """关 provider：放到守护线程里跑、封顶等待——exporter 对不可达的 collector
    会带重试等到自己的超时（默认 10 秒），不能让进程退出跟着挂那么久。"""
    global _provider, _tracer
    with _lock:
        provider, _provider, _tracer = _provider, None, None
    if provider is None:
        return
    worker = threading.Thread(target=provider.shutdown, name="otel-shutdown", daemon=True)
    worker.start()
    worker.join(timeout_s)


def _reset_for_tests(preset_provider: Any = None) -> None:
    """测试用：丢掉进程级状态，可预置一个 provider（InMemorySpanExporter）。"""
    global _provider, _tracer, _mode, _preset_provider
    with _lock:
        _provider, _tracer, _mode = None, None, ""
        _preset_provider = preset_provider
        _said.clear()


#  ---------- 父子串联：子 agent 的轮挂到触发它的工具 span 下 ----------

#  当前线程正在执行的工具 span（explore / 单发委托在父级线程里构造子 agent，这条最准）
_thread_local = threading.local()
#  线程外的兜底：七襄 / 宸枢把子 agent 构造在工作线程里，线程上没有现场。父子共用
#  同一个 Usage 对象（花的钱记一本账），按 (usage, 深度) 登记父级正在跑的工具 span，
#  子 agent 用 (usage, 自己的深度-1) 来找。同深度的兄弟并发时登记会互相覆盖，
#  只影响孙辈的挂接（默认 max_depth=1 没有孙辈）；parent_session.id 属性总是准的
_by_usage: "weakref.WeakKeyDictionary[Any, dict[int, Any]]" = weakref.WeakKeyDictionary()


def _register_tool_span(usage: Any, depth: int, entry: Any) -> None:
    _thread_local.tool = entry
    try:
        with _lock:
            _by_usage.setdefault(usage, {})[depth] = entry
    except TypeError:
        #  不可弱引用 / 不可哈希的 usage 替身（测试）：只走线程本地
        pass


def _unregister_tool_span(usage: Any, depth: int, entry: Any) -> None:
    if getattr(_thread_local, "tool", None) is entry:
        _thread_local.tool = None
    try:
        with _lock:
            slots = _by_usage.get(usage)
            if slots is not None and slots.get(depth) is entry:
                del slots[depth]
    except TypeError:
        pass


def _find_parent(usage: Any, depth: int) -> Any:
    entry = getattr(_thread_local, "tool", None)
    if entry is not None:
        return entry
    try:
        with _lock:
            return (_by_usage.get(usage) or {}).get(depth - 1)
    except TypeError:
        return None


#  ---------- 内容采集 ----------


def _content(value: Any) -> str:
    """开了采集时写进属性的内容：先脱敏再截断——截断落在令牌中间的话，剩下的半截
    对不上任何模式，原样漏出去。"""
    from .mcp import _redact

    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = _redact(text)
    if len(text) > CONTENT_LIMIT:
        text = text[:CONTENT_LIMIT] + f"…[截断，共 {len(text)} 字符]"
    return text


class AgentTracer:
    """一个 Agent 实例的 span 现场：当前轮、当前请求、在飞的工具调用。

    事件在 agent 的工作线程里顺序到达（一个 Agent 同一时刻只跑一轮），
    所以这里不加锁；跨 Agent 的共享状态在模块级，各自带锁。
    """

    def __init__(self, agent: Any, tracer: Any) -> None:
        from opentelemetry import trace as trace_api
        from opentelemetry.trace import SpanKind, Status, StatusCode

        self._api = trace_api
        self._kind = SpanKind
        self._status, self._code = Status, StatusCode
        self._agent = agent
        self._tracer = tracer
        self._capture = capture_content()
        config = getattr(agent, "config", None)
        self._depth = int(getattr(config, "subagent_depth", 0) or 0)
        self._usage = getattr(agent, "usage", None)
        #  构造时就把父级定下来：此刻父级的工具还在跑，登记项还在
        parent = _find_parent(self._usage, self._depth)
        self._parent_span = parent[0] if parent else None
        self._parent_session = parent[1] if parent else ""
        self._turn: Any = None
        self._turn_ctx: Any = None
        self._request: Any = None
        self._request_provider = ""
        self._tools: dict[str, Any] = {}
        #  轮内合计
        self._in = self._out = self._cached = 0
        self._requests = self._tool_calls = 0
        self._providers: set[str] = set()
        self._models: set[str] = set()

    #  ---- 身份 ----

    def _session_id(self) -> str:
        agent = self._agent
        log = getattr(agent, "session_log", None)
        if log is not None:
            named = getattr(log, "session_id", "")
            if named:
                return str(named)
            path = getattr(log, "path", None)
            if path is not None:
                return str(getattr(path, "stem", path))
        return str(getattr(agent, "_cache_key_fallback", "") or id(agent))

    def _agent_name(self) -> str:
        return str(getattr(self._agent, "agent_name", "") or DEFAULT_SERVICE_NAME)

    def _end(self, span: Any, error_type: str = "", description: str = "") -> None:
        if error_type:
            span.set_attribute("error.type", error_type)
            span.set_status(self._status(self._code.ERROR, description or error_type))
        else:
            span.set_status(self._status(self._code.OK))
        span.end()

    #  ---- 轮 ----

    def begin_turn(self, user_text: str) -> None:
        if self._turn is not None:
            #  上一轮没走到 end_turn（宿主绕过了 send 的 finally？）：先收口
            self.end_turn(RuntimeError("turn not ended"))
        self._in = self._out = self._cached = 0
        self._requests = self._tool_calls = 0
        self._providers, self._models = set(), set()
        parent_ctx = (
            self._api.set_span_in_context(self._parent_span) if self._parent_span is not None else None
        )
        config = getattr(self._agent, "config", None)
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": self._agent_name(),
            "gen_ai.conversation.id": self._session_id(),
            "session.id": self._session_id(),
            "gen_ai.request.model": str(getattr(config, "model", "") or ""),
            "xiaoyu.mode": str(getattr(self._agent, "mode", "") or ""),
            "xiaoyu.subagent.depth": self._depth,
        }
        if self._parent_session:
            attrs["xiaoyu.parent_session.id"] = self._parent_session
        if self._capture and user_text:
            attrs["gen_ai.input.messages"] = _content(
                [{"role": "user", "parts": [{"type": "text", "content": user_text}]}]
            )
        span = self._tracer.start_span(
            f"invoke_agent {self._agent_name()}",
            context=parent_ctx,
            kind=self._kind.INTERNAL,
            attributes=attrs,
        )
        self._turn = span
        self._turn_ctx = self._api.set_span_in_context(span)

    def end_turn(self, exc: BaseException | None = None) -> None:
        span, self._turn = self._turn, None
        if span is None:
            return
        #  轮结束了还在飞的请求 / 工具（异常路径）：先收它们，span 树不留悬空
        if self._request is not None:
            self._end_request(error_type=type(exc).__name__ if exc else "abandoned")
        for call_id in list(self._tools):
            self._finish_tool(call_id, error_type="abandoned", outcome="abandoned")
        span.set_attribute("gen_ai.usage.input_tokens", self._in)
        span.set_attribute("gen_ai.usage.output_tokens", self._out)
        span.set_attribute("gen_ai.usage.cache_read.input_tokens", self._cached)
        span.set_attribute("xiaoyu.turn.requests", self._requests)
        span.set_attribute("xiaoyu.turn.tool_calls", self._tool_calls)
        if self._providers:
            #  invoke_agent 要求 provider：一轮可能跨多家（降级链），记第一家、全集另给
            span.set_attribute("gen_ai.provider.name", sorted(self._providers)[0])
            span.set_attribute("xiaoyu.providers", sorted(self._providers))
        if self._models:
            span.set_attribute("xiaoyu.models", sorted(self._models))
        self._end(span, error_type=type(exc).__name__ if exc is not None else "", description=str(exc or ""))
        self._turn_ctx = None

    #  ---- 事件 ----

    def observe(self, event: Any) -> None:
        kind = getattr(event, "kind", "")
        if kind == "request.started":
            self._on_request_started(event)
        elif kind == "request.ended":
            self._on_request_ended(event)
        elif kind == "tool.pending":
            self._on_tool_pending(event)
        elif kind == "tool.running":
            self._on_tool_running(event)
        elif kind == "tool.completed":
            self._on_tool_completed(event)
        elif kind == "tool.denied":
            self._on_tool_denied(event)

    def _on_request_started(self, event: Any) -> None:
        if self._request is not None:
            self._end_request(error_type="abandoned")
        provider = PROVIDER_NAMES.get(event.provider, event.provider) or "unknown"
        self._request_provider = provider
        self._providers.add(provider)
        self._models.add(event.model)
        self._requests += 1
        self._request = self._tracer.start_span(
            f"chat {event.model}",
            context=self._turn_ctx,
            kind=self._kind.CLIENT,
            attributes={
                "gen_ai.operation.name": "chat",
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": event.model,
                "gen_ai.request.stream": True,
                "gen_ai.conversation.id": self._session_id(),
            },
        )

    def _on_request_ended(self, event: Any) -> None:
        span = self._request
        if span is None:
            return
        usage = event.usage or {}
        if usage:
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            cached = int(usage.get("cached_tokens") or 0)
            span.set_attribute("gen_ai.usage.input_tokens", prompt)
            span.set_attribute("gen_ai.usage.output_tokens", completion)
            span.set_attribute("gen_ai.usage.cache_read.input_tokens", cached)
            self._in += prompt
            self._out += completion
            self._cached += cached
        if event.ttft_ms is not None:
            span.set_attribute("gen_ai.response.time_to_first_chunk", event.ttft_ms / 1000)
            #  同一信息也作为时间线上的事件：看瀑布图时"首字何时到"一眼可见
            span.add_event("gen_ai.first_chunk", {"xiaoyu.ttft_ms": event.ttft_ms})
        if event.model:
            span.set_attribute("gen_ai.response.model", event.model)
        if event.response_id:
            span.set_attribute("gen_ai.response.id", event.response_id)
        if event.finish_reason:
            span.set_attribute("gen_ai.response.finish_reasons", [event.finish_reason])
        self._end_request(error_type=event.error)

    def _end_request(self, error_type: str = "") -> None:
        span, self._request = self._request, None
        if span is not None:
            self._end(span, error_type=error_type)

    def _tool_key(self, event: Any) -> str:
        call_id = getattr(event, "tool_call_id", "") or ""
        return call_id or f"name:{getattr(event, 'name', '')}"

    def _on_tool_pending(self, event: Any) -> None:
        key = self._tool_key(event)
        if key in self._tools:
            self._finish_tool(key, error_type="abandoned", outcome="abandoned")
        self._tool_calls += 1
        attrs: dict[str, Any] = {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": event.name,
            "gen_ai.tool.type": "function",
        }
        if getattr(event, "tool_call_id", ""):
            attrs["gen_ai.tool.call.id"] = event.tool_call_id
        if self._capture:
            attrs["gen_ai.tool.call.arguments"] = _content(event.args)
        span = self._tracer.start_span(
            f"execute_tool {event.name}",
            context=self._turn_ctx,
            kind=self._kind.INTERNAL,
            attributes=attrs,
        )
        #  pending 起算：审批等待也是这次调用的一部分，看瀑布图能分出"等人"与"在跑"
        entry = (span, self._session_id())
        self._tools[key] = entry

    def _on_tool_running(self, event: Any) -> None:
        entry = self._tools.get(self._tool_key(event))
        if entry is None:
            #  没见过 pending（前端替身直接发 running）：补开一个
            self._on_tool_pending(event)
            entry = self._tools[self._tool_key(event)]
        entry[0].add_event("running")
        _register_tool_span(self._usage, self._depth, entry)

    def _on_tool_completed(self, event: Any) -> None:
        key = self._tool_key(event)
        entry = self._tools.get(key)
        if entry is None:
            return
        span = entry[0]
        span.set_attribute("xiaoyu.tool.seconds", float(event.seconds))
        if self._capture:
            span.set_attribute("gen_ai.tool.call.result", _content(event.output))
        self._finish_tool(key, error_type="" if event.ok else "tool_error", outcome="ok" if event.ok else "error")

    def _on_tool_denied(self, event: Any) -> None:
        key = self._tool_key(event)
        if key not in self._tools:
            return
        span = self._tools[key][0]
        span.set_attribute("xiaoyu.tool.denied_by", event.by)
        self._finish_tool(key, error_type=f"denied_by_{event.by}", outcome="denied")

    def _finish_tool(self, key: str, *, error_type: str, outcome: str) -> None:
        entry = self._tools.pop(key, None)
        if entry is None:
            return
        _unregister_tool_span(self._usage, self._depth, entry)
        span = entry[0]
        span.set_attribute("xiaoyu.tool.outcome", outcome)
        self._end(span, error_type=error_type)

    #  ---- 收尾 ----

    def close(self) -> None:
        """会话收尾：轮已结束，把队列推出去（封顶等待）。"""
        if self._turn is not None:
            self.end_turn()
        flush()


def attach(agent: Any) -> AgentTracer | None:
    """按激活规则给一个 Agent 挂上 tracer；不激活返回 None。任何失败都不影响 agent。"""
    try:
        tracer = _get_tracer()
        if tracer is None:
            return None
        return AgentTracer(agent, tracer)
    except Exception as exc:  # noqa: BLE001
        _say_once("attach", f"初始化失败，本次不导出：{type(exc).__name__}: {exc}")
        return None
