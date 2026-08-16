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

from dataclasses import asdict, dataclass
from typing import Any, ClassVar, Literal, Protocol

NoticeLevel = Literal["info", "warn", "error"]


@dataclass(frozen=True)
class UIEvent:
    """事件基类。kind 是判别字段（ClassVar，不进 asdict，由 to_dict 补上）。"""

    kind: ClassVar[str] = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典（跨进程时的线上形态）。"""
        return {"kind": self.kind, **asdict(self)}


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


@dataclass(frozen=True)
class RequestEnded(UIEvent):
    """这次模型调用的响应流已消费完（或因异常/中断终止）。

    只是兜底的终态：正文或工具调用一出现，前端就该收掉等待指示了，不必等到这里。
    """

    kind: ClassVar[str] = "request.ended"


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
