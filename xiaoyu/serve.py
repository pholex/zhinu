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
    POST   /session/{id}/budget           改/撤本会话的预算（budget_reached 后的出路）
    POST   /mcp                           同一会话层的 MCP server 面（见 serve_mcp.py）

控制面（持久化、版本化的 Agent 对象；会话创建时 `agent` 字段引用它）：

    POST   /agent                         新建 agent（model/mode/审批档/沙箱/预算/定价）
    GET    /agent                         列出
    GET    /agent/{id}                    详情（?versions=true 带全部版本）
    POST   /agent/{id}                    更新 → 新版本；已在跑的会话仍钉着旧版本
    DELETE /agent/{id}                    归档（只读、不再接受新会话；不删）

会话清单与 agent 对象默认落盘在用户配置目录的 `serve/<root slug>/`，serve 重启后
会话自动接回（历史来自会话日志，事件游标接着编号）。细节见 serve_state.py。

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
- 回环且无 token 时，本机浏览器是唯一够得着它的外人：Host 白名单挡 DNS rebinding，
  Origin 白名单挡跨站请求 / WebSocket（见 `_install_local_guard`）。
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
import copy
import hashlib
import hmac
import json
import re
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

#  Request 必须在**模块全局**里解析得到，不能只在 create_app 内部 import：
#  本文件有 `from __future__ import annotations`，路由函数的注解是字符串，
#  FastAPI 用函数的 __globals__ 去解析它们——函数局部的 import 在那里看不见，
#  `request: Request` 会被当成一个名叫 request 的**查询参数**，SSE 端点直接 422。
#  其余 fastapi 名字（Body/Query/Header/Depends）都只作默认值用，不受影响。
try:
    from starlette.requests import Request
    from starlette.websockets import WebSocket, WebSocketDisconnect
except ImportError:  # pragma: no cover - 没装 [serve] 时 create_app 会先抛 ServeUnavailable
    Request = Any  # type: ignore[assignment,misc]
    WebSocket = Any  # type: ignore[assignment,misc]
    WebSocketDisconnect = Exception  # type: ignore[assignment,misc]

from . import serve_history, serve_catalog, serve_weaves, serve_commands, serve_contests, serve_fleets, serve_fleet_controls, serve_weave_controls, serve_fleet_commands, artifact_views, serve_artifacts

from . import serve_ownership
from . import diagnostics, folder_trust, mcp, mcp_guard, serve_questions
from . import questions as question_types
from .agent import Agent, Allow, Deny
from .config import Config, MissingConfig
from .embedding import AsyncAgent, RunCompleted
from .events import UIEvent
from .permissions import Permissions
from .serve_browser import (
    CLOSE_NOT_FOUND,
    CLOSE_REPLACED,
    CLOSE_UNAUTHORIZED,
    DEFAULT_CALL_TIMEOUT,
    HELLO_TIMEOUT,
    BrowserBridge,
    token_matches,
)
from .serve_state import (
    AgentStore,
    Budget,
    NotFound,
    SessionStore,
    StateError,
    budget_breach,
    check_mcp_servers,
    check_pricing,
    public_agent,
    spend_of,
)
from .session_log import (
    LoadedMessages,
    SessionLockedError,
    SessionLog,
    _workspace_slug,
    last_usage,
    load_messages,
    sessions_dir,
    turn_starts,
)
from .tools import Toolbox

#  进程级计量：在册会话数 / 处理中的 HTTP 请求数（GET /diagnostics 读）
SESSIONS_LIVE = diagnostics.Gauge("serve.sessions.live")
REQUESTS_IN_FLIGHT = diagnostics.Gauge("serve.requests.in_flight")

#  事件环形缓冲的默认容量：一轮重活的事件数在百量级，5000 够存几十轮；
#  超出后从头丢，并把 first_seq/dropped 如实报给客户端——静默截断会让
#  编排侧以为自己拉全了。
DEFAULT_BUFFER = 5000
#  游标水位按块预留：发出去的每个 seq 都小于盘上清单里的预留线。逐事件写盘不值得，
#  只在开轮/收尾写又不够——轮中进程被杀时盘上还是开轮时的水位，重启后从那里接号
#  就是把已经发给客户端的序号再发一遍（客户端拿旧游标一条都拉不到）。
SEQ_RESERVE_BLOCK = 1000
#  每个会话记住最近多少个幂等键，以及键的长度上限
IDEMPOTENCY_KEEP = 64
IDEMPOTENCY_KEY_MAX = 200
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
    approval: str | None = None  # ask | allow_all；未指定时由 environment 选默认值
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT
    sandbox: bool | None = None
    sandbox_network: bool | None = None
    append_system_prompt: str = ""
    #  推理深度默认值；agent 对象可覆盖（见 serve_state.AGENT_CONFIG_KEYS）
    effort: str = ""
    buffer_limit: int = DEFAULT_BUFFER
    max_field: int = DEFAULT_MAX_FIELD
    #  能同时跑的会话数 = 工作线程池大小。审批期间线程是被占着的（approver 就地
    #  阻塞），所以这个数同时也是"能同时挂起等审批的会话数"上限。
    max_sessions: int = DEFAULT_MAX_SESSIONS
    #  要不要挂 /mcp（MCP server 面，见 serve_mcp.py）。与 REST 共享会话层，
    #  默认开着；`--no-mcp` 关掉后 serve 退回纯 REST。
    mcp: bool = True
    #  agent 对象能否自带 MCP server（mcp_servers 字段）：off=不收；http=只收远端
    #  （Streamable HTTP，不在本机起进程）；all=stdio 也收。默认 off——stdio
    #  server 是**沙箱之外**的本机子进程，一个拿着 token 的 HTTP 客户端不该
    #  默认就能让服务端跑任意命令。
    agent_mcp: str = "off"
    #  控制面状态（agent 对象、会话清单、会话日志）落在哪。None = 按 root 推：
    #  用户配置目录下的 `serve/<root slug>/`（与 sessions/ 同级；Linux/macOS 即
    #  `~/.config/xiaoyu/serve/…`，Windows 为 `%APPDATA%\xiaoyu\serve\…`）。
    #  persist=False 时全在内存（测试 / 一次性跑）。
    state_dir: Path | None = None
    persist: bool = True
    # Desktop preparation shells are volatile until a task is submitted.
    persist_empty_sessions: bool = field(default=True, kw_only=True)
    #  终端会话目录是否列出 root 之外的工作目录里的会话（只列出与查看，不能在本服务继续）。
    #  本机单用户宿主才打开；缺省关着，网络可达的服务不该把无关目录的历史露出去
    catalog_everywhere: bool = field(default=False, kw_only=True)
    # Only an embedding host can resolve project identities to filesystem scopes.
    project_resolver: Any = field(default=None, kw_only=True, repr=False)
    project_scope: dict = field(default_factory=dict, kw_only=True)
    # Embedding host choice, sampled only at construction and idle turn boundaries.
    skill_policy: Any = field(default=None, kw_only=True, repr=False)
    #  对外地址（反代域名、host.docker.internal 等），写进 OpenAPI schema 的 `servers`。
    #  设了则运行中的 /openapi.json 也带上——Dify/n8n 可直接按 URL 导入；空 = 运行中的
    #  schema 不带 servers（FastAPI 默认，同源访问够用），--print-openapi 按监听地址推。
    public_url: str = ""
    #  允许跨源（CORS）的浏览器 origin 白名单，如 `chrome-extension://<id>`、
    #  `https://console.example.com`。空 = 除下面的扩展默认外不发 CORS 头（非浏览器客户端不需要）。
    #  只是"浏览器肯不肯把响应交给页面脚本"的门，不是鉴权——token 仍照常校验。
    #  无 token 时它兼作 Origin 白名单：名单外的浏览器 origin 一律 403（_install_local_guard）。
    cors_origins: tuple[str, ...] = ()
    #  默认给所有 Chrome 扩展 origin（chrome-extension://<32 位 id>）发 CORS 头：扩展 id 每台机器
    #  开发者模式装载各不相同、商店版又是另一个，没法预先列进白名单。只是 CORS，token 仍照常校验；
    #  无 token 时 _install_local_guard 仍只放行显式白名单，装了什么扩展都驱动不了无 token 的本机 serve。
    #  --no-cors-extensions 关掉，回到只认 cors_origins
    cors_extensions: bool = True
    #  浏览器桥（/session/{id}/browser）：等扩展回一次调用结果的上限（秒）
    browser_timeout: float = DEFAULT_CALL_TIMEOUT
    #  TLS：证书与私钥成对给（PEM），由 uvicorn 直接终止 TLS。只给一个在 CLI 层就拒绝。
    #  没有反代可放、又要出回环地址时用；有反代的仍建议在反代上做 TLS
    tls_cert: Path | None = None
    tls_key: Path | None = None
    #  部署者声明已有容器 / VM 隔离；不创建沙箱，也不改变鉴权与其它护栏。
    environment: str = "host"

    def __post_init__(self) -> None:
        if self.environment not in ("host", "sandbox"):
            raise ValueError("environment 必须是 host 或 sandbox")
        if self.approval is None:
            self.approval = "allow_all" if self.environment == "sandbox" else "ask"
        #  只填未指定项：agent 对象经 replace 收紧到 ask / sandbox=True 时必须保留。
        if self.environment == "sandbox" and self.sandbox is None:
            self.sandbox = False

    @property
    def scheme(self) -> str:
        return "https" if self.tls_cert is not None else "http"

    def resolved_state_dir(self) -> Path:
        if self.state_dir is not None:
            return self.state_dir
        return sessions_dir().parent / "serve" / _workspace_slug(str(self.root))


@dataclass
class _Pending:
    """一条挂起的审批请求。`done` 由 HTTP 侧 set，工作线程在它上面阻塞。"""

    id: str
    name: str
    args: dict[str, Any]
    created_at: float
    done: threading.Event = field(default_factory=threading.Event)
    verdict: Any = None
    #  HTTP 侧回的字面决定（allow / allow_session / deny）：permission.resolved 事件
    #  照这个报，消费方 UI 才分得清"放行"与"拒绝"；超时没人回则是 "timeout"
    decision: str = ""
    #  「本会话都允许」这一档的范围文案（工具名 / bash 命令键），由服务端按这次
    #  调用推导、随挂起项下发——客户端只能选档，带不进自定义范围；None = 推不出，
    #  没有这个选项。判据与 TUI / ACP 确认框同一处（Permissions.session_grant_label）
    session_scope: str | None = None
    #  可选的档位，细到粗：[{"scope","label"}, …]，首项就是 session_scope；之后是这个
    #  工具所属族的粗档位（如 browser:*，一次放开全部浏览器写操作）。客户端答
    #  allow_session 时带 scope 选档，不带 = 首项——只认 session_scope 的老客户端不受影响
    session_scopes: list[dict[str, str]] = field(default_factory=list)
    #  答了 allow_session 后真正落下的范围，permission.resolved 事件随 decision 一起报
    granted: str | None = None
    identity: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.id,
            "tool": self.name,
            "args": self.args,
            "created_at": self.created_at,
            "waiting_seconds": round(time.time() - self.created_at, 3),
            "session_scope": self.session_scope,
            "session_scopes": list(self.session_scopes),
            **({"identity": dict(self.identity), "allowed_decisions": ["allow", "deny"]} if self.identity else {}),
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
        if session is None or loop is None or session.events_closed:
            return  # 绑定前不可能有事件；真有也只能丢，不能崩在工具里
        try:
            loop.call_soon_threadsafe(session.publish_event, event)
        except RuntimeError:
            if not session.closing:
                raise


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

    def __init__(
        self,
        session_id: str,
        agent: Agent,
        workspace: Path,
        cfg: ServeConfig,
        agent_ref: dict[str, Any] | None = None,
        agent_config: dict[str, Any] | None = None,
        budget: Budget | None = None,
        pricing: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self.id = session_id
        self.agent = agent
        self.async_agent = AsyncAgent(agent)
        self.workspace = workspace
        self.cfg = cfg
        #  钉住的 agent（{id, name, version}）与那一版配置的快照。快照进会话而不是
        #  每次回 store 查：agent 之后再改版/归档都不该影响这个会话。
        self.agent_ref = agent_ref
        if agent_ref and agent_ref.get("name"):
            #  钉住了 agent 对象的会话：OpenTelemetry 里按对象名区分（gen_ai.agent.name）
            agent.agent_name = str(agent_ref["name"])
        self.agent_config = agent_config or {}
        self.pricing = pricing or {}
        self.budget = budget
        self.budget_reason = ""
        self.created_at = time.time()
        self.updated_at = self.created_at

        self.status = "idle"
        self.detail = "idle"
        self.error = ""
        self.busy = False
        #  正在跑的那一轮（开轮即落盘，见 start_turn）；lost_turn 是上一个进程里
        #  没跑完就随进程一起没了的那一轮，留到下一轮开跑才清
        self.in_flight: dict[str, Any] | None = None
        self.lost_turn: dict[str, Any] | None = None
        self.last_result: dict[str, Any] | None = None
        self.title = "新任务"
        self.fork_origin: dict[str, Any] | None = None
        self.history_warning = ""
        self.runs: list[dict[str, Any]] = []
        self.weaves = serve_weaves.WeaveView()
        self.weave_controls = serve_weave_controls.ControlLedger()
        self.contests = serve_contests.ContestView()
        self.fleets = serve_fleets.FleetView()
        self.fleet_controls = serve_fleet_controls.ControlLedger()
        self.closing = False
        self.owner_uncertain = False
        self.owner_fanout = False
        self.execution_owner = None
        self.events_closed = False
        #  agent 自带 MCP server 的会话私有 manager（assemble 里回填），关会话时收掉
        self.mcp_manager: Any = None
        self.turns = 0

        self.events: list[dict[str, Any]] = []
        self.next_seq = 1
        self.first_seq = 1
        self.dropped = 0
        #  已落盘的预留线与"越线了，去落盘"的回调（装配时接上清单存储）
        self.seq_reserved = 0
        self.on_reserve: Callable[[], None] | None = None
        #  幂等键 → 那一次提交的记录（轮号、指令摘要、任务、结果）。在跑的那条不淘汰
        self.submissions: dict[str, dict[str, Any]] = {}
        # 小回执随清单持久化；大结果只在内存里保留最近若干条。
        self.submission_receipts: dict[str, dict[str, Any]] = {}
        self.creation_receipt: dict[str, str] | None = None
        self._tick = asyncio.Event()

        self.pending: dict[str, _Pending] = {}
        self._pending_lock = threading.Lock()
        self._task: asyncio.Task | None = None
        #  浏览器桥（扩展连上才有；断开即 None）。工具随连接注册/注销
        self.bridge: BrowserBridge | None = None
        self.questions: question_types.QuestionManager | None = None
        self.question_options: dict[str, Any] | None = None
        self.question_closing = False

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
        if self.next_seq >= self.seq_reserved:
            self.seq_reserved = self.next_seq + SEQ_RESERVE_BLOCK
            if self.on_reserve is not None:
                self.on_reserve()
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

    def note_notification(self, item: dict[str, Any]) -> None:
        """有一条给模型的通知入队了（后台任务完成、monitor 有输出……）。

        `idle` 是要点：会话在跑的时候，通知搭下一条工具结果就送到了，编排方
        不必做什么；会话空闲时没有下一步，它会一直等到有人再提交一轮。
        """
        self.publish("notification.pending", idle=not self.busy, **item)

    def pending_notifications(self) -> list[dict[str, Any]]:
        limit = self.cfg.max_field
        return [
            {**item, "text": _clip(item["text"], limit)[0]}
            for item in self.agent.pending_notifications()
        ]

    def interrupt_owned(self) -> None:
        if self.busy or (self.async_agent._task is not None and not self.async_agent._task.done()):
            self.owner_uncertain = True
        self.async_agent.interrupt()

    def publish_event(self, event: UIEvent) -> None:
        if self.events_closed:
            return
        payload = event.to_dict()
        delegated = {"delegate", "qixiang", "douqiao", "douqiao_judge", "explore", "new_context"}
        delegated.update(spec.name for spec in getattr(self.agent, "_mounted_agent_specs", ()))
        if payload.get("kind") == "tool.running" and payload.get("name") in delegated:
            self.owner_fanout = True
        if self.busy and not self.closing and serve_ownership.reason(self):
            self.interrupt_owned()
        if payload.get("kind") == "qixiang.updated":
            command = (self.in_flight or {}).get("command", {})
            if command.get("operation") == "resume":
                payload["source_batch_id"] = command["source_batch_id"]
                payload["source_item_ids"] = command["item_ids"]
            self.weaves.record(payload, self.agent.session_log,
                               str((self.in_flight or {}).get("id", "")))
            for item in payload.get("items", ()):
                item.pop("artifact_previews", None)
        elif payload.get("kind") == "qixiang.execution.updated":
            if self.weaves.record_execution(payload, self.agent.session_log):
                self.weave_controls.observe_snapshot(payload)
                if not self.weaves.integrity:
                    for control in payload.get("controls", ()):
                        operation = self.weave_controls.operations.get(control.get("operation_id"))
                        if operation:
                            operation.update(persisted=False, warning=self.weaves.warning)
        elif payload.get("kind") == "douqiao.updated":
            self.contests.record(payload, self.agent.session_log,
                                 str((self.in_flight or {}).get("id", "")))
            for candidate in payload.get("candidates", ()):
                candidate.pop("artifact_previews", None)
        elif payload.get("kind") == "chenshu.updated":
            self.fleets.record(payload, self.agent.session_log,
                               str((self.in_flight or {}).get("id", "")))
            self.fleet_controls.observe(payload.get("controls", ()))
        self._append(payload)
        if self.busy and not self.budget_reason:
            reason = self.check_budget()
            if reason:
                self.budget_reason = reason
                self.publish("budget.reached", reason=reason)
                self.interrupt_owned()
                if self.questions is not None:
                    self.questions._wake()

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

    # ---------- 预算 ----------

    def spend(self):
        """累计用量（token 恒有；美元只在设了美元预算时算，见 serve_state.spend_of）。"""
        want_usd = self.budget is not None and self.budget.usd is not None
        return spend_of(self.agent.usage.snapshot(), self.pricing, want_usd)

    def check_budget(self) -> str:
        """预算耗尽的原因；空串 = 还有余量。只判定，不改状态。"""
        return budget_breach(self.budget, self.spend())

    @property
    def budget(self) -> Budget | None:
        return self._budget

    @budget.setter
    def budget(self, value: Budget | None) -> None:
        """预算一变就同步给 agent：硬闸（这里）之外模型还要**知情**——
        倒计时提示与到线前收尾都靠 agent.config.budget_tokens（美元预算折不成
        token，只同步 token 那一半）。"""
        self._budget = value
        self.agent.set_budget_tokens(value.tokens if value is not None else None)

    def status_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "title": self.title,
            "fork_origin": self.fork_origin,
            "history_persisted": bool(self.cfg.persist and self.agent.session_log
                                      and self.agent.session_log.complete and not self.history_warning),
            "history_warning": self.history_warning,
            "writable": not bool(serve_ownership.reason(self)),
            "write_blocked_reason": serve_ownership.reason(self),
            "status": self.status,
            "detail": self.detail,
            "busy": self.busy,
            "error": self.error,
            "turns": self.turns,
            "usage": self.agent.usage.to_dict(),
            "spend": self.spend().to_dict(),
            "budget": self.budget.to_dict() if self.budget else None,
            "budget_reason": self.budget_reason,
            "pending_approvals": [item.to_dict() for item in self.snapshot_pending()],
            "pending_notifications": self.pending_notifications(),
            "browser": self.bridge.info() if self.bridge is not None else None,
            "next_seq": self.next_seq,
            "first_seq": self.first_seq,
            "dropped_events": self.dropped,
            "last_result": self.last_result,
            "in_flight": self.in_flight,
            "lost_turn": self.lost_turn,
            "updated_at": self.updated_at,
        }

    def info_dict(self) -> dict[str, Any]:
        return {
            **self.status_dict(),
            "questions": self.question_options,
            "workspace": str(self.workspace),
            **({"project_id": self.cfg.project_scope["project_id"],
                "project_folders": self.cfg.project_scope["folders"]} if self.cfg.project_scope else {}),
            "model": self.agent.config.model,
            "next_model": getattr(self, "next_model", ""),
            "mode": getattr(self.agent.config, "mode", ""),
            "context_tokens": self.agent.context_tokens(),
            "agent": self.agent_ref,
            "session_log": str(self.agent.session_log.path) if self.agent.session_log else "",
            "created_at": self.created_at,
        }

    def manifest(self, final: bool = False) -> dict[str, Any]:
        """重启后重建本会话所需的全部信息（见 serve_state.SessionStore）。

        `final` = 优雅停机时的最后一次落盘：之后不会再有事件，预留线收紧到实际
        水位，重启后的编号紧接着上次，不留空档。
        """
        return {
            "session_id": self.id,
            "title": self.title,
            "fork_origin": self.fork_origin,
            "updated_at": self.updated_at,
            "history_warning": self.history_warning,
            "questions": self.question_options,
            "workspace": str(self.workspace),
            **({"project_scope": self.cfg.project_scope} if self.cfg.project_scope else {}),
            "agent": self.agent_ref,
            "agent_config": self.agent_config,
            "next_model": getattr(self, "next_model", ""),
            "budget": self.budget.to_dict() if self.budget else None,
            "pricing": self.pricing,
            "session_log": str(self.agent.session_log.path) if self.agent.session_log else "",
            "next_seq": self.next_seq,
            "seq_reserved": self.next_seq if final else max(self.seq_reserved, self.next_seq),
            "turns": self.turns,
            "created_at": self.created_at,
            "in_flight": self.in_flight,
            "lost_turn": self.lost_turn,
            "submission_receipts": self.submission_receipts,
            "creation_receipt": self.creation_receipt,
        }

    # ---------- 审批（工作线程 → 事件循环） ----------

    def mark_waiting(self, request: _Pending) -> None:
        if not request.identity:
            self.detail = "waiting_for_approval"
        self.publish("permission.requested", **request.to_dict())

    def mark_resolved(self, request_id: str, decision: str, granted: str | None = None, identity: dict | None = None) -> None:
        if self.busy and not identity:
            self.detail = "working"
        #  granted 只在 allow_session 时带：侧栏据此刷新"已放开哪些"的清单
        extra = {"granted": granted} if granted else {}
        if identity:
            extra["identity"] = identity
        self.publish("permission.resolved", request_id=request_id, decision=decision, **extra)


class _HttpApprover:
    """`Agent.approver`：在工作线程里阻塞等 HTTP 侧的决定。

    `allow_all` 档直接批准所有审批（启动方显式选，或采用 sandbox 环境预设）。
    超时 / 会话被关都按拒绝收场——安全闸门宁可多拦一次，不能在故障时静默放行。
    """

    def __init__(self, ref: _SessionRef) -> None:
        self._ref = ref

    def __call__(self, name: str, args: dict[str, Any], identity: dict | None = None, stop_requested=None) -> Any:
        session, loop = self._ref.session, self._ref.loop
        if session is None or loop is None or session.closing:
            return Deny("会话尚未就绪或正在关闭，已自动拒绝")
        if stop_requested and stop_requested():
            return Deny("该成员已请求停止")
        if problem := serve_ownership.reason(session):
            return Deny(problem)
        if session.cfg.approval == "allow_all":
            return True
        options = [] if identity else session.agent.permissions.session_grant_options(name, args)
        request = _Pending(
            id=uuid.uuid4().hex[:12], name=name, args=args, created_at=time.time(),
            session_scope=options[0][0] if options else None,
            session_scopes=[{"scope": scope, "label": label} for scope, label in options],
            identity=dict(identity) if identity else None,
        )
        session.add_pending(request)
        loop.call_soon_threadsafe(session.mark_waiting, request)
        deadline = time.monotonic() + session.cfg.approval_timeout
        got = False
        while time.monotonic() < deadline:
            if session.closing or (stop_requested and stop_requested()):
                request.verdict = Deny("会话关闭或该成员已请求停止")
                request.decision = "cancelled"
                request.done.set()
            if request.done.wait(timeout=min(0.1, max(0, deadline - time.monotonic()))):
                got = True
                break
        session.drop_pending(request.id)
        if not got:
            loop.call_soon_threadsafe(session.mark_resolved, request.id, "timeout", None, request.identity)
            return Deny(
                f"审批超时（{session.cfg.approval_timeout:g}s 内没有收到 "
                f"POST /session/{session.id}/permissions 的决定），已自动拒绝。"
                "注意：这不是用户的否决，是无人应答——不要原样重试同一调用"
                "（只会再超时一次）。请在回复里说明你需要哪个工具、想做什么，"
                "把决定留给编排侧。"
            )
        loop.call_soon_threadsafe(session.mark_resolved, request.id,
                                 request.decision or "resolved", request.granted, request.identity)
        if stop_requested and stop_requested():
            return Deny("该成员已请求停止")
        if not isinstance(request.verdict, Deny) and (problem := serve_ownership.reason(session)):
            return Deny(problem)
        return request.verdict


def build_agent(
    session_id: str,
    workspace: Path,
    cfg: ServeConfig,
    ref: _SessionRef,
    session_log: SessionLog | None = None,
    log_dir: Path | None = None,
    mcp_servers: dict[str, Any] | None = None,
) -> tuple[Agent, "mcp.McpManager | None"]:
    """现配一个 Agent（装配顺序照 acp_main 的工厂，两条协议面保持同源）。

    `mcp_servers`（agent 对象自带的 server）非空时，与工作区配置发现合并
    （同名以 agent 为准，合并在 mcp.launch 里做）起一个**会话私有**的
    manager 注入 Toolbox；返回它以便会话关闭时一并收掉——这些进程/连接
    的生命周期就是这个会话，不能挂到进程级缓存里跟别的会话共用。

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
        effort=cfg.effort or None,
        workspace_trusted=trusted,
        additional_workspaces=tuple(Path(p) for p in cfg.project_scope.get("folders", []) if Path(p) != workspace),
    )
    if cfg.skill_policy is not None:
        config.disabled_skill_names = tuple(cfg.skill_policy())
    permissions = Permissions.load(config.workspace, include_workspace=trusted)
    if session_log is None:
        #  serve 的会话日志单独放一个目录（嵌入宿主的惯例）：常驻服务的几百个
        #  会话不该混进 `xiaoyu --resume` 列表里，也方便重启时按清单定位
        session_log = serve_ownership.StrictSessionLog.create(
            config.model, str(config.workspace), directory=log_dir, session_id=session_id
        )
    manager: mcp.McpManager | None = None
    mcp_view: mcp.McpView | None = None
    if mcp_servers and config.enable_mcp:
        specs, _ = mcp.parse_server_mapping(mcp_servers, expand=False)
        manager = mcp.launch(config, extra_specs=specs)
        if manager is not None:
            mcp_view = mcp.McpView(manager, "all")
    agent = Agent(
        config,
        Toolbox(config, mcp_view=mcp_view),
        approver=_HttpApprover(ref),
        session_log=session_log,
        permissions=permissions,
        #  主循环的事件由 AsyncAgent.stream() 的临时事件桥接走；这个 sink 是给
        #  被闭包捕获的那批（explore / web_search / 子 agent）用的，同样进缓冲区。
        #  不能给 NullSink——那批工具的事件会整段消失，编排侧看不出它在干活。
        sink=_BridgeSink(ref),
    )
    return agent, manager


LOOPBACK_NAMES = frozenset({"127.0.0.1", "localhost", "::1"})
#  容器里的编排器经 Docker Desktop 访问宿主回环时用的主机名（print_openapi 推荐的写法）。
#  公网 DNS 注册不到这个名字，攻击页没法让浏览器带着它发请求。
DOCKER_HOST_NAME = "host.docker.internal"


def _hostname(netloc: str) -> str:
    """Host 头的值 → 小写主机名（剥端口与 IPv6 方括号）；解析不了返回空串。"""
    try:
        return (urlsplit("//" + netloc).hostname or "") if netloc else ""
    except ValueError:
        return ""


def _install_local_guard(app: Any, cfg: ServeConfig) -> None:
    """无 token 时的浏览器侧闸。这个服务能执行任意命令，而无 token 的回环 serve
    唯一够得着它的"外人"是用户本机浏览器里跑的网页，两条路都得堵：

    - **DNS rebinding**：攻击者域名先解析到自己、再改解析到 127.0.0.1，页面脚本
      就成了"同源"请求，CORS 根本不起作用。但浏览器发出的 Host 仍是攻击者域名——
      只收回环主机名即可挡住（端口不看：rebinding 控制不了主机名，端口映射/反代会改端口）。
    - **跨站请求 / WebSocket**：Host 正确，但 Origin 是别的站。简单 POST 与 WebSocket
      握手都不走 CORS 预检，请求会真的执行——只放行本服务自己的 origin（/docs 页）
      与 `--cors-origin` 白名单。不带 Origin 的是非浏览器客户端（curl / n8n / SDK），照常放行。

    有 token 时不装：攻击页拿不到凭据，而反代改写 Host、持令牌的任意 origin 客户端都得照常能用。
    """
    hosts = LOOPBACK_NAMES | {DOCKER_HOST_NAME}
    origins = {f"http://{name}:{cfg.port}" for name in ("127.0.0.1", "localhost", "[::1]")}
    origins |= {o.rstrip("/") for o in cfg.cors_origins}

    class _LocalGuard:
        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] not in ("http", "websocket"):
                await self.inner(scope, receive, send)
                return
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            origin = headers.get("origin")
            if _hostname(headers.get("host", "")) not in hosts:
                reason = "Host 不是本机地址（疑似 DNS rebinding）。无 token 的 serve 只接受回环主机名；经其它主机名访问请加 --token"
            elif origin is not None and origin.rstrip("/") not in origins:
                reason = f"跨站 origin {origin!r} 不在白名单。浏览器客户端用 --cors-origin 放行，或给服务加 --token"
            else:
                await self.inner(scope, receive, send)
                return
            if scope["type"] == "websocket":
                #  accept 之前发 close：服务器回 HTTP 403，握手不成立
                await send({"type": "websocket.close", "code": 1008, "reason": "forbidden"})
                return
            body = json.dumps({"detail": reason}, ensure_ascii=False).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 403,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
            })
            await send({"type": "http.response.body", "body": body})

    app.add_middleware(_LocalGuard)


#  Chrome / Edge 扩展的 origin：固定 32 个 a-p 小写字母的 id。写死形状而不是 `chrome-extension://.*`，
#  免得 `chrome-extension://evil.example` 这类畸形 origin 也被放过
EXTENSION_ORIGIN_RE = r"^chrome-extension://[a-p]{32}$"


def _install_cors(app: Any, origins: tuple[str, ...], extensions: bool = True) -> None:
    """给白名单里的浏览器 origin 发 CORS 头（含 Chrome 的 Private Network Access 预检）。

    典型消费方是浏览器扩展 / 自研 Web 控制台：它们的脚本跑在另一个 origin 上，
    没有这些头浏览器会把响应拦在页面脚本之外（请求其实到了服务端）。
    非浏览器客户端（curl / n8n / SDK）从不看这些头，所以名单外一个都不发。

    `extensions=True`（默认）额外放行所有形状合法的 Chrome 扩展 origin：扩展 id 无法预知
    （开发者模式每台机器不同、商店版又是另一个），逐个 `--cors-origin` 不现实。
    这只是让浏览器肯把响应交给扩展，token 仍照常校验；无 token 的回环 serve 另有
    `_install_local_guard` 在外层按显式白名单挡 Origin，默认放行不会让任意扩展驱动它。

    `allow_credentials=False`：凭据走 `Authorization` / `X-Xiaoyu-Token` 头，
    不用 cookie；这样也就不必为 `*` 通配开口子。
    """
    from starlette.middleware.cors import CORSMiddleware

    allowed = tuple(o.rstrip("/") for o in origins)
    extension_re = re.compile(EXTENSION_ORIGIN_RE) if extensions else None

    def is_allowed(origin: str) -> bool:
        return origin in allowed or bool(extension_re and extension_re.match(origin))

    class _PrivateNetworkAccess:
        """Chrome 从"公网/扩展"origin 访问回环地址会多发一次 PNA 预检，要求响应带
        `Access-Control-Allow-Private-Network: true`。不同版本的 CORSMiddleware
        对 PNA 支持不同，由本层统一按 origin 白名单处理。"""

        def __init__(self, inner: Any) -> None:
            self.inner = inner

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            if scope["type"] != "http":
                await self.inner(scope, receive, send)
                return
            headers = {k.decode("latin-1").lower(): v for k, v in scope.get("headers", [])}
            origin = headers.get("origin", b"").decode("latin-1").rstrip("/")
            wants = headers.get("access-control-request-private-network", b"").lower() == b"true"
            if not (wants and is_allowed(origin)):
                await self.inner(scope, receive, send)
                return

            async def send_with_pna(message: Any) -> None:
                if message["type"] == "http.response.start":
                    message = {
                        **message,
                        "headers": [
                            *message.get("headers", []),
                            (b"access-control-allow-private-network", b"true"),
                        ],
                    }
                await send(message)

            #  PNA 已按白名单判过，消费请求头，避免新版 CORSMiddleware 按其
            #  默认关闭的 PNA 策略再次拒绝；其余来源、方法与请求头校验照常。
            inner_scope = {
                **scope,
                "headers": [(k, v) for k, v in scope.get("headers", [])
                            if k.lower() != b"access-control-request-private-network"],
            }
            await self.inner(inner_scope, receive, send_with_pna)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed),
        allow_origin_regex=EXTENSION_ORIGIN_RE if extensions else None,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type", "X-Xiaoyu-Token", "Idempotency-Key", "X-Xiaoyu-Instance"],
        max_age=600,
    )
    app.add_middleware(_PrivateNetworkAccess)


def create_app(cfg: ServeConfig):  # noqa: C901 - 路由表天然长，拆开反而更难读
    """构造 FastAPI app。抽成函数是为了让测试与 --print-openapi 不必起服务。"""
    try:
        from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query
        from fastapi.responses import StreamingResponse, JSONResponse, Response
    except ImportError as exc:  # pragma: no cover - 缺包路径在 CLI 侧有提示
        raise ServeUnavailable(str(exc)) from exc

    sessions: dict[str, _Session] = {}
    fleet_claims: dict[str, _Session] = {}
    fleet_claim_lock = threading.RLock()
    question_cleanup: set[asyncio.Task] = set()
    closing_sessions: list[_Session] = []
    recovery_errors: list[dict[str, str]] = []
    state_dir = cfg.resolved_state_dir()
    log_dir = state_dir / "logs"
    agents = AgentStore(state_dir / "agents" if cfg.persist else None)
    manifests = SessionStore(state_dir / "sessions" if cfg.persist else None, retain_empty=cfg.persist_empty_sessions)

    def adopted_logs() -> dict[Path, str]:
        """本服务原地继续着的终端日志 → 会话 id。目录据此把「被自己持有」与「被终端占用」分开。"""
        held: dict[Path, str] = {}
        for item in sessions.values():
            log = item.agent.session_log
            if log is not None and (item.fork_origin or {}).get("kind") == "cli_adopted":
                held[log.path.resolve()] = item.id
        return held

    catalog = serve_catalog.Catalog(cfg.root, held=adopted_logs, everywhere=cfg.catalog_everywhere)

    def fleet_owner_check(session: _Session, *, claim: bool = False) -> str:
        workspace = str(session.workspace.resolve())
        with fleet_claim_lock:
            if session.closing:
                return "会话正在关闭，不能启动宸枢。"
            owner = fleet_claims.get(workspace)
            if owner is not None and owner is not session:
                return "同一工作目录已有其他会话持有宸枢运行时，请先结束该会话。"
            for other in list(sessions.values()):
                runtime = getattr(other.agent, "chenshu", None)
                if (other is not session and str(other.workspace.resolve()) == workspace
                        and runtime is not None and runtime.active):
                    return "同一工作目录已有其他会话运行宸枢，不能收养正在使用的状态。"
            if claim:
                fleet_claims[workspace] = session
            return ""

    def release_fleet_claim(session: _Session) -> None:
        runtime = getattr(session.agent, "chenshu", None)
        if runtime is not None and any(thread.is_alive() for thread in list(runtime._threads.values())):
            return
        serve_fleet_commands.release_owner(session)
        with fleet_claim_lock:
            workspace = str(session.workspace.resolve())
            if fleet_claims.get(workspace) is session:
                fleet_claims.pop(workspace, None)

    def work_pending(session: _Session) -> bool:
        for name in ("management_worker_done", "connection_shutdown_done"):
            done = getattr(session, name, None)
            if done is not None and not done.is_set():
                return True
        if getattr(session, "connection_shutdown_pending", False):
            return True
        runtime = getattr(session.agent, "chenshu", None)
        if runtime is not None and any(thread.is_alive() for thread in list(runtime._threads.values())):
            return True
        tasks = (session._task, getattr(session.async_agent, "_task", None))
        if any(task is not None and not task.done() for task in tasks):
            return True
        # Cancelling a consumer is not proof that its executor worker has left.
        controller = getattr(session.agent, "_weave_controls", None)
        if controller is not None:
            for (batch_id, item_id), eid in session.weaves.current.items():
                if (execution := controller.snapshot(batch_id, item_id, eid)) and execution.get("live"):
                    return True
        return False

    async def drain_connections(session: _Session) -> None:
        """Close desktop-owned pools without mistaking cancelled awaiters for exit."""
        done = getattr(session, "management_worker_done", None)
        if done is not None and not done.is_set():
            # Retain the owner; an in-progress mutation is not safe to race with close.
            session.owner_uncertain = True
            session.execution_owner.retain(session.agent.session_log)
            return
        close = getattr(session, "connection_close", None)
        if close is None:
            return
        if session.owner_uncertain or work_pending(session):
            # Model/member workers may still use the pool; do not race their
            # execution with shutdown of the underlying transports.
            session.execution_owner.retain(session.agent.session_log)
            return
        task = getattr(session, "connection_shutdown_task", None)
        if task is None:
            done = threading.Event()
            session.connection_shutdown_done = done
            session.connection_shutdown_pending = True
            def release():
                try:
                    session.connection_shutdown_pending = bool(close())
                finally:
                    done.set()
            task = asyncio.create_task(asyncio.to_thread(release))
            session.connection_shutdown_task = task
        await asyncio.wait({task}, timeout=0.5)
        if task.done() and not task.cancelled():
            try:
                task.result()
            except Exception:
                session.connection_shutdown_pending = True
        if work_pending(session):
            session.execution_owner.retain(session.agent.session_log)

    async def drain_members(session: _Session) -> None:
        # Cooperative stop cannot force a blocked provider to return. Give final
        # observations a bounded window, then recovery retains unknown liveness.
        runtime = getattr(session.agent, "chenshu", None)
        deadline = time.monotonic() + 0.5
        while ((runtime is not None and any(thread.is_alive() for thread in list(runtime._threads.values())))
               or work_pending(session)):
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0)
        session.events_closed = True
        release_fleet_claim(session)

    @contextlib.asynccontextmanager
    async def runtime_lifespan(_app):
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
        # Foreground waits may occupy every model worker. HTTP answers need an
        # independent bounded control pool to unblock those workers.
        controls = ThreadPoolExecutor(max_workers=2, thread_name_prefix="xiaoyu-question-control")
        app.state.question_pool = controls
        restore_sessions()
        try:
            yield
        finally:
            for session in sessions.values():
                session.closing = True
                session.interrupt_owned()
                if getattr(session.agent, "chenshu", None) is not None:
                    session.agent.chenshu.stop_members()
                for request in session.snapshot_pending():
                    request.verdict = Deny("服务正在关闭")
                    request.done.set()
                if session.questions is not None:
                    session.question_closing = True
                    session.interrupt_owned()
                    session.questions._wake()
                    for request in session.snapshot_pending():
                        request.verdict = Deny("服务正在关闭")
                        request.done.set()
            active = [s._task for s in sessions.values() if s.questions is not None and s._task is not None]
            draining = set(active) | question_cleanup
            if draining:
                # A provider may remain blocked after its approval is denied.
                # Keep ownership instead of waiting indefinitely or inferring
                # worker termination from a cancelled async consumer.
                await asyncio.wait(draining, timeout=0.5)
            await asyncio.gather(*(drain_members(session) for session in sessions.values()))
            await asyncio.gather(*(drain_connections(session) for session in sessions.values()))
            #  优雅停机：把每个会话的游标水位落盘（next_seq 一直在涨，逐事件写盘
            #  不值得；停机时写一次即可让重启后的编号接得上）
            for session in sessions.values():
                if session.execution_owner.held:
                    manifests.save(session.manifest(final=True))
                #  放掉会话日志的写锁（不写 exit：会话没结束，重启后按清单接回）。
                #  进程真退出时内核也会放，但嵌入宿主可能在同一进程里停了再起
                if session.agent.session_log is not None:
                    if session.owner_uncertain or work_pending(session):
                        session.execution_owner.retain(session.agent.session_log)
                    else:
                        session.agent.session_log.release()
            controls.shutdown(wait=True, cancel_futures=True)
            pool.shutdown(wait=False, cancel_futures=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        nonlocal agents
        owner = serve_ownership.StateOwner(state_dir, cfg.persist)
        _app.state.execution_owner = owner
        agents = AgentStore(state_dir / "agents" if cfg.persist else None)
        try:
            async with runtime_lifespan(_app):
                yield
        finally:
            for session in [*sessions.values(), *closing_sessions]:
                if session.owner_uncertain or work_pending(session):
                    owner.retain(session.agent.session_log)
            owner.close()

    def require_state_owner():
        owner = getattr(app.state, "execution_owner", None)
        if owner is None or not owner.held:
            raise HTTPException(503, {"code": "execution_owner_unavailable", "message": "服务状态目录未持有执行锁。"})
        return owner

    app = FastAPI(
        lifespan=lifespan,
        #  不设就传 None（FastAPI 默认不出 servers）；设了则运行中的 schema 与
        #  --print-openapi 一致，编排器按 URL 导入也拿得到 base URL
        servers=[{"url": cfg.public_url, "description": "xiaoyu serve"}] if cfg.public_url else None,
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
    #  会话注册表挂到 app.state：测试 / 运维脚本可直接查会话对象（包括私有 MCP manager）
    app.state.sessions = sessions
    app.state.catalog = catalog
    app.state.project_resolver = cfg.project_resolver
    app.state.serve_config = cfg
    instance_id = uuid.uuid4().hex
    app.state.instance_id = instance_id
    if cfg.cors_origins or cfg.cors_extensions:
        _install_cors(app, cfg.cors_origins, cfg.cors_extensions)
    if not cfg.token:
        _install_local_guard(app, cfg)

    # ---------- 鉴权 ----------

    def header_token_ok(authorization: str, x_xiaoyu_token: str) -> bool:
        """没配 token（只监听回环）恒真；配了就要对得上。"""
        if not cfg.token:
            return True
        offered = x_xiaoyu_token or authorization.removeprefix("Bearer ").strip()
        #  常数时间比较：token 是长期凭据，别把它的前缀通过响应时间漏出去。
        #  **必须比 bytes**：starlette 按 latin-1 解 header，而
        #  compare_digest(str, str) 遇非 ASCII 直接抛 TypeError——那会让
        #  非 ASCII 的 token 整个服务不可用（每个请求 500），也让任何带非 ASCII
        #  Authorization 头的请求收到 500 而不是干净的 401。
        return hmac.compare_digest(offered.encode("utf-8"), cfg.token.encode("utf-8"))

    async def require_token(
        authorization: str = Header(default=""),
        x_xiaoyu_token: str = Header(default=""),
    ) -> None:
        if not header_token_ok(authorization, x_xiaoyu_token):
            raise HTTPException(status_code=401, detail="token 不对或缺失")

    guard = [Depends(require_token)]

    # ---------- 工具 ----------

    def pick(session_id: str) -> _Session:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"未知 session_id {session_id!r}")
        return session

    def pick_agent(agent_id: str) -> dict[str, Any]:
        try:
            return agents.get(agent_id)
        except NotFound:
            raise HTTPException(status_code=404, detail=f"未知 agent_id {agent_id!r}") from None

    def check_tightening(config: dict[str, Any]) -> None:
        """agent 对象只能比服务端启动参数**更严**，不能更松。

        启动方选了 --approval ask / 开了沙箱，是这台服务的安全底线；一个拿着
        token 的 HTTP 客户端不该能通过"建个 allow_all 的 agent"把它绕开。
        放宽在这儿是 400，不是静默钳位——静默钳位会让调用方以为自己拿到了
        allow_all。
        """
        if config.get("approval") == "allow_all" and cfg.approval != "allow_all":
            raise HTTPException(
                status_code=400,
                detail="服务端以 --approval ask 启动，agent 不能放宽成 allow_all（只能收紧）",
            )
        if config.get("sandbox") is False and cfg.sandbox is not False:
            raise HTTPException(status_code=400, detail="agent 不能关掉沙箱（只能收紧：true 或 null）")
        if config.get("sandbox_network") is True and cfg.sandbox_network is not True:
            raise HTTPException(
                status_code=400, detail="agent 不能放开沙箱网络（只能收紧：false 或 null）"
            )
        check_agent_mcp(config.get("mcp_servers"))

    def check_agent_mcp(servers: dict[str, Any] | None) -> None:
        """agent 自带 MCP server 的准入：先看服务端档位，再过 mcp_guard。

        形状已在 serve_state 校过；这里判的是"这台服务允不允许"。stdio 与
        http 分档：远端 server 不在本机起进程，风险面小得多。准入闸与 acp 的
        client_server_specs 同一套（admission_violation / endpoint_violation），
        违规是 400 带原因，不静默少一个 server。
        """
        #  先过形状（StateError → 端点映射成 400），再判档位：坏形状不该被
        #  "服务端未开放"这句更笼统的拒绝遮住
        servers = check_mcp_servers(servers)
        if not servers:
            return
        if cfg.agent_mcp == "off":
            raise HTTPException(
                status_code=400,
                detail="服务端未开放 agent 自带 MCP server（启动时加 --agent-mcp http 或 all）",
            )
        specs, _ = mcp.parse_server_mapping(servers, expand=False)
        for spec in specs:
            if spec.is_http:
                reason = mcp_guard.endpoint_violation(spec.url)
            elif cfg.agent_mcp != "all":
                reason = "服务端只开放了远端（http）server，stdio 需要 --agent-mcp all"
            else:
                reason = mcp_guard.admission_violation(spec.command, spec.args, spec.env)
            if reason:
                raise HTTPException(
                    status_code=400, detail=f"mcp_servers 里的 {spec.name!r} 被拒：{reason}"
                )

    def apply_agent_config(config: dict[str, Any], model: str = "", mode: str = "") -> ServeConfig:
        """agent 配置叠在服务端启动参数上 → 这个会话用的 ServeConfig。

        append_system_prompt 是**叠加**不是替换：服务端那份常是宿主级身份/
        纪律，agent 那份是用法级人格，两层都该在。
        """
        prompts = [cfg.append_system_prompt, config.get("append_system_prompt", "")]
        return replace(
            cfg,
            model=model or config.get("model") or cfg.model,
            base_url=config.get("base_url") or cfg.base_url,
            mode=mode or config.get("mode") or cfg.mode,
            approval=config.get("approval") or cfg.approval,
            append_system_prompt="\n\n".join(part for part in prompts if part),
            effort=config.get("effort") or cfg.effort,
            sandbox=cfg.sandbox if config.get("sandbox") is None else config["sandbox"],
            sandbox_network=(
                cfg.sandbox_network
                if config.get("sandbox_network") is None
                else config["sandbox_network"]
            ),
        )

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

    def save_reservation(session: _Session) -> None:
        """把抬高了的预留线落盘。写不进去不能连累事件发布：预留线没落盘的后果是
        崩溃后序号可能重复，而在这儿抛出去的后果是这一轮直接断掉。"""
        if session.closing or session.execution_owner is None or not session.execution_owner.held:
            return
        try:
            manifests.save(session.manifest())
        except Exception as exc:  # noqa: BLE001
            print(f"serve: 会话 {session.id} 的游标水位没能落盘：{exc}", file=sys.stderr)

    def assemble(
        session_id: str,
        target: Path,
        local: ServeConfig,
        *,
        agent_ref: dict[str, Any] | None,
        agent_config: dict[str, Any],
        budget: Budget | None,
        pricing: dict[str, dict[str, float]],
        session_log: SessionLog | None = None,
        question_options: dict[str, Any] | None = None,
    ) -> _Session:
        """新建与重启恢复共用的装配口：Agent、_Session、approver/sink 绑定。"""
        ref = _SessionRef()
        try:
            agent, manager = build_agent(
                session_id,
                target,
                local,
                ref,
                session_log=session_log,
                log_dir=log_dir,
                mcp_servers=agent_config.get("mcp_servers"),
            )
        except (OSError, SessionLockedError) as exc:
            raise HTTPException(503, {"code": "execution_owner_unavailable", "message": "会话日志无法取得严格写锁或可靠创建。"}) from exc
        except MissingConfig as exc:
            #  503 而不是 500：这不是 bug，是服务端没配 provider，运维动作明确
            raise HTTPException(status_code=503, detail=f"未配置 provider：{exc}") from exc
        session = _Session(
            session_id,
            agent,
            target,
            local,
            agent_ref=agent_ref,
            agent_config=agent_config,
            budget=budget,
            pricing=pricing,
        )
        #  绑定后 approver / sink 才真正生效。绑定前不可能有一轮在跑（会话还没
        #  返回给调用方），所以这个窗口安全——但 approver 仍 fail closed 兜底
        session.mcp_manager = manager
        session.on_reserve = lambda: save_reservation(session)
        session.execution_owner = require_state_owner()
        session.fleet_owner_check = lambda *, claim=False: fleet_owner_check(session, claim=claim)
        loop = asyncio.get_running_loop()
        ref.bind(session, loop)
        if getattr(agent, "chenshu", None) is not None:
            agent.chenshu.member_approver = _HttpApprover(ref)
        if getattr(agent, "_weave_controls", None) is not None:
            agent._weave_controls.item_approver = _HttpApprover(ref)
        #  通知是从工作线程、后台任务的 watcher 线程里投递的：转交给事件循环再动缓冲区
        agent.on_notification = lambda item: loop.call_soon_threadsafe(
            session.note_notification, item
        )
        if question_options is not None:
            try:
                serve_questions.attach(session, question_options)
            except Exception:
                if agent.session_log is not None:
                    agent.session_log.release()
                if manager is not None:
                    manager.close()
                raise
        sessions[session_id] = session
        SESSIONS_LIVE.set(len(sessions))
        return session

    def make_session(
        workspace: str = "",
        model: str = "",
        mode: str = "",
        agent: Any = None,
        budget: Any = None,
        fork_from: Any = None,
        questions: Any = None,
        creation_receipt: dict[str, str] | None = None,
        project_id: str = "",
        adopt_from: Any = None,
    ) -> _Session:
        """装配一个会话并登记进注册表。REST 的 POST /session 与 MCP 的 xiaoyu
        工具共用这一个装配口——两张脸建出的会话必须一模一样，包括 approver 与
        sink 的构造期注入（见 build_agent 的 docstring）。

        `agent` 非空时会话钉住那个 agent 的某一版配置，此时 model/mode 不能再
        单独给（400）：钉版本的意义就是"这个会话的配置可被完整复现"，旁路
        覆盖会让版本号失去含义。没给 agent 就是老路——跟随服务端启动参数，
        model/mode 可临时覆盖。

        `fork_from` 非空时新会话从另一个会话的历史起步（见 fork_history）：没另给
        workspace / agent / model / mode 就沿用来源会话的工作区与 agent 版本。
        预算不沿用——用量从零记起，要设预算在这次请求里给。

        `adopt_from` 非空时不复制：本服务取得那份终端日志的写锁，接着往**同一个
        文件**写（与具名会话的续写同一条路）。锁被别的进程持有时 423，调用方可稍后
        重试或改用 fork_from 另开分支。运行配置取当前服务或本次请求，不继承来源的。
        """
        require_state_owner()
        if adopt_from is not None and fork_from not in (None, ""):
            raise HTTPException(400, "adopt_from 与 fork_from 只能给一个")
        if adopt_from is not None:
            return adopt_session(
                adopt_from, workspace=workspace, model=model, mode=mode, agent=agent, budget=budget,
                questions=questions, creation_receipt=creation_receipt, project_id=project_id,
            )
        try:
            question_options = serve_questions.parse_options(questions, persist=cfg.persist)
        except question_types.ConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        catalog_source = None
        if isinstance(fork_from, dict) and "catalog_id" in fork_from:
            try:
                catalog_source, history = catalog.branch(fork_from)
            except serve_catalog.CatalogError as exc:
                raise HTTPException(status_code=exc.status, detail=str(exc)) from None
            source = None
            if not workspace:
                workspace = catalog_source["workspace"]
        else:
            source, history = fork_history(fork_from)
        if source is not None:
            if not workspace:
                workspace = str(source.workspace)
            if (agent is None or agent == "") and not model and not mode and source.agent_ref:
                agent = {"id": source.agent_ref["id"], "version": source.agent_ref["version"]}
        scope = {}
        if source is not None and not project_id and source.cfg.project_scope:
            scope = dict(source.cfg.project_scope)
            if workspace and Path(workspace).expanduser().resolve() != Path(scope["workspace"]):
                raise HTTPException(400, "分支对话保留原项目工作目录；请在目标项目新建对话")
        elif project_id:
            if cfg.project_resolver is None:
                raise HTTPException(400, "当前服务不支持项目")
            try:
                scope = cfg.project_resolver(project_id, workspace, None)
            except (ValueError, OSError) as exc:
                raise HTTPException(400, str(exc)) from None
        target = Path(scope["workspace"]) if scope else resolve_workspace(workspace)
        session_id = f"sess-{uuid.uuid4().hex[:16]}"
        agent_ref: dict[str, Any] | None = None
        agent_config: dict[str, Any] = {}
        if agent is not None and agent != "":
            if model or mode:
                raise HTTPException(
                    status_code=400, detail="指定了 agent 就不能再单独给 model / mode（配置以 agent 版本为准）"
                )
            try:
                record, version, agent_config = agents.resolve(agent)
            except NotFound as exc:
                raise HTTPException(status_code=404, detail=f"未知 agent {exc.args[0]!r}") from None
            except StateError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from None
            agent_ref = {"id": record["agent_id"], "name": record["name"], "version": version}
        try:
            session_budget = Budget.parse(budget)
        except StateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        #  会话级预算优先于 agent 默认预算（CMA 同此）；都没有就是无预算
        if session_budget is None and agent_config.get("budget"):
            session_budget = Budget.parse(agent_config["budget"])
        local = replace(apply_agent_config(agent_config, model=model, mode=mode), project_scope=scope)
        session = assemble(
            session_id,
            target,
            local,
            agent_ref=agent_ref,
            agent_config=agent_config,
            budget=session_budget,
            pricing=agent_config.get("pricing") or {},
            question_options=question_options,
        )
        session.creation_receipt = creation_receipt
        if catalog_source is not None:
            session.fork_origin = {key: catalog_source[key] for key in
                                   ("catalog_id", "revision", "title", "workspace")}
            session.fork_origin["kind"] = "cli"
            session.title = catalog_source["title"]
            try:
                # Copy only the replayable context. Do not call restore(source=...):
                # that would also adopt source child-run archive locations.
                session.async_agent.restore(history)
                log = session.agent.session_log
                if log is None:
                    raise OSError("Branch journal is unavailable")
                log.commit_event("resumed_from", source=f"catalog:{catalog_source['catalog_id']}",
                                 revision=catalog_source["revision"])
            except BaseException:
                close_session(session)
                raise
            session.publish("session.forked", source=catalog_source["catalog_id"], messages=len(history))
        if source is not None:
            session.fork_origin = {"kind": "session", "session_id": source.id}
            origin = source.agent.session_log
            #  复制进新会话自己的日志：新会话自包含，来源关掉、删掉都不影响它
            session.async_agent.restore(
                history, source=str(origin.path) if origin is not None else ""
            )
            session.publish("session.forked", source=source.id, messages=len(history))
        session.creation_saved = False
        manifests.save(session.manifest())
        session.creation_saved = True
        return session

    def adopt_session(
        reference: Any,
        *,
        workspace: str,
        model: str,
        mode: str,
        agent: Any,
        budget: Any,
        questions: Any,
        creation_receipt: dict[str, str] | None,
        project_id: str,
    ) -> _Session:
        """原地继续一份终端日志：先拿写锁，再核对文件没变，然后才读历史。

        顺序与 open_named / 重启恢复相同——读到的就是拿锁那一刻的完整文件。
        装配失败时：还没往文件里写过东西就只放锁；已经留了续写痕迹就按正常关闭
        收尾，让它重新以「已正常结束」的样子回到目录里。
        """
        if not isinstance(reference, dict):
            raise HTTPException(400, 'adopt_from 须为 {"catalog_id": ..., "revision": ...}')
        if agent is not None and agent != "":
            raise HTTPException(400, "原地继续不接受 agent：这场会话不是按某个 agent 版本建的")
        if project_id:
            raise HTTPException(400, "原地继续保留来源工作目录，不能另指定项目")
        try:
            question_options = serve_questions.parse_options(questions, persist=cfg.persist)
        except question_types.ConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        try:
            session_budget = Budget.parse(budget)
        except StateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

        def refuse(exc: serve_catalog.CatalogError) -> HTTPException:
            if exc.status == 423:
                return HTTPException(423, {"code": "writer_conflict", "message": str(exc)})
            return HTTPException(status_code=exc.status, detail=str(exc))

        try:
            item, path = catalog.adopt(reference)
        except serve_catalog.CatalogError as exc:
            raise refuse(exc) from None
        if workspace and Path(workspace).expanduser().resolve() != Path(item["workspace"]):
            raise HTTPException(400, "原地继续保留来源工作目录；要换目录请另开分支")
        target = resolve_workspace(item["workspace"])
        try:
            log = serve_ownership.StrictSessionLog(path)
        except SessionLockedError:
            #  目录探锁与这里拿锁之间被别的进程抢先打开了
            raise HTTPException(423, {"code": "writer_conflict",
                                      "message": "来源会话已在终端中打开，请先在那边结束它。"}) from None
        except OSError:
            raise HTTPException(503, {"code": "execution_owner_unavailable",
                                      "message": "会话日志无法取得严格写锁。"}) from None
        session: _Session | None = None
        try:
            try:
                catalog.confirm(path, reference["revision"])
            except serve_catalog.CatalogError as exc:
                raise refuse(exc) from None
            history = load_messages(path)
            local = apply_agent_config({}, model=model, mode=mode)
            session = assemble(
                f"sess-{uuid.uuid4().hex[:16]}",
                target,
                local,
                agent_ref=None,
                agent_config={},
                budget=session_budget,
                pricing={},
                session_log=log,
                question_options=question_options,
            )
        except BaseException:
            if session is None:
                log.release()
            raise
        try:
            session.creation_receipt = creation_receipt
            session.fork_origin = {"kind": "cli_adopted", "catalog_id": item["catalog_id"],
                                   "title": item["title"], "workspace": item["workspace"]}
            session.title = item["title"]
            #  续写点留痕：这之后的记录出自本服务（meta 记的仍是终端首次创建时的模型）
            log.event("reopened", version=app.version, model=local.model, messages=len(history))
            #  copy=False：历史就在这个文件里，再抄一遍等于把日志翻倍
            session.async_agent.restore(history, copy=False)
            timeline = serve_history.read(path)
            session.weaves.restore(path)
            session.weave_controls.restore(path)
            session.contests.restore(path)
            session.fleets.restore(path)
            session.fleet_controls.restore(path)
            session.runs = timeline["runs"]
            for run in session.runs:
                if run.get("status") == "running":
                    run["status"] = "interrupted"
            session.turns = max([0, *(int(run.get("turn") or 0) for run in session.runs)])
            session.publish("session.adopted", source=item["catalog_id"], messages=len(history))
            session.creation_saved = False
            manifests.save(session.manifest())
            session.creation_saved = True
        except BaseException:
            close_session(session)
            raise
        return session

    def fork_history(fork_from: Any) -> tuple[_Session | None, list[dict[str, Any]]]:
        """`fork_from` → (来源会话, 要带走的历史)；没给返回 (None, [])。

        接受会话 id，或 `{"session_id": ..., "turns": K}`（只带前 K 轮）。来源正在
        跑一轮时拒绝（409）：轮中的历史里有没配对的工具调用，抄走的是半截状态。
        """
        if fork_from is None or fork_from == "":
            return None, []
        if isinstance(fork_from, str):
            source_id, turns = fork_from, None
        elif isinstance(fork_from, dict) and isinstance(fork_from.get("session_id"), str):
            source_id, turns = fork_from["session_id"], fork_from.get("turns")
        else:
            raise HTTPException(
                status_code=400, detail='fork_from 要么是会话 id，要么是 {"session_id": ..., "turns": K}'
            )
        source = pick(source_id)
        if source.busy:
            raise HTTPException(
                status_code=409, detail="来源会话正在跑一轮，等它结束或 /abort 之后再分叉"
            )
        origin = source.agent.session_log
        if origin is not None and origin.path.is_file():
            #  与重启恢复同一条读法（压缩、清空都按日志里记的重放）
            history: list[dict[str, Any]] = load_messages(origin.path)
        else:
            history = copy.deepcopy(source.agent.messages[1:])  # [0] 是 system
        if turns is not None:
            from .agent import SYNTHETIC_USER_TEXTS

            starts = turn_starts(history, SYNTHETIC_USER_TEXTS)
            if isinstance(turns, bool) or not isinstance(turns, int) or not 1 <= turns <= len(starts):
                raise HTTPException(
                    status_code=400,
                    detail=f"fork_from.turns 超出范围：来源会话有 {len(starts)} 轮，可取 1 到 {len(starts)}",
                )
            if turns < len(starts):
                #  切片会丢掉读回时的损坏记录，带上它，restore 才能照常提示
                history = LoadedMessages(
                    history[: starts[turns]], getattr(history, "corrupt_lines", ())
                )
        return source, history

    def restore_sessions() -> None:
        """启动时按清单把上次的会话接回来（历史来自会话日志，游标接着编号）。

        恢复失败的清单**留在盘上、打一行警告、跳过**：不删是因为失败可能是
        暂时的（工作区没挂上、provider 没配），删了历史就真没了。
        """
        for manifest in manifests.load_all():
            session_id = manifest["session_id"]
            log_path = Path(manifest.get("session_log") or "")
            target = Path(manifest.get("workspace") or "")
            if not log_path.is_file() or not target.is_dir():
                recovery_errors.append({"session_id": session_id, "code": "restore_source_unavailable",
                                        "reason": "会话日志或工作区不可用；原清单保留。"})
                print(
                    f"serve: 跳过会话 {session_id}（会话日志或工作区不在：{log_path} / {target}）",
                    file=sys.stderr,
                )
                continue
            log: SessionLog | None = None
            try:
                agent_config = dict(manifest.get("agent_config") or {})
                local = apply_agent_config(agent_config)
                scope = manifest.get("project_scope")
                if scope:
                    if cfg.project_resolver is None:
                        raise ValueError("当前宿主无法恢复项目范围")
                    scope = cfg.project_resolver(scope["project_id"], str(target), scope)
                    local = replace(local, project_scope=scope)
                #  先抢写锁再读历史（与 open_named 同序）：别的进程正续写这个会话时
                #  SessionLockedError 走下面的"跳过"，清单留盘，下次启动再接
                log = serve_ownership.StrictSessionLog(log_path)
                history = load_messages(log_path)
                session = assemble(
                    session_id,
                    target,
                    local,
                    agent_ref=manifest.get("agent"),
                    agent_config=agent_config,
                    budget=Budget.parse(manifest.get("budget")),
                    pricing=check_pricing(manifest.get("pricing")),
                    session_log=log,
                    question_options=serve_questions.parse_options(manifest.get("questions"), persist=cfg.persist),
                )
            except Exception as exc:  # noqa: BLE001 - 一个坏清单不能拖死整个服务
                if log is not None:
                    log.release()  # 装配中途失败：锁不能占到进程退出，别的进程还要接这个会话
                recovery_errors.append({"session_id": session_id, "code": "restore_unavailable",
                                        "reason": "未能取得会话独占写锁或恢复会话；原清单保留，请核对后重启服务进程。"})
                print(f"serve: 恢复会话 {session_id} 失败，已跳过（{type(exc).__name__}）", file=sys.stderr)
                sessions.pop(session_id, None)
                SESSIONS_LIVE.set(len(sessions))
                continue
            session.next_model = manifest.get("next_model", "")
            log.event("reopened", version=app.version, model=local.model, messages=len(history))
            #  copy=False：历史就在这个文件里，再抄一遍等于每次重启都把日志翻倍
            session.async_agent.restore(history, copy=False)
            #  预算是按会话累计卡的，账本却只活在内存里：从日志最后一条 usage
            #  事件（轮末写的累计快照）接上，否则花超的会话重启一次就又能提交。
            #  中途被杀的那一轮没来得及记账——少算不到一轮，不会多算
            recorded = last_usage(log_path)
            if recorded:
                session.agent.usage.absorb(recorded.get("by_model"))
                session.budget_reason = session.check_budget()
            #  游标接着编号：重启前的事件计入 dropped，客户端手里的 from 仍单调。
            #  从预留线接——上个进程发出去的序号都在它之下；优雅停机时它就是实际水位，
            #  中途被杀时它比实际水位高出不到一个块，序号跳过一段但绝不回头
            resume_seq = max(
                1, int(manifest.get("next_seq") or 1), int(manifest.get("seq_reserved") or 0)
            )
            session.next_seq = session.first_seq = session.seq_reserved = resume_seq
            session.dropped = resume_seq - 1
            session.turns = int(manifest.get("turns") or 0)
            session.created_at = float(manifest.get("created_at") or session.created_at)
            saved_updated_at = float(manifest.get("updated_at") or session.created_at)
            timeline = serve_history.read(log_path)
            session.weaves.restore(log_path)
            session.weave_controls.restore(log_path)
            session.contests.restore(log_path)
            session.fleets.restore(log_path)
            session.fleet_controls.restore(log_path)
            session.runs = timeline["runs"]
            session.turns = max([session.turns, *(int(run.get("turn") or 0) for run in session.runs)])
            session.history_warning = str(manifest.get("history_warning") or "")
            first_text = next((m["content"] for m in timeline["messages"] if m["role"] == "user"), "新任务")
            session.title = str(manifest.get("title") or " ".join(first_text.split())[:80])
            session.fork_origin = manifest.get("fork_origin")
            settled = [run for run in session.runs if run.get("status") != "running"]
            if settled:
                session.last_result = settled[-1].get("result")
                session.error = settled[-1].get("error") or ""
                saved_updated_at = max(saved_updated_at, settled[-1].get("finished_at") or 0)
            session.creation_receipt = manifest.get("creation_receipt")
            session.submission_receipts = dict(manifest.get("submission_receipts") or {})
            #  清单里还挂着在途标记 = 那一轮没走到收尾进程就没了。它做到哪一步、
            #  副作用落没落地，服务端说不清——编排方必须知道，不能当成"接回来了"
            lost = manifest.get("in_flight") or manifest.get("lost_turn")
            # A durable settlement wins over a stale in-flight manifest.
            if isinstance(lost, dict) and any(r.get("id") == lost.get("id") for r in settled):
                lost = None
            session.lost_turn = lost if isinstance(lost, dict) else None
            for run in session.runs:
                if run.get("status") == "running":
                    run["status"] = "interrupted_by_restart"
                    run["error"] = "服务中断，未记录可靠结算；请检查已有成果后再继续。"
            session.detail = "interrupted_by_restart" if session.lost_turn else "recovered"
            session.publish(
                "session.recovered", messages=len(history), lost_turn=session.lost_turn
            )
            session.updated_at = saved_updated_at
            if session.lost_turn:
                session.status = "idle"
            elif settled:
                last = settled[-1]
                session.status = "error" if last.get("status") == "failed" else "idle"
                session.detail = {"completed": "recovered", "failed": "failed",
                                  "interrupted": "interrupted",
                                  "stopped": (last.get("result") or {}).get("stopped", "stopped")}.get(last.get("status"), "recovered")
            manifests.save(session.manifest())

    def close_session(session: _Session) -> None:
        """摘除一个会话：先打断，再把挂着的审批放掉（否则工作线程堵到
        approval_timeout），最后出注册表。REST DELETE 与 MCP xiaoyu_close 共用。"""
        session.closing = True
        if getattr(session.agent, "chenshu", None) is not None:
            session.agent.chenshu.stop_members()
        if session.questions is not None:
            session.question_closing = True
        session.interrupt_owned()
        if session.questions is not None:
            session.questions._wake()
        for request in session.snapshot_pending():
            request.verdict = Deny("会话已被关闭")
            request.done.set()
        closing_sessions.append(session)
        sessions.pop(session.id, None)
        SESSIONS_LIVE.set(len(sessions))
        if session.execution_owner.held:
            manifests.delete(session.id)
        if session.bridge is not None:
            bridge, session.bridge = session.bridge, None
            #  close() 自己先 detach（注销工具、放掉在途调用）再发 bye、关连接；
            #  这里在事件循环上，排个任务即可，不阻塞关会话
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(bridge.close("session closed"))
        def release_resources():
            try:
                if session.questions is not None:
                    session.questions._cancel_pending()
                    session.questions._changes.close()
            finally:
                if session.agent.session_log:
                    if session.owner_uncertain or work_pending(session):
                        session.execution_owner.retain(session.agent.session_log)
                    else:
                        session.agent.session_log.close("closed")
                if session.mcp_manager is not None:
                    session.mcp_manager.close()
                if session in closing_sessions:
                    closing_sessions.remove(session)

        runtime = getattr(session.agent, "chenshu", None)
        has_members = runtime is not None and any(thread.is_alive() for thread in list(runtime._threads.values()))
        if (session.questions is None and not has_members and not work_pending(session)
                and getattr(session, "connection_close", None) is None):
            session.events_closed = True
            release_fleet_claim(session)
            release_resources()
        else:
            async def finish():
                try:
                    if session.questions is not None and session._task is not None:
                        await asyncio.shield(session._task)
                    await drain_members(session)
                    await drain_connections(session)
                finally:
                    try:
                        await asyncio.get_running_loop().run_in_executor(app.state.question_pool, release_resources)
                    except Exception as exc:
                        print(f"serve: question cleanup failed ({type(exc).__name__})", file=sys.stderr)
            cleanup = asyncio.create_task(finish())
            question_cleanup.add(cleanup)
            cleanup.add_done_callback(question_cleanup.discard)

    def delete_idle_sessions(ids: list[str]) -> dict[str, Any]:
        """Private host operation: delete owned idle histories, preserving workspace files."""
        require_state_owner()
        targets = [sessions[sid] for sid in ids if sid in sessions]
        # Validate the entire selection before touching any history. No await between
        # validation and removal: new submissions cannot enter on this event loop.
        for session in targets:
            if (session.closing or session.owner_uncertain or session.busy
                    or getattr(session, "management_busy", False) or work_pending(session)
                    or session.snapshot_pending()):
                raise HTTPException(409, {"code": "session_delete_busy"})
            if not getattr(session.agent.session_log, "locked", False):
                raise HTTPException(409, {"code": "session_delete_unowned"})
        deleted = []
        for session in targets:
            try:
                manifests.delete(session.id)
                try:
                    #  原地继续的会话，日志同时是终端那边的历史：只交还，不删文件
                    if (session.fork_origin or {}).get("kind") != "cli_adopted":
                        session.agent.session_log.delete_history()
                except OSError:
                    manifests.save(session.manifest())
                    raise
            except OSError:
                return {"deleted": deleted, "incomplete": True}
            close_session(session)
            deleted.append(session.id)
        return {"deleted": deleted, "incomplete": False}

    app.state.delete_idle_sessions = delete_idle_sessions

    def earlier_submission(session: _Session, key: str, text: str) -> dict[str, Any] | None:
        """带着同一个幂等键的上一次提交；没有返回 None。

        编排器等不到响应就会重发（同步端点最常见：它自己的 HTTP 超时先到）。那一轮
        其实已经跑完、或还在跑——再开一轮就是把同一件事做两遍，副作用也落两遍。
        """
        if not key:
            return None
        if len(key) > IDEMPOTENCY_KEY_MAX:
            raise HTTPException(
                status_code=400, detail=f"Idempotency-Key 太长（上限 {IDEMPOTENCY_KEY_MAX} 字符）"
            )
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        record = session.submissions.get(key)
        if record is not None:
            if record["digest"] != digest:
                raise HTTPException(
                    status_code=409,
                    detail="这个 Idempotency-Key 已经用在另一条指令上了；新的指令请换一个键",
                )
            return record
        lost = session.lost_turn or {}
        if lost.get("key") == key:
            #  服务重启前这个键对应的那一轮没跑完。做到哪一步、副作用落没落地说不清，
            #  悄悄重跑是替调用方做了一个它没同意的决定
            raise HTTPException(
                status_code=409,
                detail=(
                    f"带着这个 Idempotency-Key 的第 {lost.get('turn')} 轮在服务重启前没跑完，"
                    "不自动重跑。先看 /status 与会话历史确认它做到了哪一步；"
                    "确定要重做就换一个键提交"
                ),
            )
        if key in session.submission_receipts:
            receipt = session.submission_receipts[key]
            if receipt["digest"] != digest:
                raise HTTPException(status_code=409, detail="这个幂等键已用于其他指令")
            # 回执证明已经接收过，不把丢失的结果伪装成成功，也绝不重跑。
            raise HTTPException(status_code=409, detail={"code": "submission_recorded",
                "turn": receipt["turn"], "message": "这次提交已被记录；结果不在内存中，请核对会话状态和成果。不会重复执行。"})
        return None

    def remember_submission(session: _Session, key: str, text: str, task: asyncio.Task) -> None:
        if not key:
            return
        record: dict[str, Any] = {
            "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
            "turn": session.turns + 1,
            "task": task,
            "result": None,
        }
        session.submissions[key] = record

        def settle(done: asyncio.Task) -> None:
            record["task"] = None
            if not done.cancelled() and done.exception() is None:
                record["result"] = done.result()

        task.add_done_callback(settle)
        finished = [name for name, item in session.submissions.items() if item["task"] is None]
        for name in finished[: max(0, len(session.submissions) - IDEMPOTENCY_KEEP)]:
            session.submissions.pop(name, None)

    async def start_turn(
        session: _Session,
        text: str,
        output_schema: dict[str, Any] | None = None,
        key: str = "",
        command: serve_commands.Prepared | serve_fleet_commands.Prepared | None = None,
    ) -> asyncio.Task:
        """开一轮。已经在跑就 409——同步架构下同一 Agent 一次只能跑一轮，
        默默排队会让编排器以为自己的第二次提交立刻生效了。"""
        serve_ownership.require_write(session)
        if session.busy:
            raise HTTPException(status_code=409, detail="该会话正在跑一轮，先等它结束或 /abort")
        if session.questions is not None:
            try:
                session.questions._check()
            except question_types.QuestionError:
                raise HTTPException(status_code=503, detail="Question storage is unavailable; restart and reconcile") from None
        reason = session.check_budget()
        if reason:
            session.budget_reason = reason
            raise HTTPException(
                status_code=409,
                detail=f"预算已耗尽：{reason}。POST /session/{session.id}/budget 调高或撤掉预算后再继续",
            )
        log = session.agent.session_log
        in_flight = {"id": f"run-{uuid.uuid4().hex}", "turn": session.turns + 1,
                     "started_at": time.time(), "live_from_seq": session.next_seq,
                     "log_offset": log.path.stat().st_size if log and log.path.exists() else 0}
        run = {"id": in_flight["id"], "turn": in_flight["turn"], "status": "running",
               "model": getattr(session, "next_model", "") or session.agent.config.model,
               "started_at": in_flight["started_at"], "finished_at": None,
               "result": None, "error": ""}
        if command is not None:
            in_flight["command"] = copy.deepcopy(command.command)
            run["command"] = copy.deepcopy(command.command)
        label = text
        if command is not None:
            if command.command["kind"] == "chenshu":
                label = command.label
            else:
                title_kind = "斗巧" if command.command["kind"] == "douqiao" else "七襄"
                label = f"{title_kind}：{command.command['spec']} · {command.preview['count']} 项"
        receipts = dict(session.submission_receipts)
        if key:
            in_flight["key"] = key
            receipts[key] = {
                "digest": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
                "turn": session.turns + 1,
            }
        #  先落盘再切换状态或调模型；写失败不能留下永远 busy 的幽灵轮次。
        title = (" ".join(label.split())[:80] or "新任务") if session.title == "新任务" else session.title
        manifest = {**session.manifest(), "in_flight": in_flight, "title": title,
                    "updated_at": in_flight["started_at"],
                    "lost_turn": None, "submission_receipts": receipts}
        reserved = False
        try:
            manifests.save(manifest)
            reserved = True
            if log is not None:
                log.commit_event(serve_history.START, run=run)
        except OSError:
            # A manifest may have committed before the journal failed. Retain its
            # receipt so this process is as conservative as restart recovery.
            if reserved:
                session.submission_receipts = receipts
            session.history_warning = "会话回执无法可靠保存，本轮未启动。"
            raise HTTPException(status_code=503, detail="会话回执无法保存，本轮未启动") from None
        session.runs.append(run)
        session.title = title
        session.last_result = None
        # Admission already rejected unresolved prior execution ownership.
        # A successful previous delegation must not taint this new plain turn.
        session.owner_fanout = False
        session.busy = True
        session.status = "running"
        session.detail = "working"
        session.error = ""
        session.lost_turn = None
        session.in_flight = in_flight
        session.submission_receipts = receipts
        if command is not None and command.command["kind"] in {"qixiang", "douqiao"}:
            store = session.agent.subagent_runs
            with store.lock:
                # Keep the validated selections newest. Qixiang reserves at
                # least twice its item count, so child-result eviction cannot
                # replace a not-yet-started selection with a fresh disk read.
                for archive_id, archive in command.archives.items():
                    store.pop(archive_id, None)
                    store[archive_id] = archive
                store.capacity = max(store.capacity, len(store), 2 * command.preview["count"] + 8)
        session.publish("run.started", prompt=label, model=run["model"], previous_model=session.agent.config.model)
        task = asyncio.create_task(_drive(session, label, output_schema, command))
        session._task = task
        remember_submission(session, key, text, task)
        return task

    async def _drive(
        session: _Session, text: str, output_schema: dict[str, Any] | None = None,
        command: serve_commands.Prepared | serve_fleet_commands.Prepared | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        run = session.runs[-1]
        runtime = None
        original_guard = None
        try:
            if cfg.skill_policy is not None:
                disabled = tuple(cfg.skill_policy())
                if disabled != session.agent.config.disabled_skill_names:
                    previous_disabled = session.agent.config.disabled_skill_names
                    session.agent.config.disabled_skill_names = disabled
                    try:
                        session.agent.reload_skills()
                    except Exception:
                        session.agent.config.disabled_skill_names = previous_disabled
                        raise
            # A saved choice applies only at a new turn boundary, never to an in-flight reply.
            target_model = run.get("model")
            if target_model and target_model != session.agent.config.model:
                session.agent.registry.resolve(target_model)
                session.agent.switch_model(target_model)
                session.agent_config = {**session.agent_config, "model": target_model}
            if getattr(session, "next_model", "") == target_model:
                session.next_model = ""
                manifests.save(session.manifest())
            if command is not None and command.command["kind"] == "chenshu":
                # Never replace a guard while a cancelled prior worker is still settling.
                await session.async_agent._settle_previous()
                runtime = session.agent.chenshu
                original_guard = runtime.host_guard
                runtime.host_guard = command.guard
            stream = (session.async_agent._stream_host_tool(
                        command.tool_name if command.command["kind"] == "chenshu" else command.command["kind"], command.args, text)
                      if command is not None else session.async_agent.stream(text, output_schema))
            async for event in stream:
                if isinstance(event, RunCompleted):
                    if command is not None and event.result.stopped == "done" and not event.result.interrupted:
                        if command.command["kind"] == "douqiao":
                            contests = [item for item in session.contests.contests.values() if item.get("run_id") == run["id"]]
                            if not contests or any(not item.get("final") or item.get("cancelled")
                                    or item["judge"].get("status") != "completed"
                                    or any(candidate.get("status") != "completed" for candidate in item["candidates"])
                                    for item in contests):
                                event = replace(event, result=replace(event.result, stopped="contest_incomplete"))
                        elif command.command["kind"] == "qixiang":
                            batches = [batch for batch in session.weaves.batches.values() if batch.get("run_id") == run["id"]]
                            if not batches or any(not batch.get("final") or any(item.get("status") != "completed" for item in batch["items"]) for batch in batches):
                                event = replace(event, result=replace(event.result, stopped="batch_incomplete"))
                    result = asdict(event.result)
                    session.last_result = result
                session.publish_event(event)
                #  预算是**硬**闸：一轮里模型可能调用几十次，不能等轮末再算。
                #  每个事件后看一眼累计用量，越线就 interrupt——下一个 chunk 边界
                #  收尾，半截话入历史，与 /abort 同一条路
                if not session.budget_reason:
                    reason = session.check_budget()
                    if reason:
                        session.budget_reason = reason
                        session.publish("budget.reached", reason=reason)
                        if result is None:
                            session.interrupt_owned()
                        if session.questions is not None:
                            session.questions._wake()
        except asyncio.CancelledError:
            session.owner_uncertain = True
            session.detail = "interrupted"
            run["status"] = "interrupted"
            run["error"] = "服务停止，本轮执行已中断。"
            raise
        except Exception as exc:  # noqa: BLE001 - 编排方要结构化错误，不要 traceback
            if session.owner_fanout:
                session.owner_uncertain = True
            session.status = "error"
            session.detail = "failed"
            session.error = f"{type(exc).__name__}: {exc}"
            run["status"] = "failed"
            run["error"] = session.error
            session.publish("run.failed", error=session.error)
            return {"error": session.error}
        else:
            # RunCompleted is emitted only after the actual parent send_task
            # returns. A plain turn can therefore safely resume after stopping.
            # Delegation may leave child executors beyond that parent's return.
            fleet = getattr(session.agent, "chenshu", None)
            fleet_live = fleet is not None and any(thread.is_alive() for thread in list(fleet._threads.values()))
            if result is not None and not session.owner_fanout and not fleet_live:
                session.owner_uncertain = False
            elif (result or {}).get("interrupted"):
                session.owner_uncertain = True
            session.status = "idle"
            session.detail = "interrupted" if (result or {}).get("interrupted") else "finished"
            #  轮末再结一次账：最后一次模型调用的用量可能在最后一个事件之后才记上
            if not session.budget_reason:
                session.budget_reason = session.check_budget()
                #  软预算：模型在硬闸之前自己收了尾（RunResult.stopped=budget）——
                #  同样算到线，下一轮要先调预算；硬闸没越线不等于还能继续干
                if not session.budget_reason and (result or {}).get("stopped") == "budget":
                    session.budget_reason = "token 预算即将用尽，模型已按预算收尾"
                if session.budget_reason:
                    session.publish("budget.reached", reason=session.budget_reason)
            if session.budget_reason:
                session.detail = "budget_reached"
            stopped = (result or {}).get("stopped", "done")
            run["status"] = ("interrupted" if (result or {}).get("interrupted") else
                             "completed" if stopped == "done" else "stopped")
            if run["status"] == "stopped":
                run["error"] = f"本轮已停止：{stopped}；请检查成果并决定是否继续。"
                if session.detail == "finished":
                    session.detail = stopped
            return result or {}
        finally:
            if runtime is not None:
                def restore_guard(_done=None):
                    if runtime.host_guard is command.guard:
                        runtime.host_guard = original_guard
                worker = session.async_agent._task
                if worker is not None and not worker.done():
                    worker.add_done_callback(restore_guard)
                else:
                    restore_guard()
            run["finished_at"] = time.time()
            run["result"] = result
            session.turns += 1
            session.updated_at = run["finished_at"]
            try:
                artifacts = (result or {}).get("artifacts") or []
                if artifacts:
                    # Shutdown can close the executor before a final model
                    # result reaches this task. Only scheduling failure is
                    # downgraded; a running capture's exceptions still surface.
                    try:
                        capture = asyncio.get_running_loop().run_in_executor(
                            None, artifact_views.capture_session, session, run)
                    except RuntimeError as exc:
                        if not any(word in str(exc).lower() for word in ("shutdown", "shut down")):
                            raise
                        note = "服务关闭时未能保存成果快照；当前文件须重新校验，历史差异暂不可用。"
                        run["artifact_previews"] = [
                            {"id": item.get("id"), "path": item.get("path"),
                             "workspace": str(session.workspace), "note": note,
                             "delivered": {"available": False, "note": note},
                             "before": {"available": False, "note": note}}
                            for item in artifacts if isinstance(item, dict)]
                    else:
                        await capture
                if session.agent.session_log is not None:
                    session.agent.session_log.commit_event(serve_history.SETTLED, run=run)
                session.in_flight = None
                if sessions.get(session.id) is session:
                    if session.execution_owner.held:
                        manifests.save(session.manifest())
            except OSError:
                session.history_warning = "执行已停止，但结算保存失败；请检查成果，勿直接重复提交。"
                session.status = "error"
                session.detail = "persistence_failed"
                session.error = session.history_warning
                # Keep the in-flight marker when the settlement was not acknowledged.
                # Its idempotency receipt prevents a blind retry after restart.
            finally:
                session.busy = False
            #  会话若在跑的过程中被 DELETE，挂起的审批要有人收尸，否则工作线程
            #  一直阻塞到 approval_timeout。这里统一兜底放拒绝。
            for request in session.snapshot_pending():
                if request.identity:
                    continue
                request.verdict = Deny("会话已结束")
                request.done.set()

    # ---------- 路由 ----------

    @app.middleware("http")
    async def count_in_flight(request, call_next):
        with REQUESTS_IN_FLIGHT.track():
            return await call_next(request)

    @app.get(
        "/diagnostics",
        summary="进程自诊断：RSS / 线程 / fd + 各子系统在册计量（会话、请求、MCP 连接、后台任务）",
        tags=["system"],
        dependencies=guard,
        operation_id="diagnostics",
    )
    async def diagnostics_report() -> dict[str, Any]:
        #  与 /health 不同：这里有 token 门——进程仪表是运维视角的数据，只给持令牌者
        body = diagnostics.report()
        body["uptime_s"] = body["process"].pop("uptime_s")
        return body

    @app.get("/health", summary="存活探针", tags=["system"], operation_id="health")
    async def health(
        authorization: str = Header(default=""),
        x_xiaoyu_token: str = Header(default=""),
    ) -> dict[str, Any]:
        #  探针本身不要 token（负载均衡 / 容器编排的存活检查带不了凭据），但配了
        #  token 的服务多半暴露在回环之外：工作区路径、状态目录、审批档这些是
        #  部署细节，只回给对得上 token 的调用方，匿名探针只拿到"活着"和版本
        if not header_token_ok(authorization, x_xiaoyu_token):
            return {"ok": True, "version": app.version}
        return {
            "ok": True,
            "version": app.version,
            "root": str(cfg.root),
            "instance_id": instance_id,
            "desktop_protocol": 1,
            "history_protocol": 1,
            "catalog_protocol": 1 if catalog.supported else 0,
            "weave_protocol": 1,
            "weave_control_protocol": 1,
            "weave_command_protocol": 1,
            "contest_protocol": 1,
            "fleet_protocol": 1,
            "fleet_control_protocol": 1,
            "fleet_command_protocol": 1,
            "fleet_review_protocol": 1,
            "artifact_protocol": 1,
            "artifact_image_protocol": 1,
            "sessions": len(sessions),
            "agents": len(agents.list()),
            "approval": cfg.approval,
            "environment": cfg.environment,
            "persist": cfg.persist,
            "state_dir": str(state_dir) if cfg.persist else "",
            "execution_owner_protocol": 1,
            "recovery_errors": list(recovery_errors),
            "shutdown_requires_process_exit": any(s.owner_uncertain for s in sessions.values()) or app.state.execution_owner.retained,
            "ownership_note": serve_ownership.RETAINED_REASON if (any(s.owner_uncertain for s in sessions.values()) or app.state.execution_owner.retained) else "",
        }

    # ---------- 控制面：agent 对象 ----------

    @app.post(
        "/agent",
        summary="新建 agent（持久化、版本化的配置对象）",
        tags=["agent"],
        dependencies=guard,
        operation_id="create_agent",
        description=(
            "把 model / mode / effort / append_system_prompt / approval / sandbox / budget / pricing "
            "打包成一个可引用的对象；POST /session 用 agent 字段引用它。"
            "approval / sandbox 只能比服务端启动参数更严。"
        ),
    )
    async def agent_create(
        name: str = Body(embed=True, description="给人看的名字，不要求唯一"),
        config: dict[str, Any] = Body(
            default_factory=dict,
            embed=True,
            description=(
                "model / base_url / mode / effort / approval / append_system_prompt / sandbox / "
                "sandbox_network / budget{tokens,usd} / pricing{模型:{input,output}}（美元/百万 token）"
                " / mcp_servers{名:{command,args,env}|{url,headers}}（宿主自有 MCP server，"
                "同 .mcp.json 的 mcpServers；需服务端 --agent-mcp 开放）"
            ),
        ),
    ) -> dict[str, Any]:
        require_state_owner()
        try:
            check_tightening(config)
            record = agents.create(name, config)
        except StateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return public_agent(record)

    @app.get("/agent", summary="列出 agent", tags=["agent"], dependencies=guard, operation_id="list_agents")
    async def agent_list() -> dict[str, Any]:
        return {"agents": [public_agent(item) for item in agents.list()]}

    @app.get(
        "/agent/{agent_id}",
        summary="agent 详情",
        tags=["agent"],
        dependencies=guard,
        operation_id="get_agent",
    )
    async def agent_get(
        agent_id: str,
        versions: bool = Query(default=False, description="true=带全部历史版本"),
    ) -> dict[str, Any]:
        return public_agent(pick_agent(agent_id), with_versions=versions)

    @app.post(
        "/agent/{agent_id}",
        summary="更新 agent → 新版本",
        tags=["agent"],
        dependencies=guard,
        operation_id="update_agent",
        description="config 里给的键覆盖、没给的沿用；每次更新版本号 +1。已在跑的会话仍钉着创建时的版本。",
    )
    async def agent_update(
        agent_id: str,
        config: dict[str, Any] = Body(default_factory=dict, embed=True),
        name: str | None = Body(default=None, embed=True),
    ) -> dict[str, Any]:
        require_state_owner()
        record = pick_agent(agent_id)
        try:
            check_tightening({**record["config"], **config})
            record = agents.update(agent_id, config, name=name)
        except StateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        return public_agent(record)

    @app.delete(
        "/agent/{agent_id}",
        summary="归档 agent（只读、不再接受新会话）",
        tags=["agent"],
        dependencies=guard,
        operation_id="archive_agent",
    )
    async def agent_archive(agent_id: str) -> dict[str, Any]:
        require_state_owner()
        pick_agent(agent_id)
        return public_agent(agents.archive(agent_id))

    @app.get("/catalog/sessions", tags=["session"], dependencies=guard,
             operation_id="list_catalog_sessions", summary="发现当前工作范围内的终端会话")
    async def catalog_sessions(cursor: int = Query(default=0, ge=0),
                               limit: int = Query(default=100, ge=1, le=200)):
        return await asyncio.to_thread(catalog.list, cursor, limit)

    @app.get("/catalog/sessions/{catalog_id}/history", tags=["session"], dependencies=guard,
             operation_id="get_catalog_history", summary="只读查看终端会话正文")
    async def catalog_history(catalog_id: str, cursor: int = Query(default=0, ge=0),
                              limit: int = Query(default=100, ge=1, le=500)):
        try:
            return await asyncio.to_thread(catalog.history, catalog_id, cursor, limit)
        except serve_catalog.CatalogError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None

    @app.post(
        "/session",
        summary="新建会话",
        tags=["session"],
        dependencies=guard,
        operation_id="create_session",
    )
    async def session_create(
        idempotency_key: str = Header(default=""),
        project_id: str = Body(default="", embed=True, description="宿主登记的项目；缺省保持原工作区行为"),
        workspace: str = Body(default="", embed=True, description="工作区路径，须在 root 之内；缺省用 root"),
        model: str = Body(default="", embed=True, description="模型名，缺省跟随服务端配置"),
        mode: str = Body(default="", embed=True, description="default / auto / plan"),
        agent: str | dict[str, Any] | None = Body(
            default=None,
            embed=True,
            description='引用一个 agent：agent_id（最新版本）或 {"id": ..., "version": n}（钉版本）。给了它就不能再给 model/mode',
        ),
        budget: dict[str, Any] | None = Body(
            default=None,
            embed=True,
            description="本会话的硬预算 {tokens, usd}；缺省用 agent 的默认预算；usd 需要 agent 带 pricing",
        ),
        questions: dict[str, Any] | None = Body(default=None, embed=True,
            description="显式启用持久化问答：{} 或 {foreground_timeout_seconds: 60}；须启用 persist"),
        fork_from: str | dict[str, Any] | None = Body(
            default=None,
            embed=True,
            description=(
                '服务内分支用会话 id 或 {"session_id": ..., "turns": K}。'
                '终端会话分支用 {"catalog_id": ..., "revision": ...}，只复制上下文，不继承源配置或授权。'
                "来源会话不受影响；终端分支默认使用来源工作区，运行配置取当前服务或显式请求。"
            ),
        ),
        adopt_from: dict[str, Any] | None = Body(
            default=None,
            embed=True,
            description=(
                '原地继续一段终端会话：{"catalog_id": ..., "revision": ...}。不复制历史，'
                "本服务取得该日志的写锁并续写同一个文件；被其他进程占用时返回 423 "
                "（code=writer_conflict），可稍后重试或改用 fork_from 另开分支。"
            ),
        ),
    ) -> dict[str, Any]:
        key = idempotency_key.strip()
        if len(key) > IDEMPOTENCY_KEY_MAX:
            raise HTTPException(status_code=400, detail="Idempotency-Key 太长")
        receipt = None
        if key:
            body = {"workspace": workspace, "model": model, "mode": mode, "agent": agent,
                    "budget": budget, "fork_from": fork_from, "questions": questions}
            if project_id:
                body["project_id"] = project_id
            if adopt_from is not None:
                body["adopt_from"] = adopt_from
            digest = hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
            receipt = {"key": key, "digest": digest}
            # 读取磁盘也覆盖暂时无法恢复的会话；不能在恢复失败后重复创建。
            records = [item.manifest() for item in sessions.values()] + manifests.load_all()
            for record in records:
                previous = record.get("creation_receipt") or {}
                if previous.get("key") != key:
                    continue
                if previous.get("digest") != digest:
                    raise HTTPException(status_code=409, detail="这个幂等键已用于其他会话配置")
                existing = sessions.get(record["session_id"])
                if existing is None:
                    raise HTTPException(status_code=409, detail="会话已创建但尚未恢复，请先检查服务状态")
                if not getattr(existing, "creation_saved", True):
                    serve_ownership.require_write(existing)
                    try:
                        manifests.save(existing.manifest())
                    except OSError:
                        raise HTTPException(503, "会话回执无法保存") from None
                    existing.creation_saved = True
                return {**existing.info_dict(), "duplicate": True}
        try:
            return make_session(
                workspace=workspace, model=model, mode=mode, agent=agent, budget=budget,
                fork_from=fork_from, questions=questions, creation_receipt=receipt, project_id=project_id,
                adopt_from=adopt_from,
            ).info_dict()
        except question_types.SessionStorageError:
            raise HTTPException(status_code=503, detail="Question storage is unavailable") from None
        except OSError:
            raise HTTPException(status_code=503, detail="会话回执无法保存") from None

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
        output_schema: dict[str, Any] | None = Body(
            default=None,
            embed=True,
            description="JSON Schema：要求本轮以符合它的对象收尾，结果在 result.output（模型没给则 null）",
        ),
        idempotency_key: str = Header(
            default="", description="重发同一次提交时带同一个键：不会再跑一轮，拿回的是那一轮的结果"
        ),
    ) -> dict[str, Any]:
        session = pick(session_id)
        key = idempotency_key.strip()
        if (earlier := earlier_submission(session, key, text)) is not None:
            task = earlier["task"]
            result = await task if task is not None else earlier["result"]
            return {
                "session_id": session_id, **session.status_dict(),
                "result": result, "duplicate": True, "turn": earlier["turn"],
            }
        task = await start_turn(session, text, output_schema, key)
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
        output_schema: dict[str, Any] | None = Body(
            default=None,
            embed=True,
            description="JSON Schema：要求本轮以符合它的对象收尾，结果在 result.output（模型没给则 null）",
        ),
        idempotency_key: str = Header(
            default="", description="重发同一次提交时带同一个键：不会再跑一轮"
        ),
    ) -> dict[str, Any]:
        session = pick(session_id)
        key = idempotency_key.strip()
        if (earlier := earlier_submission(session, key, text)) is not None:
            return {
                "session_id": session_id, "accepted": True, **session.status_dict(),
                "duplicate": True, "turn": earlier["turn"],
            }
        await start_turn(session, text, output_schema, key)
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

    @app.get("/session/{session_id}/history", tags=["sessions"], dependencies=guard,
             operation_id="get_session_history", summary="分页读取原始会话正文")
    async def session_history(
        session_id: str, cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
        include_activity: bool = Query(default=False),
    ) -> dict[str, Any]:
        session = pick(session_id)
        current = session.in_flight if session.busy else None
        log = session.agent.session_log
        live_from_seq = current["live_from_seq"] if current else session.next_seq
        try:
            before = current["log_offset"] if current else log.path.stat().st_size if log else None
        except OSError:
            before = None
        timeline = await asyncio.to_thread(serve_history.read, log.path if log else None, before=before, include_activity=include_activity)
        paged = serve_history.page(timeline["messages"], cursor, limit)
        warning = " ".join(filter(None, (timeline["warning"], session.history_warning)))
        if log is not None and not log.complete:
            warning += " 会话日志写入不完整。"
        return {"session_id": session_id, "messages": paged.pop("items"), **paged,
                "warning": warning, "persisted": bool(cfg.persist and log and log.complete
                                                          and not session.history_warning and timeline["integrity"]),
                "live_from_seq": live_from_seq}

    serve_artifacts.register_artifact_routes(app, guard, pick)
    serve_contests.register_routes(app, guard, pick, earlier_submission, start_turn, cfg)
    serve_fleets.register_routes(app, guard, pick, cfg)
    serve_fleet_controls.register_routes(app, guard, pick)
    serve_weave_controls.register_routes(app, guard, pick)
    serve_fleets.register_command_routes(app, guard, pick, earlier_submission, start_turn)

    @app.get("/session/{session_id}/weaves/capabilities", dependencies=guard,
             operation_id="get_weave_capabilities", tags=["sessions"])
    async def weave_capabilities(session_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(serve_commands.capabilities, pick(session_id))

    @app.post("/session/{session_id}/weaves/preview", dependencies=guard,
              operation_id="preview_weave", tags=["sessions"])
    async def weave_preview(session_id: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            return (await asyncio.to_thread(serve_commands.prepare, pick(session_id), body)).preview
        except serve_commands.CommandError as exc:
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None

    @app.post("/session/{session_id}/weaves", dependencies=guard, status_code=202,
              operation_id="start_weave", tags=["sessions"])
    async def weave_start(session_id: str, body: dict[str, Any] = Body(...),
                          idempotency_key: str = Header(default="")) -> dict[str, Any]:
        session = pick(session_id)
        key = idempotency_key.strip()
        if not key or len(key) > 200:
            raise HTTPException(status_code=400, detail="七襄执行需要 1 至 200 字符的 Idempotency-Key")
        fingerprint = serve_commands.identity(body)
        if (earlier := earlier_submission(session, key, fingerprint)) is not None:
            run = next((run for run in session.runs if run["turn"] == earlier["turn"]), {})
            return {"session_id": session_id, "accepted": True, **session.status_dict(),
                    "duplicate": True, "turn": earlier["turn"], "run_id": run.get("id")}
        try:
            prepared = await asyncio.to_thread(serve_commands.prepare, session, body, require_preview=True)
        except serve_commands.CommandError as exc:
            if (earlier := earlier_submission(session, key, fingerprint)) is not None:
                run = next((run for run in session.runs if run["turn"] == earlier["turn"]), {})
                return {"session_id": session_id, "accepted": True, **session.status_dict(),
                        "duplicate": True, "turn": earlier["turn"], "run_id": run.get("id")}
            raise HTTPException(status_code=exc.status, detail=str(exc)) from None
        # Preflight performs IO off-loop. Recheck the receipt after the await so
        # simultaneous clicks cannot both reserve the same operation.
        if (earlier := earlier_submission(session, key, fingerprint)) is not None:
            run = next((run for run in session.runs if run["turn"] == earlier["turn"]), {})
            return {"session_id": session_id, "accepted": True, **session.status_dict(),
                    "duplicate": True, "turn": earlier["turn"], "run_id": run.get("id")}
        await start_turn(session, fingerprint, key=key, command=prepared)
        return {"session_id": session_id, "accepted": True, **session.status_dict(),
                "run_id": session.in_flight["id"]}

    @app.get("/session/{session_id}/weaves", tags=["sessions"], dependencies=guard,
             operation_id="get_session_weaves", summary="分页读取七襄协作状态与结果")
    async def session_weaves(
        session_id: str, cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=64),
    ) -> dict[str, Any]:
        session = pick(session_id)
        log = session.agent.session_log
        snapshot = session.weaves.snapshot()
        serve_weave_controls.enrich(session, snapshot)
        for batch in snapshot:
            for item in batch["items"]:
                item.pop("artifact_previews", None)
        paged = serve_history.page(snapshot, cursor, limit)
        return {"session_id": session_id, "batches": paged.pop("items"), **paged,
                "persisted": bool(cfg.persist and log and log.complete and session.weaves.integrity),
                "warning": session.weaves.warning, "truncated": session.weaves.truncated}

    @app.get("/session/{session_id}/runs", tags=["sessions"], dependencies=guard,
             operation_id="get_session_runs", summary="分页读取历轮结算与成果")
    async def session_runs(
        session_id: str, cursor: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        session = pick(session_id)
        log = session.agent.session_log
        snapshot = copy.deepcopy(session.runs)
        for run in snapshot:
            run.pop("artifact_previews", None)
        timeline = await asyncio.to_thread(serve_history.read, log.path if log else None)
        paged = serve_history.page(snapshot, cursor, limit)
        return {"session_id": session_id, "runs": paged.pop("items"), **paged,
                "persisted": bool(cfg.persist and log and log.complete and not session.history_warning
                                  and timeline["integrity"]),
                "warning": " ".join(filter(None, (timeline["warning"], session.history_warning)))}

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

    @app.websocket("/session/{session_id}/browser")
    async def session_browser(ws: WebSocket, session_id: str) -> None:
        """浏览器桥（docs/browser-bridge.md）。鉴权在第一帧 hello 里：浏览器的 WebSocket
        发不了自定义头，token 又不该进 URL / 日志。

        `ws: WebSocket` 与上面 `request: Request` 同一个坑：注解必须在模块全局解析得到，
        否则 FastAPI 把 ws 当查询参数，握手直接被关（close 1000，连 accept 都没有）。"""
        await ws.accept()
        try:
            hello = await asyncio.wait_for(ws.receive_json(), HELLO_TIMEOUT)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, RuntimeError):
            with contextlib.suppress(Exception):
                await ws.close(code=CLOSE_UNAUTHORIZED)
            return
        if (
            not isinstance(hello, dict)
            or hello.get("type") != "hello"
            or not token_matches(hello.get("token"), cfg.token)
        ):
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "message": "第一帧必须是 hello，且 token 要对"})
                await ws.close(code=CLOSE_UNAUTHORIZED)
            return
        session = sessions.get(session_id)
        if session is None:
            with contextlib.suppress(Exception):
                await ws.send_json({"type": "error", "message": f"未知 session_id {session_id!r}"})
                await ws.close(code=CLOSE_NOT_FOUND)
            return
        #  一个会话只接一条桥：用户重开侧栏时旧连接可能还没死，后来的顶掉先来的
        if session.bridge is not None:
            await session.bridge.close("replaced by a newer connection", code=CLOSE_REPLACED)
        bridge = BrowserBridge(
            session.id, session.agent.toolbox, asyncio.get_running_loop(), cfg.browser_timeout
        )
        session.bridge = bridge
        await bridge.attach(ws, hello)
        session.publish("browser.connected", client=bridge.client, tools=list(bridge.registered))
        try:
            while True:
                frame = await ws.receive_json()
                if isinstance(frame, dict):
                    bridge.handle_frame(frame)
        except WebSocketDisconnect:
            pass
        except Exception:  # noqa: BLE001 - 坏 JSON / 连接异常都按断线处理
            pass
        finally:
            if session.bridge is bridge:
                session.bridge = None
                bridge.detach("disconnected")
                session.publish("browser.disconnected")

    def pick_questions(session_id: str) -> question_types.QuestionManager:
        manager = pick(session_id).questions
        if manager is None:
            raise HTTPException(status_code=409, detail="Questions are not enabled for this session")
        return manager

    async def question_call(operation, *args):
        try:
            return await asyncio.get_running_loop().run_in_executor(
                app.state.question_pool, lambda: operation(*args))
        except question_types.QuestionNotFoundError:
            raise HTTPException(status_code=404, detail="Question not found in this session") from None
        except (question_types.QuestionConflictError, question_types.SessionClosedError):
            raise HTTPException(status_code=409, detail="Question conflicts with current session state") from None
        except question_types.ConfigurationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except question_types.SessionStorageError:
            raise HTTPException(status_code=503, detail="Question storage is unavailable; restart and reconcile") from None

    @app.get("/session/{session_id}/questions", tags=["questions"], dependencies=guard,
             operation_id="list_questions", summary="查询待答问题或重连时的全部状态")
    async def questions_list(session_id: str, include_terminal: bool = False):
        manager = pick_questions(session_id)
        # Capture the event cursor BEFORE the snapshot. Racing transitions may
        # appear twice; clients deduplicate by question version, never miss them.
        cursor = pick(session_id).next_seq
        items = await question_call(serve_questions.snapshots, manager, include_terminal)
        return {"session_id": session_id, "questions": items, "next_seq": cursor}

    @app.get("/session/{session_id}/questions/{question_id}", tags=["questions"], dependencies=guard,
             operation_id="get_question", summary="查询问题状态与提交回执")
    async def questions_get(session_id: str, question_id: str):
        return {"question": asdict(await question_call(pick_questions(session_id).get, question_id))}

    @app.post("/session/{session_id}/questions/{question_id}/answers", tags=["questions"], dependencies=guard,
              operation_id="answer_question", summary="幂等提交回答，不启动空闲模型",
              openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": {
                  "type": "object", "required": ["answers", "idempotency_key"], "additionalProperties": False,
                  "properties": {"idempotency_key": {"type": "string", "maxLength": 200}, "answers": {
                      "type": "array", "minItems": 1, "maxItems": 4, "items": {"type": "object",
                      "required": ["item_id"], "properties": {"item_id": {"type": "string"},
                      "selected": {"type": "array", "items": {"type": "string"}},
                      "custom": {"type": "string", "maxLength": 4000}, "skipped": {"type": "boolean"}}}}}}}}}})
    async def questions_answer(session_id: str, question_id: str, request: Request):
        manager = pick_questions(session_id)
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 131072:
                raise HTTPException(status_code=413, detail="Answer body exceeds 128 KiB")
            data.extend(chunk)
        try:
            body = json.loads(data.decode("utf-8", errors="replace"))
            answers, key = serve_questions.parse_answer(body)
        except (ValueError, question_types.ConfigurationError):
            raise HTTPException(status_code=400, detail="Invalid answer body") from None
        previous = await question_call(manager.get, question_id)
        if previous.idempotency_key == key and previous.answers == answers:
            return JSONResponse({"question": asdict(previous)}, status_code=200 if previous.state == "answered" else 202)
        serve_ownership.require_write(pick(session_id))
        receipt = await question_call(manager.answer, question_id, answers, key)
        return JSONResponse({"question": asdict(receipt)}, status_code=200 if receipt.state == "answered" else 202)

    @app.delete("/session/{session_id}/questions/{question_id}", tags=["questions"], dependencies=guard,
                operation_id="cancel_question", summary="取消未接纳的问题", status_code=204)
    async def questions_cancel(session_id: str, question_id: str):
        await question_call(pick_questions(session_id).cancel, question_id)
        return Response(status_code=204)

    @app.get(
        "/session/{session_id}/permissions",
        summary="挂起的审批",
        operation_id="list_permissions",
        tags=["approval"],
        dependencies=guard,
    )
    async def permissions_list(session_id: str) -> dict[str, Any]:
        session = pick(session_id)
        permissions = session.agent.permissions
        return {
            "pending": [item.to_dict() for item in session.snapshot_pending()],
            #  已放开哪些：会话授权可经 DELETE …/granted/{name} 收回；持久规则只读，
            #  改它请改规则文件（HTTP 面刻意不开写规则的口）
            "granted": permissions.session_grants(),
            "rules": [str(rule) for rule in permissions.rules],
        }

    @app.post(
        "/session/{session_id}/permissions",
        summary="回一个审批决定",
        operation_id="respond_permission",
        description=(
            "decision=allow 放行；allow_session 放行且本会话内同范围（挂起项的 session_scope）"
            "不再问——范围由服务端按这次调用推导，退出会话即失效；scope 可在挂起项"
            "session_scopes 列出的档位里选一个更粗的（如 browser:*），不带即最细一档；deny 拒绝（reason 会原文"
            "回灌给模型，等于改指令）；updated_args 非空时整体替换本次调用参数再执行"
            "（宿主'包一层再放行'的通道，不能与 allow_session 同用）。"
        ),
        tags=["approval"],
        dependencies=guard,
    )
    async def permissions_respond(
        session_id: str,
        request_id: str = Body(embed=True),
        decision: str = Body(embed=True, description="allow | allow_session | deny"),
        reason: str = Body(default="", embed=True),
        updated_args: dict[str, Any] | None = Body(default=None, embed=True),
        scope: str | None = Body(
            default=None, embed=True, description="allow_session 选哪一档，须是挂起项 session_scopes 之一"
        ),
    ) -> dict[str, Any]:
        session = pick(session_id)
        request = session.take_pending(request_id)
        if request is None:
            raise HTTPException(status_code=404, detail=f"没有挂起的审批 {request_id!r}（可能已超时）")
        if request.done.is_set():
            raise HTTPException(status_code=409, detail="这条审批已经处理，请刷新状态")
        if request.identity and (decision not in ("allow", "deny") or updated_args is not None or scope is not None):
            raise HTTPException(status_code=400, detail="成员审批仅支持单次 allow 或 deny，不能改写参数或授予会话权限")
        if decision not in ("allow", "allow_session", "deny"):
            raise HTTPException(status_code=400, detail="decision 只能是 allow、allow_session 或 deny")
        if scope is not None and decision != "allow_session":
            raise HTTPException(status_code=400, detail="scope 只配合 allow_session 用")
        if decision != "deny":
            serve_ownership.require_write(session)
        if decision == "allow_session":
            if updated_args is not None:
                raise HTTPException(
                    status_code=400,
                    detail="allow_session 不能与 updated_args 同用：会话授权按原调用推导范围，"
                    "改写后的调用请用 allow",
                )
            #  范围只能由服务端从挂起的这次调用推导（与 TUI / ACP 确认框同一判据）：
            #  持 token 的客户端放得开的，只有模型真实尝试过、且推得出范围的那一类调用，
            #  这是它与"建个 allow_all 的 agent"那种绕过的本质区别
            permissions = session.agent.permissions
            if request.session_scope is None:
                raise HTTPException(
                    status_code=400, detail="这次调用推不出会话授权范围，只能 allow 或 deny"
                )
            #  档位也只认服务端随挂起项下发的那几个：不带 = 最细一档
            granted = permissions.grant_session_scope(
                request.name, request.args, request.session_scope if scope is None else scope
            )
            if granted is None:
                offered = "、".join(item["scope"] for item in request.session_scopes)
                raise HTTPException(
                    status_code=400, detail=f"scope {scope!r} 不在这次可选的档位里（{offered}）"
                )
            request.granted = granted
        request.decision = decision
        request.verdict = (
            Deny(reason) if decision == "deny" else Allow(note=reason, updated_args=updated_args)
        )
        request.done.set()
        result: dict[str, Any] = {"request_id": request_id, "decision": decision}
        if request.granted:
            result["granted"] = request.granted
        return result

    @app.delete(
        "/session/{session_id}/permissions/granted/{name:path}",
        summary="收回一条会话授权",
        operation_id="revoke_session_grant",
        description=(
            "name 是 GET /permissions 返回的 granted.tools 里的工具名或 granted.commands 里的"
            "命令键。只收会话授权；持久规则不经 HTTP 改。"
        ),
        tags=["approval"],
        dependencies=guard,
        status_code=204,
    )
    async def permissions_revoke(session_id: str, name: str):
        session = pick(session_id)
        if not session.agent.permissions.revoke_session(name):
            raise HTTPException(status_code=404, detail=f"没有这条会话授权 {name!r}")
        return Response(status_code=204)

    @app.post(
        "/session/{session_id}/budget",
        summary="改/撤本会话的预算",
        operation_id="set_budget",
        description=(
            "budget=null 撤掉预算；否则整体替换为 {tokens, usd}。到线后的会话（detail=budget_reached）"
            "只有经这里调高或撤掉才能再跑——这是硬闸，不是提醒。"
        ),
        tags=["run"],
        dependencies=guard,
    )
    async def session_budget(
        session_id: str,
        budget: dict[str, Any] | None = Body(default=None, embed=True),
    ) -> dict[str, Any]:
        session = pick(session_id)
        serve_ownership.require_write(session)
        try:
            session.budget = Budget.parse(budget)
        except StateError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        session.budget_reason = session.check_budget()
        if not session.budget_reason and session.detail == "budget_reached":
            session.detail = "finished"
        session.publish("budget.changed", budget=session.budget.to_dict() if session.budget else None)
        manifests.save(session.manifest())
        return session.status_dict()

    @app.post(
        "/session/{session_id}/abort",
        summary="打断当前这一轮",
        tags=["run"],
        dependencies=guard,
        operation_id="abort",
    )
    async def session_abort(
        session_id: str,
        x_xiaoyu_instance: str = Header(default=""),
        turn: int | None = Body(
            default=None, embed=True,
            description="要打断的是第几轮（/status 的 in_flight）。给了就只打断那一轮：它已经结束、"
            "在跑的是别的轮时什么都不做",
        ),
    ) -> dict[str, Any]:
        session = pick(session_id)
        running = (session.in_flight or {}).get("turn") if session.busy else None
        if ((turn is not None and turn != running)
                or (x_xiaoyu_instance and x_xiaoyu_instance != instance_id)):
            #  迟到的 abort：它想停的那一轮已经结束，这时候打断只会误伤后面那一轮
            return {
                "session_id": session_id, "aborted": False, "running_turn": running,
                "detail": f"第 {turn} 轮不在跑" + (f"（在跑的是第 {running} 轮）" if running else ""),
            }
        session.interrupt_owned()
        if session.questions is not None:
            session.questions._wake()
        #  打断时挂着审批的话，工作线程还堵在 Event 上——先把它放掉，
        #  否则 interrupt 要等到 approval_timeout 才生效
        for request in session.snapshot_pending():
            if request.identity:
                continue
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
        turn: int | None = Body(default=None, embed=True),
        x_xiaoyu_instance: str = Header(default=""),
    ) -> dict[str, Any]:
        session = pick(session_id)
        serve_ownership.require_write(session)
        #  Agent._turn() 开头就 drain_steers()，空闲时入队的插话下一轮开头即被
        #  丢弃。回 200 会让编排器以为这句话进了上下文——响亮地 409，别装成功
        if not session.busy:
            raise HTTPException(
                status_code=409, detail="会话空闲，没有正在进行的一轮可插话（改用 /prompt）"
            )
        if (session.in_flight or {}).get("command", {}).get("kind") in {"qixiang", "douqiao", "chenshu"}:
            raise HTTPException(status_code=409, detail="结构化协作执行中不接收父会话插话；请停止后另开任务或按支持的条目续跑")
        if ((turn is not None and turn != (session.in_flight or {}).get("turn"))
                or (x_xiaoyu_instance and x_xiaoyu_instance != instance_id)):
            raise HTTPException(status_code=409, detail="服务或轮次已变化，插话未提交")
        session.async_agent.steer(text)
        return {"session_id": session_id, "steered": True}

    def rename_session(session_id: str, title: str) -> dict[str, Any]:
        session = pick(session_id)
        if session.title == title:
            return {"session_id": session_id, "title": title}
        serve_ownership.require_write(session)
        if session.busy:
            raise HTTPException(409, "请在当前轮完成后重命名会话")
        try:
            manifests.save({**session.manifest(), "title": title})
        except OSError:
            raise HTTPException(503, "标题无法可靠保存，原标题已保留") from None
        session.title = title
        return {"session_id": session_id, "title": title}

    app.state.rename_session = rename_session

    def select_session_model(session_id: str, model: str, previous: str) -> dict[str, Any]:
        session = pick(session_id)
        serve_ownership.require_write(session)
        if session.agent_ref:
            raise HTTPException(409, "此会话使用固定助手配置，请新建普通会话后选择模型")
        current = getattr(session, "next_model", "") or session.agent.config.model
        if current != previous and current != model:
            raise HTTPException(409, "另一窗口已更改模型，请刷新后重新选择")
        try:
            session.agent.registry.resolve(model)
        except Exception:
            raise HTTPException(422, "所选模型没有可用的已配置路由") from None
        next_model = model if model != session.agent.config.model else ""
        try:
            manifests.save({**session.manifest(), "next_model": next_model})
        except OSError:
            raise HTTPException(503, "模型选择未能保存，原选择已保留") from None
        session.next_model = next_model
        return {"session_id": session_id, "model": session.agent.config.model, "next_model": next_model}

    app.state.select_session_model = select_session_model


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
    tls: dict[str, Any] = {}
    if cfg.tls_cert is not None and cfg.tls_key is not None:
        tls = {"ssl_certfile": str(cfg.tls_cert), "ssl_keyfile": str(cfg.tls_key)}
    uvicorn.run(app, host=cfg.host, port=cfg.port, log_level="warning", **tls)
    return 0


def print_openapi(cfg: ServeConfig, public_url: str = "") -> int:
    """把 OpenAPI schema 打到 stdout —— Dify 自定义工具直接贴这份。

    必须带 `servers`：Dify / n8n 的 OpenAPI 导入器要从这里取 base URL，缺了
    它们只能拿到相对路径，导进去也发不出请求。FastAPI 默认不生成这一段
    （它假设调用方就在同一个 origin 下），所以这里显式补。

    `public_url`（参数优先，其次 `cfg.public_url`）缺省按监听地址推。**编排器多半
    跑在容器里**，那时 127.0.0.1 是容器自己而不是本机——所以推出来是回环地址时
    额外打一行提示，别让人照抄一个在 Dify 里必然连不上的地址。
    """
    app = create_app(cfg)
    schema = app.openapi()
    explicit = public_url or cfg.public_url
    url = explicit or f"http://{cfg.host}:{cfg.port}"
    schema["servers"] = [{"url": url, "description": "xiaoyu serve"}]
    if not explicit and cfg.host in ("127.0.0.1", "::1", "localhost", "0.0.0.0"):
        print(
            f"提示：schema 里的 servers 填的是 {url}。若 Dify / n8n 跑在容器里，"
            "这个地址指向的是容器自己——请用 --public-url 指定它们真正能访问到的地址"
            "（Docker Desktop 上通常是 http://host.docker.internal:%d）。" % cfg.port,
            file=sys.stderr,
        )
    json.dump(schema, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    return 0
