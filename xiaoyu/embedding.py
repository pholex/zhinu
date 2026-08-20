"""供宿主进程把 xiaoyu 当库嵌入使用的官方入口（「库层嵌入 SDK 化」）。

这不是新架构——`Agent` 本来就是可以被反复 `send()`、可注入 `approver`/`sink`、
跨轮保留 `messages` 的库对象（`tests/test_embedding_smoke.py` 已验证）。这个模块
补的是最后一块：`Agent` 核心循环是同步的，宿主如果自己跑在 asyncio 事件循环里
（飞书 bot 这类），直接调用会话堵住整个循环。`AsyncAgent` 把它包一层，并提供
`interrupt()`——"先打断保住会话，不是杀进程"的语义。

用法（host 常驻嵌入模式，一个 Agent 实例跨多轮复用）：

    agent = Agent(config, approver=my_approver, sink=my_sink, session_log=...)
    async_agent = AsyncAgent(agent)

    result = await async_agent.send("用户第一句话")   # RunResult：正文/usage 增量/耗时/水位
    ...
    await async_agent.send("用户第二句话")   # 同一实例，上下文自动接上
    async_agent.interrupt()                  # 随时从事件循环里调用，非阻塞
    async_agent.recycle()                    # 清对话重开，对象不重建（reset 是别名）

要逐事件消费（画进度、转发心跳）用 stream()——不必再手糊 sink→Queue 桥：

    async for event in async_agent.stream("任务"):
        ...                                  # TextDelta / ToolRunning / …
        # 最后一个事件是 RunCompleted(result=RunResult)

resume（daemon 重启后续会话）：构造 Agent 时注入 SessionLog，重启后用
session_log.load_messages(path) 读出历史、async_agent.restore(messages,
source=str(path)) 接回——与 CLI `xiaoyu resume` 走同一条路径。常驻宿主建议
`SessionLog.create(model, workspace, directory=自己的目录)` 显式传目录，
把宿主会话与用户 CLI 手动会话彻底隔离（缺省则落在按工作区分区的公共目录）。

`Agent.approver` 本身是同步签名（`Callable[[str, dict], bool | str |
tuple[bool, str] | Allow | Deny]`，见 `agent.Approver`），执行时机在 `send()`
的工作线程里，不在事件循环线程。宿主如果
要在审批过程中等一个异步结果（飞书审批卡的点击，可能要等几十秒），直接把一个
`async def` 传给 `Agent(approver=...)` 是错的——那样传进去的是"没被 await 过的协程
对象"本身，会被误判成真值（永远批准）。正确写法是 `AsyncApprover`：把宿主的
async 审批函数桥接成一个真正同步、Agent 认识的 approver。

    async def approve(name: str, args: dict):
        card = await send_feishu_approval_card(name, args)
        return await wait_for_click(card)
        #  契约同 Approver：True/(True,附言) 批准；非空 str 或 (False,理由) 或
        #  Deny(理由) 拒绝并附理由；Allow(updated_args=…) 批准并改写参数
        #  （宿主"包沙箱再放行"的通道）——Allow/Deny 从本模块可直接 import

    loop = asyncio.get_running_loop()
    agent = Agent(config, approver=AsyncApprover(approve, loop, timeout=120), sink=my_sink)
    async_agent = AsyncAgent(agent)

`AsyncApprover.__call__` 内部用 `asyncio.run_coroutine_threadsafe(coro, loop).result()`
把协程扔回宿主的事件循环执行、阻塞的是工作线程本身，不是事件循环——宿主的其它
协程（心跳、别的会话）照常推进。超时或协程抛异常都按"拒绝并附理由"处理（fail
closed，不是 fail open）：安全闸门宁可多问一次，不能在故障时静默放行。

`ask_user` 工具（模型向用户提选择题）走同款注入通道：`Agent(asker=...)`（签名
见 `agent.Asker`：归一化问题列表 → `{问题: 答案}`，同样在工作线程被调用）。
不注入时工具不进 schemas，模型看不见——CLI 的 headless/wire 就是这么处理的。
宿主要把提问转成自家 UI（飞书卡片按钮等）且过程是异步的，桥接方式与
AsyncApprover 完全同构（run_coroutine_threadsafe + 超时按"没答"返回空 dict）。
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar

from .agent import Agent, Allow, Deny, Interrupted  # noqa: F401 - Allow/Deny 是嵌入宿主的常用面，从这里一并可得
from . import media
from .events import UIEvent


@dataclass(frozen=True)
class RunResult:
    """一轮 `send()` 的元数据（宿主记账/落日志的单轮汇总）。

    usage 是**本轮增量**（不是会话累计）：turns / prompt_tokens /
    completion_tokens / by_model（按 provider/model 全限定名分列，含子 agent
    与摘要调用——它们花的也是这一轮的钱）。宿主拿它对账、限额、写 run 日志。
    context_tokens 是跑完后的上下文水位，宿主据此决定何时 recycle()。
    """

    text: str                     # 本轮最后一条 assistant 正文（被打断时含已说出的半截）
    interrupted: bool             # 本轮是否被 interrupt() 收掉
    duration_seconds: float
    usage: dict[str, Any] = field(default_factory=dict)
    context_tokens: int = 0


@dataclass(frozen=True)
class RunCompleted(UIEvent):
    """`AsyncAgent.stream()` 的终结事件：永远是最后一个，携带本轮元数据。

    宿主的 `async for` 循环里据它收尾（记账、落日志、取交付文本）。
    """

    kind: ClassVar[str] = "run.completed"
    result: RunResult


class _QueueSink:
    """把事件从 `Agent.send()` 的工作线程搬进宿主事件循环上的 asyncio.Queue。

    `call_soon_threadsafe` 是这里唯一的跨线程原语；put_nowait 在无界队列上
    不会失败，事件顺序与产生顺序一致。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue) -> None:
        self._loop = loop
        self._queue = queue

    def emit(self, event: UIEvent) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)


#  stream() 的内部哨兵：工作线程收尾时入队，标记"事件到此为止"
_STREAM_DONE = object()


def measured_send(agent: Agent, user_input: str | list[dict[str, Any]]) -> RunResult:
    """跑一轮 `Agent.send()` 并结算本轮元数据（同步；异步宿主经 `AsyncAgent`）。

    `interrupt()` 触发的打断在这里被吸收成 `RunResult.interrupted=True`——
    那是宿主自己的主动动作，不该再以异常的形式弹回宿主；真正的错误
    （网络、配置、bug）照常抛出。
    """
    #  锁内快照：宸枢的 worker 线程可跨轮存活，裸迭代 by_model 会撞上
    #  另一头的 add()（dictionary changed size）
    before = agent.usage.snapshot()
    #  text 只在本轮新增的消息里取：本轮若以空回复收场（护栏路径），
    #  last_assistant_text() 会翻出上一轮的交付文本——把旧话当新交付发给
    #  用户是嵌入宿主最不该踩的坑
    start_index = len(agent.messages)
    started = time.monotonic()
    interrupted = False
    try:
        agent.send(user_input)
    except Interrupted:
        interrupted = True
    duration = time.monotonic() - started

    by_model: dict[str, dict[str, int]] = {}
    turns = prompt = completion = 0
    for model, (calls, prompt_toks, completion_toks) in agent.usage.snapshot().items():
        base = before.get(model, (0, 0, 0))
        delta = (
            calls - base[0],
            prompt_toks - base[1],
            completion_toks - base[2],
        )
        if any(delta):
            by_model[model] = {
                "calls": delta[0],
                "prompt_tokens": delta[1],
                "completion_tokens": delta[2],
            }
            turns += delta[0]
            prompt += delta[1]
            completion += delta[2]
    text = next(
        (
            media.text_of(message["content"]).strip()
            for message in reversed(agent.messages[start_index:])
            if message.get("role") == "assistant" and message.get("content")
        ),
        "",
    )
    return RunResult(
        text=text,
        interrupted=interrupted,
        duration_seconds=duration,
        usage={
            "turns": turns,
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "by_model": by_model,
        },
        context_tokens=agent.context_tokens(),
    )


class AsyncApprover:
    """把宿主的 async 审批函数桥接成 `Agent` 认识的同步 `Approver`。

    审批协程实际在 `loop` 所属的事件循环上运行（不是工作线程里另开一个循环）——
    宿主可以在协程里正常 await 任何依赖那个事件循环的东西（发消息、等回调）。
    `__call__` 本身跑在 `AsyncAgent.send()` 的工作线程里，阻塞它去等结果完全
    没问题，事件循环不受影响。
    """

    def __init__(
        self,
        coro_fn: Callable[[str, dict[str, Any]], Awaitable[Any]],
        loop: asyncio.AbstractEventLoop,
        timeout: float | None = None,
    ) -> None:
        self._coro_fn = coro_fn
        self._loop = loop
        self._timeout = timeout

    def __call__(self, name: str, args: dict[str, Any]) -> Any:
        #  误用护栏：如果这里正跑在 self._loop 自己的线程上（宿主没经 AsyncAgent
        #  而是在事件循环里直接同步调了 Agent.send()），下面的 .result() 会阻塞
        #  事件循环本身，审批协程永远得不到调度——timeout=None 时就是永久死锁，
        #  且现象（整个 daemon 卡住）离原因极远。fail closed 成一条指路的拒绝。
        try:
            running: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            return (
                "配置错误：AsyncApprover 在它自己事件循环的线程上被同步调用，"
                "这会死锁（审批协程永远得不到调度）。请经 AsyncAgent.send()/"
                "stream() 驱动 Agent，别在事件循环里直接调 Agent.send()。已自动拒绝"
            )
        future = asyncio.run_coroutine_threadsafe(self._coro_fn(name, args), self._loop)
        try:
            return future.result(timeout=self._timeout)
        except FutureTimeoutError:
            future.cancel()
            #  说清"不是用户否决 + 别原样重试"：无人值守下模型读不到这两句就会
            #  盲重试同一调用，每次白烧一个完整超时窗口（serve._HttpApprover 同款纪律）。
            return (
                f"审批超时（{self._timeout}s 内宿主未给出决定），已自动拒绝。"
                "注意：这不是用户的否决，是无人应答——不要原样重试同一调用"
                "（只会再超时一次）。请在回复里说明你需要哪个工具、想做什么，"
                "把决定留给宿主。"
            )
        except Exception as exc:  # noqa: BLE001 - 审批链路故障必须 fail closed，不能让异常掀翻整轮对话
            return f"审批过程出错：{type(exc).__name__}: {exc}，已自动拒绝"


class AsyncAgent:
    """把同步的 `Agent` 包成可在 asyncio 事件循环里安全驱动的对象。

    不复制/包装 `Agent` 的状态——`self.agent` 就是传入的那个实例，宿主随时可以
    直接访问它做同步的事（读 `messages`、查 `usage`、注册技能等）。这个类只加
    "怎么在事件循环里安全驱动它"这一层，不是另一套 API。
    """

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        #  上一轮的工作线程 task。提前退出的 stream() 会留下一个"还在收尾"的
        #  线程（要到下一个 chunk 边界才发现 interrupt），宿主视角的"顺序下一轮"
        #  实际可能与它重叠——重叠意味着两个线程并发写同一个 Agent.messages，
        #  以及 sink 恢复错乱。所以每轮入口先等上一轮真正结束。
        self._task: "asyncio.Task[RunResult] | None" = None
        #  提前退出的 stream() 留下的"sink 待物归原主"记录：(bridge, original)。
        #  由线程收尾的 done-callback 或下一轮入口的 _settle_previous 兑现，
        #  谁先到谁做（都跑在 loop 线程上，天然互斥）。
        self._pending_restore: tuple[Any, Any] | None = None

    async def _settle_previous(self) -> None:
        """等上一轮的工作线程真正收尾（若它还在跑，补一次 interrupt 催收）。

        上一轮的异常在这里吞掉——它属于上一轮，要么已经被消费过，要么消费方
        已提前离场（stream 的 done-callback 已取走异常），不该炸在新一轮的入口。
        收尾后兑现待恢复的 sink：不能只靠 done-callback——task.done() 置位
        与回调实际执行之间隔着一次 call_soon，新一轮若在那个缝里启动，会把
        旧 bridge 误当成要保存的 original。
        """
        task = self._task
        if task is not None and not task.done():
            self.agent.interrupt()
            try:
                await task
            except (Exception, asyncio.CancelledError):
                pass
        pending, self._pending_restore = self._pending_restore, None
        if pending is not None:
            bridge, original = pending
            if self.agent.sink is bridge:
                self.agent.sink = original

    async def send(self, user_input: str | list[dict[str, Any]]) -> RunResult:
        """在工作线程里跑 `Agent.send()`，不阻塞调用方的事件循环。

        返回本轮元数据 `RunResult`。`interrupt()` 触发的打断不作为异常抛出
        ——以 `result.interrupted=True` 报告（打断是宿主自己的主动动作）；
        真正的错误照常抛。
        """
        await self._settle_previous()
        task = asyncio.ensure_future(asyncio.to_thread(measured_send, self.agent, user_input))
        self._task = task
        return await task

    async def stream(
        self, user_input: str | list[dict[str, Any]]
    ) -> AsyncIterator[UIEvent]:
        """驱动一轮对话并逐个产出 UI 事件；最后一个事件是 `RunCompleted`。

        消费形态：

            async for event in async_agent.stream("任务"):
                match event:
                    case TextDelta(text=t): ...      # 正文分片
                    case ToolRunning(name=n): ...    # 工具开跑
                    case RunCompleted(result=r): ... # 收尾：元数据在这

        期间 `Agent` 的 sink 被临时替换成事件桥——事件只进本迭代器，不进
        构造 `Agent` 时传的 sink（需要双路时在消费循环里自己转发）。同一
        `Agent` 同时只能有一轮在跑（send/stream 都一样），不做并发保护。

        提前退出自动 `interrupt()`：工作线程在下一个 chunk 边界收尾、半截话
        入历史，不会留下继续烧钱的孤儿请求。但注意 **`break` 本身不会立刻
        触发**——异步生成器的 finally 要等 aclose，裸 break 后那要靠 GC
        终结器，时机不确定。要提前退出的宿主请用标准姿势显式关闭：

            async with contextlib.aclosing(async_agent.stream("任务")) as events:
                async for event in events:
                    if 不想看了:
                        break   # aclosing 保证退出即 interrupt

        提前退出后直接发起下一轮是安全的：send()/stream() 的入口会先等
        上一轮的工作线程真正收尾（interrupt 已发，通常在下一个 chunk 边界、
        毫秒级），再开新轮——sink 恢复与消息历史都不会与旧线程交错。
        """
        await self._settle_previous()
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        bridge = _QueueSink(loop, queue)
        original, self.agent.sink = self.agent.sink, bridge

        def run() -> RunResult:
            try:
                return measured_send(self.agent, user_input)
            finally:
                #  哨兵在 finally：正常收尾、抛异常两条路都必须让消费循环退出
                loop.call_soon_threadsafe(queue.put_nowait, _STREAM_DONE)

        send_task = asyncio.ensure_future(asyncio.to_thread(run))
        self._task = send_task
        try:
            while (event := await queue.get()) is not _STREAM_DONE:
                yield event
            #  非 Interrupted 的真错误（网络/配置/bug）在这里原样抛给宿主
            yield RunCompleted(result=await send_task)
        finally:
            if send_task.done():
                if self.agent.sink is bridge:
                    self.agent.sink = original
            else:
                #  消费方提前退出：打断生成；sink 等线程真收完再恢复——立刻换回去
                #  会让收尾期间的残余事件打到宿主原 sink 上（PlainSink 会直接打印）。
                #  恢复记录挂在 self._pending_restore 上：线程收尾的回调与下一轮
                #  入口的 _settle_previous 谁先到谁兑现。
                self.agent.interrupt()
                pending = (bridge, original)
                self._pending_restore = pending

                def _cleanup(task: "asyncio.Task[RunResult]") -> None:
                    if self._pending_restore is pending:
                        self._pending_restore = None
                        if self.agent.sink is bridge:
                            self.agent.sink = original
                    #  结果没人收——取走异常，别留 "never retrieved" 噪音
                    task.cancelled() or task.exception()

                send_task.add_done_callback(_cleanup)

    def interrupt(self) -> None:
        """请求打断当前正在进行的生成。线程安全、非阻塞，随时可调用。"""
        self.agent.interrupt()

    def steer(self, text: str) -> None:
        """向正在进行的一轮追加用户输入（线程安全、非阻塞）。

        与 interrupt() 相反的语义：不打断，只是让模型在下一个 step 边界
        看到这句话再继续。典型场景：宿主转发用户在 bot 会话里补发的消息。
        本轮已结束时入队的内容会在下一次 send() 开头被丢弃——宿主若要保留，
        用 `agent.drain_steers()` 自行回收拼进下一轮输入。
        """
        self.agent.steer(text)

    def notify(self, text: str, key: str = "") -> None:
        """投递一条系统通知（线程安全、非阻塞），搭下一条工具结果送达模型。

        与 steer() 的分工：steer 是"用户说的话"（以 user 消息入历史），notify
        是"系统事件"（宿主的后台任务完成、外部状态变化）——以 <system-reminder>
        附在下一条工具结果尾部，不冒充用户发言。同一 key 整个会话只送达一次，
        宿主可放心重复投递幂等事件。空闲时入队的通知在下一轮送达，不会被丢弃。
        """
        self.agent.notify(text, key=key)

    def recycle(self) -> None:
        """清空对话重开（对象/registry/config 都不重建）——"关会话重开"语义。
        名字取自常驻宿主侧的惯用词：会话回收复用，不是把对象重建一遍。"""
        self.agent.reset()

    def reset(self) -> None:
        """`recycle()` 的同义别名（保留早期嵌入代码用的名字）。"""
        self.recycle()

    def restore(self, messages: list[Any], source: str = "") -> None:
        """resume：把 `session_log.load_messages()` 读出的历史接回会话。
        透传 `Agent.restore()`——放在这里是让异步宿主在一个对象上找全动词。"""
        self.agent.restore(messages, source=source)
