"""serve 模式：HTTP API server —— 给 n8n / Dify 这类工作流编排器用的外挂层。

`xiaoyu serve` 起一个 HTTP 服务，把库层的 `AsyncAgent` 暴露成 REST + SSE。
定位与 `--acp` / `--wire` 平行：都是"把内核接给外部驱动方"的协议面，区别在
驱动方是谁——acp 是编辑器（stdio，一对一），wire 是自家外壳与测试驱动器，
serve 是**跑在别处的编排器**（HTTP，一对多、可跨机、可轮询）。

范围纪律（重要）：**这一层不改内核架构。** 小羽的 TUI/CLI 仍然是进程内直驱
`Agent`，不经 HTTP；serve 只是外挂一个适配面，不是把小羽改造成 client/server
产品。内核该是什么形态的判断不由这个模块推翻。

资源模型照编排场景的真实需要定（session 是资源、不是一次性 job）：

    POST   /session                       新建会话 → {session_id, ...}
    GET    /session                       列出会话
    GET    /session/{id}                  会话详情（状态/模型/用量水位）
    DELETE /session/{id}                  关掉会话（先打断再摘除）
    POST   /session/{id}/prompt           跑一轮，**等它跑完**再返回
    POST   /session/{id}/prompt_async     跑一轮，**立刻 202 返回**，进度靠事件/状态
    GET    /session/{id}/status           轮询状态机（编排器的主要抓手）
    GET    /session/{id}/events           拉事件（支持 from 游标 + long-poll）
    GET    /session/{id}/events/stream    同一份事件流的 SSE 形态
    GET    /session/{id}/permissions      当前挂起的审批请求
    POST   /session/{id}/permissions      回一个审批决定（放行/拒绝/改写参数）
    POST   /session/{id}/abort            打断当前这一轮（不杀会话）
    POST   /session/{id}/steer            向进行中的一轮插话
    POST   /mcp                           同一会话层的 MCP server 面（见 serve_mcp.py）

为什么同步与异步两个 prompt 端点都要有：coding agent 一轮动辄几分钟，编排器
的 HTTP 节点普遍有超时上限（n8n/Dify 默认都在分钟级），同步端点在短任务上
最省事、长任务上必然超时。异步端点把"提交"和"取结果"拆开，超时不再是问题。

**状态机是这一层的核心表达力**，不是装饰：

    status=running  detail=working               正在干活
    status=running  detail=waiting_for_approval  卡在等审批 ← 必须能与 working 区分
    status=idle     detail=finished              上一轮正常收尾
    status=idle     detail=interrupted           上一轮被 abort 收掉
    status=error    detail=failed                上一轮抛异常（error 字段有原文）

`waiting_for_approval` 单独占一格是因为：编排器只看"还在跑"的话，无法区分
"模型在想"和"它在等人点确认"——前者该继续等，后者该去回 `/permissions`
或者判超时。把这个区分藏起来，编排侧的超时逻辑就没法写对。

审批通道的线程模型：`Agent.approver` 在 `AsyncAgent` 的工作线程里被调用，
本模块的 `_HttpApprover` 就地阻塞在 `threading.Event` 上等 HTTP 回决定——
阻塞的是工作线程，事件循环照常处理请求（包括那条来放行的请求）。这里刻意
不用 `embedding.AsyncApprover`：那是给"审批逻辑本身是协程"的宿主用的，而
这里的兑现动作只是 `Event.set()`，绕一趟事件循环反而多一层可死锁的耦合。
超时按**拒绝**处理（fail closed，与 AsyncApprover 同一纪律）。

安全边界（这个服务会执行任意命令，边界必须是硬的）：
- 默认只绑 127.0.0.1。绑到非回环地址时**必须**给 token，否则拒绝启动。
- `--workspace` 是 root：session 只能落在它或它的子目录里，越界 400。
- folder trust 按信任表非交互判定（headless 纪律，与 acp 一致）：没信任
  记录的目录不吃工作区级 .mcp.json/permissions/.env，也绝不在协议通道上发问。

依赖：fastapi + uvicorn 是可选额外（`pip install "xiaoyu-agent[serve]"`），
缺包时 `xiaoyu serve` 报一句怎么装就退出，不影响其它任何功能。选 fastapi 而
不是手糊 http.server，图的是 **OpenAPI schema 由代码自动生成**——Dify 的
自定义工具吃的就是 OpenAPI，schema 手写会随代码漂移，生成的不会
（`xiaoyu serve --print-openapi` 直接导出给 Dify 贴）。
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

#  Request 必须在**模块全局**里解析得到，不能只在 create_app 内部 import：
#  本文件有 `from __future__ import annotations`，路由函数的注解是字符串，
#  FastAPI 用函数的 __globals__ 去解析它们——函数局部的 import 在那里看不见，
#  `request: Request` 会被当成一个名叫 request 的**查询参数**，SSE 端点直接 422。
#  其余 fastapi 名字（Body/Query/Header/Depends）都只作默认值用，不受影响。
try:
    from starlette.requests import Request
except ImportError:  # pragma: no cover - 没装 [serve] 时 create_app 会先抛 ServeUnavailable
    Request = Any  # type: ignore[assignment,misc]

from . import folder_trust
from .agent import Agent, Allow, Deny
from .config import Config, MissingConfig
from .embedding import AsyncAgent, RunCompleted
from .events import UIEvent
from .permissions import Permissions
from .session_log import SessionLog
from .tools import Toolbox

#  事件环形缓冲的默认容量：一轮重活的事件数在百量级，5000 够存几十轮；
#  超出后从头丢，并把 first_seq/dropped 如实报给客户端——静默截断会让
#  编排侧以为自己拉全了。
DEFAULT_BUFFER = 5000
#  单条事件里超长字符串字段的上限。工具输出（尤其 bash / read）可以是 MB 级，
#  常驻服务把它们全留在内存里迟早撑爆。截断处标记 *_truncated 与原始长度，
#  客户端看得见自己拿到的是截断版。
DEFAULT_MAX_FIELD = 20000
#  审批默认等多久。编排器那头通常有人（或有自动规则）在回，5 分钟足够；
#  超时不是"继续等"而是拒绝，见模块 docstring 的 fail closed 纪律。
DEFAULT_APPROVAL_TIMEOUT = 300.0
#  long-poll 的上限：编排器一次 HTTP 最多挂这么久就返回（哪怕没有新事件），
#  免得撞上它自己的连接超时而显示成错误。
MAX_WAIT = 60.0
#  SSE 泵的空转切片：每片醒一次去查客户端还在不在。切得太长，断开的连接要拖
#  一整片才被回收；太短就是白转 CPU。1s 是这两头之间的常规折中。
POLL_SLICE = 1.0
#  多久没字节可发就打一条心跳注释（nginx 默认 60s 掐读超时，取它的一半）。
HEARTBEAT = 30.0
#  同时能跑的会话数（= 工作线程池大小，见 create_app 的 lifespan）。默认线程池
#  是 min(32, cpu+4)，而审批期间线程被占着，那个隐形上限会让超出的会话静默饿死。
DEFAULT_MAX_SESSIONS = 32


class ServeUnavailable(RuntimeError):
    """fastapi/uvicorn 没装。CLI 捕获后打安装提示，不抛 traceback。"""


@dataclass
class ServeConfig:
    """启动期参数（CLI 装配好传进来，本模块不读 argv）。"""

    root: Path
    host: str = "127.0.0.1"
    port: int = 8420
    token: str = ""
    model: str = ""
    base_url: str = ""
    mode: str = ""
    approval: str = "ask"  # ask | allow_all
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT
    sandbox: bool | None = None
    sandbox_network: bool | None = None
    append_system_prompt: str = ""
    buffer_limit: int = DEFAULT_BUFFER
    max_field: int = DEFAULT_MAX_FIELD
    #  能同时跑的会话数 = 工作线程池大小。审批期间线程是被占着的（approver 就地
    #  阻塞），所以这个数同时也是"能同时挂起等审批的会话数"上限。
    max_sessions: int = DEFAULT_MAX_SESSIONS
    #  要不要挂 /mcp（MCP server 面，见 serve_mcp.py）。与 REST 共享会话层，
    #  默认开着；`--no-mcp` 关掉后 serve 退回纯 REST。
    mcp: bool = True


@dataclass
class _Pending:
    """一条挂起的审批请求。`done` 由 HTTP 侧 set，工作线程在它上面阻塞。"""

    id: str
    name: str
    args: dict[str, Any]
    created_at: float
    done: threading.Event = field(default_factory=threading.Event)
    verdict: Any = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.id,
            "tool": self.name,
            "args": self.args,
            "created_at": self.created_at,
            "waiting_seconds": round(time.time() - self.created_at, 3),
        }


def _clip(value: Any, limit: int) -> tuple[Any, bool, int]:
    """超长字符串截断。返回 (值, 是否截断, 原始长度)。"""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit], True, len(value)
    return value, False, 0


def _clip_deep(value: Any, limit: int) -> Any:
    """递归截断嵌套结构里的超长字符串。

    只截顶层是不够的：`tool.pending` / `tool.running` / `permission.requested`
    最大的负载在 **`args` 这个 dict 里**（`write_file` 的 content 可以是 MB
    级），只看顶层等于把最该防的那一块原样留在了内存里。截断处就地换成
    带 `…（已截断，共 N 字符）` 的字符串——嵌套结构里没法像顶层那样另起
    `*_truncated` 兄弟字段，把事实写进值本身，消费方一眼看得见。
    """
    if isinstance(value, str):
        if len(value) > limit:
            return f"{value[:limit]}…（已截断，共 {len(value)} 字符）"
        return value
    if isinstance(value, dict):
        return {key: _clip_deep(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip_deep(item, limit) for item in value]
    return value


class _SessionRef:
    """先于 `Agent` 创建、随后 `bind()` 的间接层。**这不是花活，是必须的。**

    `Agent.__init__` 把 `approver` 与 `sink` **按值捕获**进 explore /
    web_search / 子 agent 的工具闭包（`agent.py:563/570/733`
    → `agents.py:450`）。所以构造完再回填 `agent.approver` / `agent.sink`
    **到不了那些闭包**——回填只改了父 Agent 的属性，闭包里还攥着构造时那个值。
    两个后果都是实打实的：

    - 回填 approver：子 agent 拿到的是 `Agent.__init__` 的缺省
      `lambda name, args: True`，于是 `--approval ask` 档下**子 agent 里的
      bash/write_file 完全不经审批**，也不出现在 /permissions 和事件流里。
      审批闸门在这条路上等于没有。
    - 回填 sink：explore / web_search / 子 agent 的事件全进黑洞 sink，
      编排侧看到的是"两分钟一个事件都没有"，与卡死无法区分。

    解法就是让这两样**在构造时就是最终对象**：先造这个 ref，把
    `_HttpApprover(ref)` / `_BridgeSink(ref)` 传进 `Agent(...)`，`_Session`
    造好后再 `bind()`。绑定前不可能有任何一轮在跑，所以这个窗口是安全的。
    （`AsyncAgent.stream()` 另外会把 `agent.sink` 临时换成自己的事件桥，那管
    的是主循环；被闭包攥住的是这里的 `_BridgeSink`，两条路都通向同一个缓冲区。）
    """

    def __init__(self) -> None:
        self.session: "_Session | None" = None
        self.loop: asyncio.AbstractEventLoop | None = None

    def bind(self, session: "_Session", loop: asyncio.AbstractEventLoop) -> None:
        self.session = session
        self.loop = loop


class _BridgeSink:
    """工具闭包捕获的 sink：把事件搬进会话缓冲区。

    被 explore / web_search / 子 agent 在**工作线程**里调用，所以必须经
    `call_soon_threadsafe` 转交，不能直接动缓冲区。
    """

    def __init__(self, ref: _SessionRef) -> None:
        self._ref = ref

    def emit(self, event: UIEvent) -> None:
        session, loop = self._ref.session, self._ref.loop
        if session is None or loop is None:
            return  # 绑定前不可能有事件；真有也只能丢，不能崩在工具里
        loop.call_soon_threadsafe(session.publish_event, event)


class _Session:
    """一个会话：AsyncAgent + 状态机 + 事件缓冲 + 挂起的审批。

    事件缓冲与状态只在事件循环线程上改（工作线程来的一律经
    `call_soon_threadsafe` 转交），所以那部分不需要锁。

    `pending` 是唯一的例外：审批的登记/摘除发生在**工作线程**（approver 就地
    阻塞在那儿），而 /status、/permissions、abort、`_drive` 收尾都在事件循环上
    遍历它。裸 dict 会在"一边 pop 一边迭代"时抛
    `RuntimeError: dictionary changed size during iteration`——打在 /status
    这个编排器每几百毫秒就要打一次的端点上。所以它单独配一把锁，读一律走
    `snapshot_pending()` 取快照。
    """

    def __init__(self, session_id: str, agent: Agent, workspace: Path, cfg: ServeConfig) -> None:
        self.id = session_id
        self.agent = agent
        self.async_agent = AsyncAgent(agent)
        self.workspace = workspace
        self.cfg = cfg
        self.created_at = time.time()
        self.updated_at = self.created_at

        self.status = "idle"
        self.detail = "idle"
        self.error = ""
        self.busy = False
        self.last_result: dict[str, Any] | None = None
        self.turns = 0

        self.events: list[dict[str, Any]] = []
        self.next_seq = 1
        self.first_seq = 1
        self.dropped = 0
        self._tick = asyncio.Event()

        self.pending: dict[str, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._task: asyncio.Task | None = None

    # ---------- 挂起的审批（跨线程，一律走这三个口） ----------

    def add_pending(self, request: _Pending) -> None:
        with self._pending_lock:
            self.pending[request.id] = request

    def drop_pending(self, request_id: str) -> None:
        with self._pending_lock:
            self.pending.pop(request_id, None)

    def snapshot_pending(self) -> list[_Pending]:
        with self._pending_lock:
            return list(self.pending.values())

    def take_pending(self, request_id: str) -> "_Pending | None":
        with self._pending_lock:
            return self.pending.get(request_id)

    # ---------- 事件缓冲 ----------

    def _append(self, payload: dict[str, Any]) -> None:
        limit = self.cfg.max_field
        clipped: dict[str, Any] = {}
        for key, value in payload.items():
            #  顶层字符串额外挂 *_truncated / *_chars 兄弟字段（消费方好判断）；
            #  嵌套结构走 _clip_deep，把截断事实写进值本身
            value, cut, original = _clip(value, limit)
            clipped[key] = value if cut else _clip_deep(value, limit)
            if cut:
                clipped[f"{key}_truncated"] = True
                clipped[f"{key}_chars"] = original
        record = {"seq": self.next_seq, "time": time.time(), **clipped}
        self.next_seq += 1
        self.events.append(record)
        if len(self.events) > self.cfg.buffer_limit:
            overflow = len(self.events) - self.cfg.buffer_limit
            del self.events[:overflow]
            self.dropped += overflow
            self.first_seq = self.events[0]["seq"]
        self.updated_at = record["time"]
        #  广播：把当前这只 Event 换掉再 set，等待者各自持有旧的那只，
        #  不会漏掉"在 await 之间发生的" publish。
        tick, self._tick = self._tick, asyncio.Event()
        tick.set()

    def publish(self, kind: str, **fields: Any) -> None:
        self._append({"kind": kind, **fields})

    def publish_event(self, event: UIEvent) -> None:
        self._append(event.to_dict())

    def since(self, from_seq: int, limit: int) -> list[dict[str, Any]]:
        return [item for item in self.events if item["seq"] >= from_seq][:limit]

    async def wait_new(self, from_seq: int, timeout: float) -> None:
        """等到 seq >= from_seq 的事件出现，或超时（long-poll 用）。"""
        deadline = time.monotonic() + timeout
        while self.next_seq <= from_seq:
            remain = deadline - time.monotonic()
            if remain <= 0:
                return
            tick = self._tick
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(tick.wait(), remain)

    # ---------- 状态 ----------

    def status_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "status": self.status,
            "detail": self.detail,
            "busy": self.busy,
            "error": self.error,
            "turns": self.turns,
            "pending_approvals": [item.to_dict() for item in self.snapshot_pending()],
            "next_seq": self.next_seq,
            "first_seq": self.first_seq,
            "dropped_events": self.dropped,
            "last_result": self.last_result,
            "updated_at": self.updated_at,
        }

    def info_dict(self) -> dict[str, Any]:
        return {
            **self.status_dict(),
            "workspace": str(self.workspace),
            "model": self.agent.config.model,
            "mode": getattr(self.agent.config, "mode", ""),
            "context_tokens": self.agent.context_tokens(),
            "created_at": self.created_at,
        }

    # ---------- 审批（工作线程 → 事件循环） ----------

    def mark_waiting(self, request: _Pending) -> None:
        self.detail = "waiting_for_approval"
        self.publish("permission.requested", **request.to_dict())

    def mark_resolved(self, request_id: str, decision: str) -> None:
        if self.busy:
            self.detail = "working"
        self.publish("permission.resolved", request_id=request_id, decision=decision)


class _HttpApprover:
    """`Agent.approver`：在工作线程里阻塞等 HTTP 侧的决定。

    `allow_all` 档直接放行（等价 --yolo，必须由启动方显式选）。
    超时 / 会话被关都按拒绝收场——安全闸门宁可多拦一次，不能在故障时静默放行。
    """

    def __init__(self, ref: _SessionRef) -> None:
        self._ref = ref

    def __call__(self, name: str, args: dict[str, Any]) -> Any:
        session, loop = self._ref.session, self._ref.loop
        if session is None or loop is None:
            #  绑定前不可能有工具在跑；真跑到了也必须 fail closed，
            #  绝不能因为"还没绑好"就放行
            return Deny("会话尚未就绪，已自动拒绝")
        if session.cfg.approval == "allow_all":
            return True
        request = _Pending(id=uuid.uuid4().hex[:12], name=name, args=args, created_at=time.time())
        #  先落进 pending 再通知：GET /permissions 与事件谁先到都能看到这一条
        session.add_pending(request)
        loop.call_soon_threadsafe(session.mark_waiting, request)
        got = request.done.wait(timeout=session.cfg.approval_timeout)
        session.drop_pending(request.id)
        if not got:
            loop.call_soon_threadsafe(session.mark_resolved, request.id, "timeout")
            return Deny(
                f"审批超时（{session.cfg.approval_timeout:g}s 内没有收到 "
                f"POST /session/{session.id}/permissions 的决定），已自动拒绝"
            )
        loop.call_soon_threadsafe(session.mark_resolved, request.id, "resolved")
        return request.verdict


def build_agent(session_id: str, workspace: Path, cfg: ServeConfig, ref: _SessionRef) -> Agent:
    """现配一个 Agent（装配顺序照 acp_main 的工厂，两条协议面保持同源）。

    approver 与 sink **必须在这里就是最终对象**（经 `_SessionRef` 间接绑定），
    不能构造完再回填——`Agent.__init__` 会把这两样按值捕获进 explore /
    web_search / 子 agent 的工具闭包，回填到不了那里。理由与后果见
    `_SessionRef` 的 docstring；`cli.py` 与 `acp.py` 的工厂也都是构造时注入。
    """
    trusted = folder_trust.evaluate(workspace, interactive=False).verdict == "trusted"
    config = Config.from_env(
        workspace=workspace,
        model=cfg.model or None,
        base_url=cfg.base_url or None,
        auto_approve=True if cfg.approval == "allow_all" else None,
        mode=cfg.mode or None,
        sandbox=cfg.sandbox,
        sandbox_network=cfg.sandbox_network,
        append_system_prompt=cfg.append_system_prompt or None,
        workspace_trusted=trusted,
    )
    permissions = Permissions.load(config.workspace, include_workspace=trusted)
    session_log = SessionLog.create(config.model, str(config.workspace), session_id=session_id)
    return Agent(
        config,
        Toolbox(config),
        approver=_HttpApprover(ref),
        session_log=session_log,
        permissions=permissions,
        #  主循环的事件由 AsyncAgent.stream() 的临时事件桥接走；这个 sink 是给
        #  被闭包捕获的那批（explore / web_search / 子 agent）用的，同样进缓冲区。
        #  不能给 NullSink——那批工具的事件会整段消失，编排侧看不出它在干活。
        sink=_BridgeSink(ref),
    )


def create_app(cfg: ServeConfig):  # noqa: C901 - 路由表天然长，拆开反而更难读
    """构造 FastAPI app。抽成函数是为了让测试与 --print-openapi 不必起服务。"""
    try:
        from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
        from fastapi.responses import StreamingResponse
    except ImportError as exc:  # pragma: no cover - 缺包路径在 CLI 侧有提示
        raise ServeUnavailable(str(exc)) from exc

    sessions: dict[str, _Session] = {}

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        """把 `asyncio.to_thread` 的默认线程池换成按并发会话数定的那个。

        `AsyncAgent.send/stream` 经 `to_thread` 跑一轮，用的是默认
        `ThreadPoolExecutor`（`min(32, cpu+4)`，常见机器上就 12 条）。而
        `_HttpApprover` **会占着工作线程**等审批（默认 300s）。于是十来个会话
        卡在 waiting_for_approval，第 13 个 `prompt_async` 照样回 202、
        `/status` 照样报 running/working，但 `_drive` 根本没开始跑——没有事件、
        没有进度，与"模型卡死"完全无法区分，最长闷五分钟。
        一个"一对多"的服务不能有这种隐形天花板，所以显式按 max_sessions 配。
        """
        from concurrent.futures import ThreadPoolExecutor

        pool = ThreadPoolExecutor(
            max_workers=cfg.max_sessions, thread_name_prefix="xiaoyu-serve"
        )
        asyncio.get_running_loop().set_default_executor(pool)
        try:
            yield
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

    app = FastAPI(
        lifespan=lifespan,
        title="xiaoyu HTTP API",
        version=__import__("xiaoyu").__version__,
        description=(
            "把小羽（coding agent）接给工作流编排器（n8n / Dify / 自研调度）的 HTTP 面。"
            "长任务用 prompt_async + status 轮询；需要人工放行的工具调用会让 status 停在 "
            "running/waiting_for_approval，回 POST /session/{id}/permissions 放行。"
            #  MCP 面只在描述里提、不进下方路由表（include_in_schema=False）：
            #  这份 schema 会被 Dify/n8n 整份导入当 REST 工具清单，但 /docs 的
            #  读者是人——完全不提，人会以为这个服务只有 REST 一张脸。
            + (
                "\n\n本服务同时在 `POST /mcp` 挂着 **MCP server 面**（agent 级三工具 "
                "xiaoyu / xiaoyu_reply / xiaoyu_close，streamable HTTP；LangChain / LangGraph "
                "经 langchain-mcp-adapters 即插即用，`--no-mcp` 可关）。它是 JSON-RPC 端点，"
                "刻意不列进下方路由表——这份 schema 是给 Dify / n8n 导入的 REST 工具清单。"
                "详见 docs/mcp-server.md。"
                if cfg.mcp
                else ""
            )
        ),
    )

    # ---------- 鉴权 ----------

    async def require_token(
        authorization: str = Header(default=""),
        x_xiaoyu_token: str = Header(default=""),
    ) -> None:
        if not cfg.token:
            return
        offered = x_xiaoyu_token or authorization.removeprefix("Bearer ").strip()
        #  常数时间比较：token 是长期凭据，别把它的前缀通过响应时间漏出去。
        #  **必须比 bytes**：starlette 按 latin-1 解 header，而
        #  compare_digest(str, str) 遇非 ASCII 直接抛 TypeError——那会让
        #  非 ASCII 的 token 整个服务不可用（每个请求 500），也让任何带非 ASCII
        #  Authorization 头的请求收到 500 而不是干净的 401。
        if not hmac.compare_digest(offered.encode("utf-8"), cfg.token.encode("utf-8")):
            raise HTTPException(status_code=401, detail="token 不对或缺失")

    guard = [Depends(require_token)]

    # ---------- 工具 ----------

    def pick(session_id: str) -> _Session:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"未知 session_id {session_id!r}")
        return session

    def resolve_workspace(raw: str) -> Path:
        """把请求里的 workspace 归一并锁进 root 之内。"""
        if not raw:
            return cfg.root
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = cfg.root / candidate
        candidate = candidate.resolve()
        if candidate != cfg.root and cfg.root not in candidate.parents:
            raise HTTPException(
                status_code=400,
                detail=f"workspace 必须在 root（{cfg.root}）之内，收到 {candidate}",
            )
        if not candidate.is_dir():
            raise HTTPException(status_code=400, detail=f"工作区不存在：{candidate}")
        return candidate

    def make_session(workspace: str = "", model: str = "", mode: str = "") -> _Session:
        """装配一个会话并登记进注册表。REST 的 POST /session 与 MCP 的 xiaoyu
        工具共用这一个装配口——两张脸建出的会话必须一模一样，包括 approver 与
        sink 的构造期注入（见 build_agent 的 docstring）。"""
        target = resolve_workspace(workspace)
        session_id = f"sess-{uuid.uuid4().hex[:16]}"
        local = replace(cfg, model=model or cfg.model, mode=mode or cfg.mode)
        ref = _SessionRef()
        try:
            agent = build_agent(session_id, target, local, ref)
        except MissingConfig as exc:
            #  503 而不是 500：这不是 bug，是服务端没配 provider，运维动作明确
            raise HTTPException(status_code=503, detail=f"未配置 provider：{exc}") from exc
        session = _Session(session_id, agent, target, local)
        #  绑定后 approver / sink 才真正生效。绑定前不可能有一轮在跑（会话还没
        #  返回给调用方），所以这个窗口安全——但 approver 仍 fail closed 兜底
        ref.bind(session, asyncio.get_running_loop())
        sessions[session_id] = session
        return session

    def close_session(session: _Session) -> None:
        """摘除一个会话：先打断，再把挂着的审批放掉（否则工作线程堵到
        approval_timeout），最后出注册表。REST DELETE 与 MCP xiaoyu_close 共用。"""
        session.async_agent.interrupt()
        for request in session.snapshot_pending():
            request.verdict = Deny("会话已被关闭")
            request.done.set()
        sessions.pop(session.id, None)

    async def start_turn(session: _Session, text: str) -> asyncio.Task:
        """开一轮。已经在跑就 409——同步架构下同一 Agent 一次只能跑一轮，
        默默排队会让编排器以为自己的第二次提交立刻生效了。"""
        if session.busy:
            raise HTTPException(status_code=409, detail="该会话正在跑一轮，先等它结束或 /abort")
        session.busy = True
        session.status = "running"
        session.detail = "working"
        session.error = ""
        session.publish("run.started", prompt=text)
        task = asyncio.create_task(_drive(session, text))
        session._task = task
        return task

    async def _drive(session: _Session, text: str) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        try:
            async for event in session.async_agent.stream(text):
                if isinstance(event, RunCompleted):
                    result = asdict(event.result)
                    session.last_result = result
                session.publish_event(event)
        except Exception as exc:  # noqa: BLE001 - 编排方要结构化错误，不要 traceback
            session.status = "error"
            session.detail = "failed"
            session.error = f"{type(exc).__name__}: {exc}"
            session.publish("run.failed", error=session.error)
            return {"error": session.error}
        else:
            session.status = "idle"
            session.detail = "interrupted" if (result or {}).get("interrupted") else "finished"
            session.turns += 1
            return result or {}
        finally:
            session.busy = False
            #  会话若在跑的过程中被 DELETE，挂起的审批要有人收尸，否则工作线程
            #  一直阻塞到 approval_timeout。这里统一兜底放拒绝。
            for request in session.snapshot_pending():
                request.verdict = Deny("会话已结束")
                request.done.set()

    # ---------- 路由 ----------

    @app.get("/health", summary="存活探针", tags=["system"], operation_id="health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "version": app.version,
            "root": str(cfg.root),
            "sessions": len(sessions),
            "approval": cfg.approval,
        }

    @app.post(
        "/session",
        summary="新建会话",
        tags=["session"],
        dependencies=guard,
        operation_id="create_session",
    )
    async def session_create(
        workspace: str = Body(default="", embed=True, description="工作区路径，须在 root 之内；缺省用 root"),
        model: str = Body(default="", embed=True, description="模型名，缺省跟随服务端配置"),
        mode: str = Body(default="", embed=True, description="default / auto / plan"),
    ) -> dict[str, Any]:
        return make_session(workspace=workspace, model=model, mode=mode).info_dict()

    @app.get(
        "/session",
        summary="列出会话",
        tags=["session"],
        dependencies=guard,
        operation_id="list_sessions",
    )
    async def session_list() -> dict[str, Any]:
        return {"sessions": [item.info_dict() for item in sessions.values()]}

    @app.get(
        "/session/{session_id}",
        summary="会话详情",
        tags=["session"],
        dependencies=guard,
        operation_id="get_session",
    )
    async def session_get(session_id: str) -> dict[str, Any]:
        return pick(session_id).info_dict()

    @app.delete(
        "/session/{session_id}",
        summary="关闭会话",
        tags=["session"],
        dependencies=guard,
        operation_id="close_session",
    )
    async def session_delete(session_id: str) -> dict[str, Any]:
        close_session(pick(session_id))
        return {"session_id": session_id, "closed": True}

    @app.post(
        "/session/{session_id}/prompt",
        summary="跑一轮（同步等结果）",
        operation_id="prompt",
        description="适合几十秒内能跑完的任务；长任务用 prompt_async，否则会撞上调用方的 HTTP 超时。",
        tags=["run"],
        dependencies=guard,
    )
    async def session_prompt(
        session_id: str,
        text: str = Body(embed=True, description="给模型的指令"),
    ) -> dict[str, Any]:
        session = pick(session_id)
        task = await start_turn(session, text)
        result = await task
        return {"session_id": session_id, **session.status_dict(), "result": result}

    @app.post(
        "/session/{session_id}/prompt_async",
        summary="跑一轮（立刻返回）",
        operation_id="prompt_async",
        description="接受后立刻返回 202；用 GET /status 轮询，或 GET /events 拉进度。",
        status_code=202,
        tags=["run"],
        dependencies=guard,
    )
    async def session_prompt_async(
        session_id: str,
        text: str = Body(embed=True, description="给模型的指令"),
    ) -> dict[str, Any]:
        session = pick(session_id)
        await start_turn(session, text)
        return {"session_id": session_id, "accepted": True, **session.status_dict()}

    @app.get(
        "/session/{session_id}/status",
        summary="轮询状态",
        operation_id="get_status",
        description=(
            "status: idle / running / error；detail: working / waiting_for_approval / "
            "finished / interrupted / failed。waiting_for_approval 表示卡在等人放行，"
            "不是在算——编排侧据此决定去回 /permissions 还是继续等。"
        ),
        tags=["run"],
        dependencies=guard,
    )
    async def session_status(session_id: str) -> dict[str, Any]:
        return pick(session_id).status_dict()

    @app.get(
        "/session/{session_id}/events",
        summary="拉事件（游标 + long-poll）",
        operation_id="get_events",
        description=(
            "从 from 开始拉；返回的 `next` 就是下次的 from。wait>0 时没有新事件会挂起"
            "等待，最长 60s——给不方便消费 SSE 的编排器（n8n HTTP 节点、Dify）用。"
        ),
        tags=["events"],
        dependencies=guard,
    )
    async def session_events(
        session_id: str,
        from_seq: int = Query(default=1, alias="from", ge=1, description="起始游标"),
        limit: int = Query(default=200, ge=1, le=2000),
        wait: float = Query(default=0.0, ge=0.0, le=MAX_WAIT, description="long-poll 秒数"),
    ) -> dict[str, Any]:
        session = pick(session_id)
        if wait:
            await session.wait_new(from_seq, min(wait, MAX_WAIT))
        items = session.since(from_seq, limit)
        return {
            "session_id": session_id,
            "events": items,
            "next": items[-1]["seq"] + 1 if items else max(from_seq, session.next_seq),
            "first_seq": session.first_seq,
            "dropped_events": session.dropped,
            "status": session.status,
            "detail": session.detail,
        }

    @app.get(
        "/session/{session_id}/events/stream",
        summary="事件流（SSE）",
        operation_id="stream_events",
        description=(
            "follow=true（默认）常驻推送，会话关掉或客户端断开才结束——编辑器/看板用。"
            "follow=false 只把当前这一轮推完就关流：调用方不必自己想办法中断连接，"
            "适合'提交一轮 → 跟完 → 收工'的一次性消费方。"
            "⚠️ follow=false 要在**提交 prompt 之后**开——会话空闲且没有新事件时它"
            "立刻关流，先开会拿到一个空流（200 但零事件）。"
        ),
        tags=["events"],
        dependencies=guard,
    )
    async def session_events_stream(
        request: Request,
        session_id: str,
        from_seq: int = Query(default=1, alias="from", ge=1),
        follow: bool = Query(default=True, description="false=本轮跑完即关流"),
    ):
        session = pick(session_id)

        async def pump():
            cursor = from_seq
            idle = 0.0
            while session_id in sessions:
                #  客户端走了就收摊。不查的话每个断掉的连接都留一个永远转下去的
                #  task——常驻服务上这是稳定泄漏，不是理论问题
                if await request.is_disconnected():
                    return
                for item in session.since(cursor, 500):
                    cursor = item["seq"] + 1
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    idle = 0.0
                caught_up = session.next_seq <= cursor
                if not follow and caught_up and not session.busy:
                    return
                await session.wait_new(cursor, POLL_SLICE)
                if session.next_seq <= cursor:
                    idle += POLL_SLICE
                    if idle >= HEARTBEAT:
                        idle = 0.0
                        #  心跳：中间的反代/网关普遍会掐掉长时间无字节的连接
                        yield ": keep-alive\n\n"

        return StreamingResponse(pump(), media_type="text/event-stream")

    @app.get(
        "/session/{session_id}/permissions",
        summary="挂起的审批",
        operation_id="list_permissions",
        tags=["approval"],
        dependencies=guard,
    )
    async def permissions_list(session_id: str) -> dict[str, Any]:
        session = pick(session_id)
        return {"pending": [item.to_dict() for item in session.snapshot_pending()]}

    @app.post(
        "/session/{session_id}/permissions",
        summary="回一个审批决定",
        operation_id="respond_permission",
        description=(
            "decision=allow 放行；deny 拒绝（reason 会原文回灌给模型，等于改指令）；"
            "updated_args 非空时整体替换本次调用参数再执行（宿主'包一层再放行'的通道）。"
        ),
        tags=["approval"],
        dependencies=guard,
    )
    async def permissions_respond(
        session_id: str,
        request_id: str = Body(embed=True),
        decision: str = Body(embed=True, description="allow | deny"),
        reason: str = Body(default="", embed=True),
        updated_args: dict[str, Any] | None = Body(default=None, embed=True),
    ) -> dict[str, Any]:
        session = pick(session_id)
        request = session.take_pending(request_id)
        if request is None:
            raise HTTPException(status_code=404, detail=f"没有挂起的审批 {request_id!r}（可能已超时）")
        if decision not in ("allow", "deny"):
            raise HTTPException(status_code=400, detail="decision 只能是 allow 或 deny")
        request.verdict = (
            Allow(note=reason, updated_args=updated_args) if decision == "allow" else Deny(reason)
        )
        request.done.set()
        return {"request_id": request_id, "decision": decision}

    @app.post(
        "/session/{session_id}/abort",
        summary="打断当前这一轮",
        tags=["run"],
        dependencies=guard,
        operation_id="abort",
    )
    async def session_abort(session_id: str) -> dict[str, Any]:
        session = pick(session_id)
        session.async_agent.interrupt()
        #  打断时挂着审批的话，工作线程还堵在 Event 上——先把它放掉，
        #  否则 interrupt 要等到 approval_timeout 才生效
        for request in session.snapshot_pending():
            request.verdict = Deny("本轮已被 abort")
            request.done.set()
        return {"session_id": session_id, "aborted": True}

    @app.post(
        "/session/{session_id}/steer",
        summary="向进行中的一轮插话",
        operation_id="steer",
        description=(
            "不打断，模型在下一个 step 边界看到这句话再继续。"
            "会话空闲时回 409——空闲期入队的插话会在下一轮开头被 drain 掉，"
            "回 200 等于骗调用方说话已送到。"
        ),
        tags=["run"],
        dependencies=guard,
    )
    async def session_steer(
        session_id: str,
        text: str = Body(embed=True),
    ) -> dict[str, Any]:
        session = pick(session_id)
        #  Agent._turn() 开头就 drain_steers()，空闲时入队的插话下一轮开头即被
        #  丢弃。回 200 会让编排器以为这句话进了上下文——响亮地 409，别装成功
        if not session.busy:
            raise HTTPException(
                status_code=409, detail="会话空闲，没有正在进行的一轮可插话（改用 /prompt）"
            )
        session.async_agent.steer(text)
        return {"session_id": session_id, "steered": True}

    if cfg.mcp:
        #  MCP server 面：同一会话层的第二张脸（LangChain / LangGraph 经
        #  langchain-mcp-adapters、其它 MCP client 从这儿进来）。
        #  钩子清单就是两张脸的共享契约面，别宽于此（见 serve_mcp.mount_mcp）。
        from .serve_mcp import mount_mcp

        mount_mcp(
            app,
            cfg,
            sessions=sessions,
            make_session=make_session,
            start_turn=start_turn,
            close_session=close_session,
            guard=guard,
        )

    return app


def serve(cfg: ServeConfig) -> int:
    """起服务（阻塞）。返回退出码。"""
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover
        raise ServeUnavailable(str(exc)) from exc

    loopback = cfg.host in ("127.0.0.1", "::1", "localhost")
    if not loopback and not cfg.token:
        print(
            "拒绝启动：绑到非回环地址却没有 token。这个服务能执行任意命令，"
            "裸奔等于把 shell 开放给整个网段。加 --token 或改绑 127.0.0.1。",
            file=sys.stderr,
        )
        return 2
    app = create_app(cfg)
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning")
    return 0


def print_openapi(cfg: ServeConfig, public_url: str = "") -> int:
    """把 OpenAPI schema 打到 stdout —— Dify 自定义工具直接贴这份。

    必须带 `servers`：Dify / n8n 的 OpenAPI 导入器要从这里取 base URL，缺了
    它们只能拿到相对路径，导进去也发不出请求。FastAPI 默认不生成这一段
    （它假设调用方就在同一个 origin 下），所以这里显式补。

    `public_url` 缺省按监听地址推。**编排器多半跑在容器里**，那时 127.0.0.1
    是容器自己而不是本机——所以推出来是回环地址时额外打一行提示，别让人
    照抄一个在 Dify 里必然连不上的地址。
    """
    app = create_app(cfg)
    schema = app.openapi()
    url = public_url or f"http://{cfg.host}:{cfg.port}"
    schema["servers"] = [{"url": url, "description": "xiaoyu serve"}]
    if not public_url and cfg.host in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
        print(
            f"提示：schema 里的 servers 填的是 {url}。若 Dify / n8n 跑在容器里，"
            "这个地址指向的是容器自己——请用 --public-url 指定它们真正能访问到的地址"
            "（Docker Desktop 上通常是 http://host.docker.internal:%d）。" % cfg.port,
            file=sys.stderr,
        )
    json.dump(schema, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0
