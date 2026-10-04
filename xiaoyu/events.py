"""UI 事件对象：agent 面向前端的输出，统一为类型化、可序列化的事件流。

前端协议设计：核心只产出带判别字段（kind，
点分命名如 tool.completed）的事件对象，所有前端——进程内的明文 REPL、
TUI，将来可能的 headless/SSE/IDE 插件——消费同一套词汇。今天事件在
进程内直接分发给 UISink；哪天要跨进程，`to_dict()` + JSON 就是线上协议，
不用重新设计。

模型调用本身也有一对事件（request.started / request.ended）：它覆盖的是
"请求已发出、还没有任何输出"这段空白，没有它前端只能干等。

工具生命周期是一台小状态机，按小羽的实际语义定为四态：
    tool.pending   收到调用（可能还要等确认）
    tool.running   通过权限/确认关卡，即将真正执行
    tool.completed 执行完毕（工具级错误在 output 的 ERROR: 前缀里，ok=False）
    tool.denied    被 deny 规则或用户拒绝，没有执行
不变量：每个 pending 最终恰好收到一个终态（completed 或 denied）——
将来 TUI 活区的 spinner 靠它保证不悬空。
"""

from __future__ import annotations

import threading
import time

from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar, Literal, Protocol

NoticeLevel = Literal["info", "warn", "error"]


@dataclass(frozen=True, kw_only=True)
class UIEvent:
    """事件基类。kind 是判别字段（ClassVar，不进 asdict，由 to_dict 补上）。"""

    kind: ClassVar[str] = ""
    session_id: str = ""
    run_id: str = ""
    request_id: str = ""
    tool_call_id: str = ""
    task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典（跨进程时的线上形态）。"""
        fields = asdict(self)
        for key in ("session_id", "run_id", "request_id", "tool_call_id", "task_id"):
            # SDK events opt into correlation with a session identity. Keep
            # the existing CLI JSONL shape, even when the kernel supplies a
            # native tool-call ID for in-process SDK consumers.
            if not self.session_id or not fields[key]:
                del fields[key]
        return {"kind": self.kind, **fields}


@dataclass(frozen=True)
class RequestStarted(UIEvent):
    """一次模型调用已发出，正在等响应。

    补的是三段以前完全静默的等待：提交后等首个 token、每次工具执行完回到模型、
    重试之间。这段时间里 agent 阻塞在 HTTP 上，事件流里原本一个事件都没有，
    屏幕就停在用户刚敲的那行不动——用户无从判断它是在干活还是卡死了。

    不变量：每个 request.started 最终恰好收到一个 request.ended。前端的活区
    spinner 靠它保证不悬空。
    """

    kind: ClassVar[str] = "request.started"
    model: str
    #  这条路由的 provider 名（直连厂商名或网关名）。前端按模型名就够画等待指示，
    #  但 OpenTelemetry 导出要按约定写 gen_ai.provider.name，事件里得带着
    provider: str = ""


@dataclass(frozen=True)
class RequestEnded(UIEvent):
    """这次模型调用的响应流已消费完（或因异常/中断终止）。

    只是兜底的终态：正文或工具调用一出现，前端就该收掉等待指示了，不必等到这里。

    顺带捎上这一次调用的计时与用量——"慢"到底慢在排队（首 token 迟迟不来）
    还是慢在吐字（tok/s 低），只看一轮总耗时分不出来；按请求记才有分辨率。
    三个字段都有默认值：老消费方 `RequestEnded()` 照常构造，线上形态只是多几个键。
    - duration_ms：从发出请求到流消费完；
    - ttft_ms：首个 chunk 到达的延迟，一个 chunk 都没等到（异常/中断）为 None；
    - usage：这次调用的 prompt_tokens / completion_tokens / cached_tokens，
      上游没回 usage 时为空 dict（不编数）。
    """

    kind: ClassVar[str] = "request.ended"
    duration_ms: int = 0
    ttft_ms: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    #  响应侧的事实（OpenTelemetry 导出按约定逐项写进 span；前端可以无视）：
    #  - model：上游在响应里报的模型名，可能与请求的不同（网关改写、别名解析）；
    #  - response_id：上游响应编号，对账 / 向厂商报障用；
    #  - finish_reason：最后一个 chunk 报的收尾原因（stop / length / tool_calls …）；
    #  - error：这次调用没正常结束时的分类——errors.classify 的 kind
    #    （rate_limit / transient / …）或 interrupted；正常结束为空串。
    model: str = ""
    response_id: str = ""
    finish_reason: str = ""
    error: str = ""


@dataclass(frozen=True)
class TextDelta(UIEvent):
    """流式正文的一个分片（原样透传）。"""

    kind: ClassVar[str] = "text.delta"
    text: str


@dataclass(frozen=True)
class TextEnd(UIEvent):
    """一次流式回复的正文结束（仅在有正文时发出）。"""

    kind: ClassVar[str] = "text.end"


@dataclass(frozen=True)
class ToolPreparing(UIEvent):
    """参数仍在生成；仅供展示，不代表完整、合法或获准的调用。

    index 在当前 request.started/ended 区间内标识调用，所有预览在
    request.ended 时失效（含异常、中断、截断和重试）。不进入工具四态机。
    """

    kind: ClassVar[str] = "tool.preparing"
    name: str
    index: int
    argument_chars: int
    path: str = ""
    purpose: str = ""


@dataclass(frozen=True)
class ToolPending(UIEvent):
    """收到一次工具调用（参数已解析合法；可能还要等权限/确认）。"""

    kind: ClassVar[str] = "tool.pending"
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolPurpose(UIEvent):
    """模型自述的调用目的（仅在需要人工确认且模型提供了目的时发出）。"""

    kind: ClassVar[str] = "tool.purpose"
    name: str
    purpose: str


@dataclass(frozen=True)
class ToolRunning(UIEvent):
    """通过全部关卡，即将真正执行（活区 spinner 的起点）。"""

    kind: ClassVar[str] = "tool.running"
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ToolProgress(UIEvent):
    """运行中的工具报了一次进度（目前只有 MCP server 的 notifications/progress 会发）。

    不改变四态机：它只出现在 running 与终态之间，可以有零到多次，前端拿它刷新
    活区文案（"第 3/10 步 · 正在下载"），不打新行——进度是可覆盖的状态，不是
    事件流里值得各占一行的里程碑。progress/total 按规范都是数字，total 可能没有。
    """

    kind: ClassVar[str] = "tool.progress"
    name: str
    message: str = ""
    progress: float | None = None
    total: float | None = None


@dataclass(frozen=True)
class ToolCompleted(UIEvent):
    """执行完毕。ok=False 表示工具返回了 ERROR: 前缀（工具级错误照样是终态）。"""

    kind: ClassVar[str] = "tool.completed"
    name: str
    output: str
    ok: bool
    seconds: float


@dataclass(frozen=True)
class ToolDenied(UIEvent):
    """没有执行就被拦下：deny 规则（by="rule"）或用户在确认框拒绝（by="user"）。"""

    kind: ClassVar[str] = "tool.denied"
    name: str
    by: Literal["rule", "user"]


@dataclass(frozen=True)
class SteerAccepted(UIEvent):
    """一条运行中插话已进入本轮上下文。

    在 step 边界被消费时发出，不是在 steer() 入队时——事件表达的是
    "模型接下来真的会看到它"。TUI 借它打一行确认；headless 消费方
    可据此对齐"哪句插话生效于哪一步"。
    """

    kind: ClassVar[str] = "steer.accepted"
    text: str


@dataclass(frozen=True)
class PlanUpdated(UIEvent):
    """计划全量更新（plan 已通过校验：每项含 step 与合法 status）。"""

    kind: ClassVar[str] = "plan.updated"
    plan: list[dict[str, str]]
    explanation: str = ""


@dataclass(frozen=True)
class Notice(UIEvent):
    """一条独立提示（压缩/重试/降级等）。text 是完整成品文案。"""

    kind: ClassVar[str] = "notice"
    text: str
    level: NoticeLevel = "info"


class UISink(Protocol):
    """前端的唯一入口：消费事件流。实现方决定画在哪、怎么画、忽略哪些。"""

    def emit(self, event: UIEvent) -> None: ...


#  同一句告警多久之内不重复转述
REPEAT_WINDOW = 30.0


class ObservingSink:
    """子 agent 的旁观 sink：不转发正文和工具刷屏（N 个并发的刷屏毫无可读性），
    但也不是什么都丢。

    全丢的代价：重试、退避、换路由这些告警本来是为了"让人看得见在等什么、不会
    误判成假死"才打印的，批量运行时却一句都出不来——卡在限流退避上和正在干活，
    从外面看一模一样。这里只留两样：最近一次动作，和还没转述过的告警。

    emit 在子 agent 的工作线程里被调用，所以只记不发；转述由调用方在自己的线程
    里取走（drain）再发。
    """

    def __init__(self, label: str = "") -> None:
        self.label = label
        self._lock = threading.Lock()
        self._last_tool = ""
        self._last_at: float | None = None
        self._last_warning = ""
        self._queued: list[str] = []
        self._said: dict[str, float] = {}

    def emit(self, event: Any) -> None:
        kind = getattr(event, "kind", "")
        now = time.monotonic()
        with self._lock:
            if kind == "tool.running":
                self._last_tool = str(getattr(event, "name", ""))
                self._last_at = now
            elif kind == "notice" and getattr(event, "level", "info") in ("warn", "error"):
                text = str(getattr(event, "text", "")).strip()
                if not text:
                    return
                self._last_warning = text
                self._last_at = now
                if now - self._said.get(text, float("-inf")) >= REPEAT_WINDOW:
                    self._said[text] = now
                    self._queued.append(text)
            elif kind in ("request.started", "text.delta"):
                self._last_at = now

    def drain(self) -> list[str]:
        """还没转述过的告警（取完即清）。"""
        with self._lock:
            queued, self._queued = self._queued, []
        return queued

    def activity(self) -> str:
        """一句话：最近在干什么。还没有任何动静返回空串。"""
        with self._lock:
            tool, at, warning = self._last_tool, self._last_at, self._last_warning
        if at is None:
            return ""
        parts = [f"{time.monotonic() - at:.0f}s 前还有动静"]
        if tool:
            parts.append(f"最近调用 {tool}")
        if warning:
            parts.append(f"最近告警 {warning}")
        return "；".join(parts)
