"""serve 的 MCP server 面：把小羽整个 agent 当一个工具，挂在 `/mcp` 上。

这是 serve（HTTP API）之上的第二张协议脸，不是第四条协议面：REST 与 MCP
共用同一个会话注册表、同一套状态机与审批回路，MCP 只是换一种消费方能直接
吃的话法。目标消费方是 **agent 编排框架**（LangChain / LangGraph 经官方
langchain-mcp-adapters、其它 MCP client）——它们没有消费自定义
REST API 的习惯，有的是消费 MCP 的习惯。

工具面照"agent 级 serve"的成熟形态（跑任务 + 按会话续聊 + 收尾）做小而全：

    xiaoyu(prompt, workspace?, model?, mode?)   新会话跑一轮 → 结果 + session_id
    xiaoyu_reply(session_id, prompt)            带着 session_id 续聊
    xiaoyu_close(session_id)                    释放会话（MCP-only 消费方的唯一释放口）

三条刻意的设计决定（这层的骨架，改之前先想清楚）：

1. **无状态 server：不发 Mcp-Session-Id，会话延续走工具参数里的 session_id。**
   langchain-mcp-adapters 默认每次工具调用新建一条 ClientSession，MCP 传输层
   会话在主流消费方那里根本活不过一次调用。把对话身份放进工具参数，
   无状态客户端就是一等公民；顺带砍掉整类
   "传输层会话过期/清理"的状态 bug。`initialize` 幂等，随便发几次。

2. **审批不走 elicitation，走既有 REST 回路。** elicitation 是协议正解，但
   主流客户端（含 adapters）不支持，做了没人能用。ask 档下工具调用挂起时，
   MCP 侧的 tools/call 停在 waiting_for_approval，由人或编排器从
   REST `POST /session/{id}/permissions` 放行——两张脸共享同一 `_Session`，
   这条跨面回路是免费的。无人值守场景在**启动期**选 `--approval allow_all`。
   刻意不把 approval 做成工具参数：MCP 工具的参数是模型填的，让模型能给
   自己选免审批档，等于把闸门的钥匙挂在闸门上。

3. **tools/call 一律以 SSE 应答，长轮次靠 notifications/progress 续命。**
   coding agent 一轮动辄几分钟，MCP 客户端普遍有请求超时，多数以
   progress 通知为重置依据（spec 授权的姿势）。请求带 `_meta.progressToken`
   才发 progress（spec 要求）；不带也走 SSE，靠心跳注释防中间层掐连接。
   initialize / tools/list / ping 这类瞬时方法直接回 JSON。

支持的方法就这五个：initialize、notifications/*（按 spec 回 202 不应答）、
ping、tools/list、tools/call。sampling / roots / resources / elicitation
都不做——与 mcp.py（client 侧）"不接反向使唤通道"同一纪律。JSON-RPC batch
不收（2025-06-18 spec 已删除 batch）。

鉴权与 REST 同一道门：`Authorization: Bearer <token>`（或 X-Xiaoyu-Token），
绑非回环地址必须带 token 的启动闸在 serve.py，这里不重复。`/mcp` 不进
OpenAPI schema——那份 schema 是给 Dify/n8n 的 REST 工具清单，混进一个
JSON-RPC 端点只会把导入器搞糊涂。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable

#  与 serve.py 同一个坑：本文件有 `from __future__ import annotations`，路由
#  函数的 `request: Request` 注解是字符串，FastAPI 拿函数 __globals__ 解析——
#  mount_mcp 里的局部 import 在那儿看不见，Request 会被当成查询参数（422）。
try:
    from starlette.requests import Request
except ImportError:  # pragma: no cover - 没装 [serve] 时 create_app 根本走不到这里
    Request = Any  # type: ignore[assignment,misc]

from .serve import HEARTBEAT, POLL_SLICE, ServeConfig, _Session

#  新在前：客户端要的版本我们有就照给，没有就报最新的（spec 的协商规则，
#  客户端拿到不认识的版本自己决定去留）
SUPPORTED_PROTOCOLS = ("2025-06-18", "2025-03-26")

#  tools/call 的进度事件里，这些 kind 太碎（一个 token 一条），不值得
#  逐条打进 notifications/progress——进度的意义是"还活着、在哪一步"，
#  不是把事件流整个复述一遍（要全量事件去拉 REST /events）。
NOISY_KINDS = frozenset({"text.delta"})


class _RpcError(Exception):
    """JSON-RPC 协议级错误（区别于工具执行错误——那走 result.isError）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


#  工具执行结果的结构化形态（三个工具共用一份词汇；xiaoyu_close 用子集）。
#  declared 在 outputSchema 里就必须每次都给全 required——消费方是按 schema
#  写解析的，字段时有时无等于没有 schema。
_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string", "description": "会话 ID，xiaoyu_reply / xiaoyu_close 用它"},
        "text": {"type": "string", "description": "本轮最后一条 assistant 正文"},
        "status": {"type": "string", "description": "idle / error"},
        "detail": {"type": "string", "description": "finished / interrupted / failed"},
        "interrupted": {"type": "boolean"},
        "turns": {"type": "integer", "description": "会话累计轮数"},
        "error": {"type": "string", "description": "失败时的错误原文，正常为空串"},
    },
    "required": ["session_id", "text", "status", "detail", "error"],
}

_CLOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "session_id": {"type": "string"},
        "closed": {"type": "boolean"},
    },
    "required": ["session_id", "closed"],
}


def _tool_defs() -> list[dict[str, Any]]:
    """tools/list 的负载。描述写给**做工具选择的模型**看：说清分工与衔接。"""
    return [
        {
            "name": "xiaoyu",
            "title": "小羽：新会话跑一个任务",
            "description": (
                "让小羽（coding agent）在新会话里执行一个任务（改代码/跑命令/查仓库），"
                "跑完返回结果与 session_id。同一件事要续聊用 xiaoyu_reply 带上 session_id，"
                "不要重开会话；收尾后用 xiaoyu_close 释放。长任务会持续用 notifications/progress "
                "上报进度（请求带 _meta.progressToken 才有）。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "给小羽的指令"},
                    "workspace": {
                        "type": "string",
                        "description": "工作区路径，必须在服务端 root 之内（相对路径按 root 解析）；缺省用 root",
                    },
                    "model": {"type": "string", "description": "模型名，缺省跟随服务端配置"},
                    "mode": {"type": "string", "description": "default / auto / plan，缺省跟随服务端配置"},
                },
                "required": ["prompt"],
            },
            "outputSchema": _RESULT_SCHEMA,
        },
        {
            "name": "xiaoyu_reply",
            "title": "小羽：在已有会话里续聊",
            "description": (
                "向 xiaoyu 建出的会话追加一轮指令（上下文完整保留）。"
                "session_id 来自 xiaoyu / xiaoyu_reply 的返回值。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "xiaoyu 返回的会话 ID"},
                    "prompt": {"type": "string", "description": "给小羽的指令"},
                },
                "required": ["session_id", "prompt"],
            },
            "outputSchema": _RESULT_SCHEMA,
        },
        {
            "name": "xiaoyu_close",
            "title": "小羽：关闭会话",
            "description": (
                "释放一个会话（消息历史与事件缓冲随之回收）。会话不会自动过期，"
                "任务收尾后调这个，长跑的编排流程才不会攒出一屋子僵尸会话。"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                },
                "required": ["session_id"],
            },
            "outputSchema": _CLOSE_SCHEMA,
        },
    ]


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _tool_error(message: str) -> dict[str, Any]:
    """工具**执行**失败的 result（isError 走 result 而不是协议错误——spec 的
    分界：协议错误是"这个调用发得不对"，执行错误是"调用没问题、活儿没干成"，
    后者要让模型看得见原文才能自我修正）。

    带 outputSchema 的工具连错误 result 也必须带合 schema 的 structuredContent
    ——消费方按 schema 解析，isError 分支缺字段照样炸它的解析器。
    """
    return {
        "content": [{"type": "text", "text": message}],
        "structuredContent": {
            "session_id": "",
            "text": "",
            "status": "error",
            "detail": "failed",
            "error": message,
        },
        "isError": True,
    }


def _turn_result(session: _Session, result: dict[str, Any]) -> dict[str, Any]:
    """一轮跑完 → tools/call 的 result。文本给模型看，结构化给代码用。"""
    failed = session.status == "error"
    text = str(result.get("text") or "")
    structured = {
        "session_id": session.id,
        "text": text,
        "status": session.status,
        "detail": session.detail,
        "interrupted": bool(result.get("interrupted")),
        "turns": session.turns,
        "error": session.error,
    }
    return {
        "content": [{"type": "text", "text": text if not failed else session.error}],
        "structuredContent": structured,
        "isError": failed,
    }


def _progress_message(event: dict[str, Any]) -> str:
    """事件 → progress 通知的一行人话。全量事件在 REST /events，这里只报站。"""
    kind = str(event.get("kind", ""))
    name = event.get("name") or event.get("tool") or ""
    return f"{kind} {name}".strip()


def mount_mcp(
    app: Any,
    cfg: ServeConfig,
    *,
    sessions: dict[str, _Session],
    make_session: Callable[..., _Session],
    start_turn: Callable[..., Any],
    close_session: Callable[[_Session], None],
    guard: list[Any],
) -> None:
    """把 `/mcp` 挂到 serve 的 FastAPI app 上。

    依赖以钩子传入而不是 import serve 的闭包内部——REST 与 MCP 共享的就是
    这几样（会话注册表、装配、开轮、关闭、鉴权），清单本身就是两张脸的
    契约面，宽于此的耦合都不该有。
    """
    from fastapi import Response
    from fastapi.responses import JSONResponse, StreamingResponse

    server_version = getattr(app, "version", "")

    # ---------- 瞬时方法（纯函数：进请求出响应，不碰会话） ----------

    def dispatch_plain(method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = str(params.get("protocolVersion", ""))
            version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "xiaoyu", "version": server_version},
                "instructions": (
                    "xiaoyu 新会话跑任务 → 返回里拿 session_id → xiaoyu_reply 续聊 → "
                    "xiaoyu_close 释放。服务端 ask 档下需要人工放行的工具调用会让调用"
                    "挂起，由 REST 侧 POST /session/{id}/permissions 放行或拒绝。"
                ),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": _tool_defs()}
        raise _RpcError(-32601, f"不支持的方法 {method!r}（这台 server 只有 tools 一种能力）")

    # ---------- tools/call（长活：SSE 应答 + 进度） ----------

    def resolve_call(name: str, args: dict[str, Any]) -> tuple[_Session, str] | dict[str, Any]:
        """工具名+参数 → (会话, prompt)，或一个现成的错误 result。

        参数形状不对是协议错误（-32602，模型/客户端发错了调用）；会话不存在、
        workspace 越界这类是执行错误（isError result，让模型看到原文自己纠偏）。
        """
        if name == "xiaoyu":
            prompt = args.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise _RpcError(-32602, "xiaoyu 需要非空的 prompt")
            try:
                session = make_session(
                    workspace=str(args.get("workspace") or ""),
                    model=str(args.get("model") or ""),
                    mode=str(args.get("mode") or ""),
                )
            except Exception as exc:  # noqa: BLE001 - HTTPException(400/503) 与装配错误统一降成工具错误
                return _tool_error(f"建会话失败：{getattr(exc, 'detail', None) or exc}")
            return session, prompt
        if name == "xiaoyu_reply":
            session_id = args.get("session_id")
            prompt = args.get("prompt")
            if not isinstance(session_id, str) or not isinstance(prompt, str) or not prompt.strip():
                raise _RpcError(-32602, "xiaoyu_reply 需要 session_id 与非空的 prompt")
            session = sessions.get(session_id)
            if session is None:
                return _tool_error(f"未知 session_id {session_id!r}（可能已被 xiaoyu_close 释放）")
            return session, prompt
        raise _RpcError(-32602, f"未知工具 {name!r}（可用：xiaoyu / xiaoyu_reply / xiaoyu_close）")

    async def run_tool_call(message: dict[str, Any]) -> AsyncIterator[str]:
        """跑一轮并以 SSE 逐帧吐 progress，最后一帧是 JSON-RPC 响应。

        响应头已经 200 出去了，这里面**任何**失败都只能落成帧（错误响应帧或
        isError result），绝不能抛——抛出去客户端看到的是流断掉，比任何错误
        信息都难排查。
        """
        request_id = message.get("id")
        params = message.get("params") or {}
        name = str(params.get("name", ""))
        args = params.get("arguments") or {}
        token = (params.get("_meta") or {}).get("progressToken")

        def frame(payload: dict[str, Any]) -> str:
            return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

        try:
            #  close 是瞬时动作，不值得进"开轮 + 追事件"的机器
            if name == "xiaoyu_close":
                session_id = str(args.get("session_id", ""))
                session = sessions.get(session_id)
                if session is None:
                    yield frame(_rpc_result(request_id, _tool_error(f"未知 session_id {session_id!r}")))
                    return
                close_session(session)
                yield frame(
                    _rpc_result(
                        request_id,
                        {
                            "content": [{"type": "text", "text": f"会话 {session_id} 已关闭"}],
                            "structuredContent": {"session_id": session_id, "closed": True},
                            "isError": False,
                        },
                    )
                )
                return

            resolved = resolve_call(name, args)
            if isinstance(resolved, dict):
                yield frame(_rpc_result(request_id, resolved))
                return
            session, prompt = resolved

            #  游标必须先于 start_turn 取：run.started 在 start_turn 里就发出了
            cursor = session.next_seq
            try:
                task = await start_turn(session, prompt)
            except Exception as exc:  # noqa: BLE001 - busy 的 409 等，对 MCP 消费方是执行错误
                yield frame(
                    _rpc_result(
                        request_id, _tool_error(str(getattr(exc, "detail", None) or exc))
                    )
                )
                return

            progress_seq = 0
            idle = 0.0
            while not task.done():
                await session.wait_new(cursor, POLL_SLICE)
                fresh = session.since(cursor, 500)
                if fresh:
                    idle = 0.0
                    cursor = fresh[-1]["seq"] + 1
                    if token is not None:
                        for event in fresh:
                            if event.get("kind") in NOISY_KINDS:
                                continue
                            progress_seq = event["seq"]
                            yield frame(
                                {
                                    "jsonrpc": "2.0",
                                    "method": "notifications/progress",
                                    "params": {
                                        "progressToken": token,
                                        #  单调递增用事件 seq：轮与轮之间也不回退
                                        "progress": progress_seq,
                                        "message": _progress_message(event),
                                    },
                                }
                            )
                else:
                    idle += POLL_SLICE
                    if idle >= HEARTBEAT:
                        idle = 0.0
                        #  与 SSE 端点同一道理：反代/网关会掐长时间无字节的连接
                        yield ": keep-alive\n\n"

            result = task.result()
            yield frame(_rpc_result(request_id, _turn_result(session, result)))
        except _RpcError as exc:
            yield frame(_rpc_error(request_id, exc.code, exc.message))
        except Exception as exc:  # noqa: BLE001 - 见 docstring：流开了就只能落帧
            yield frame(_rpc_error(request_id, -32603, f"{type(exc).__name__}: {exc}"))

    # ---------- 传输 ----------

    @app.post("/mcp", include_in_schema=False, dependencies=guard)
    async def mcp_endpoint(request: Request):
        try:
            message = json.loads(await request.body())
        except (ValueError, UnicodeDecodeError):
            return JSONResponse(_rpc_error(None, -32700, "不是合法的 JSON"), status_code=400)
        if isinstance(message, list):
            #  2025-06-18 spec 删除了 batch；旧客户端发来也响亮拒绝，
            #  静默处理第一条会让它以为剩下的也发出去了
            return JSONResponse(
                _rpc_error(None, -32600, "不支持 JSON-RPC batch（MCP 2025-06-18 起已删除）"),
                status_code=400,
            )
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return JSONResponse(_rpc_error(None, -32600, "不是 JSON-RPC 2.0 消息"), status_code=400)

        if "id" not in message:
            #  通知（notifications/initialized 等）：spec 要求 202 无正文
            return Response(status_code=202)

        method = str(message.get("method", ""))
        if method == "tools/call":
            return StreamingResponse(run_tool_call(message), media_type="text/event-stream")
        try:
            result = dispatch_plain(method, message.get("params") or {})
        except _RpcError as exc:
            return JSONResponse(_rpc_error(message.get("id"), exc.code, exc.message))
        return JSONResponse(_rpc_result(message.get("id"), result))

    @app.get("/mcp", include_in_schema=False)
    @app.delete("/mcp", include_in_schema=False)
    async def mcp_not_stream() -> Response:
        #  无状态 server：不开 GET 的常驻推送通道，也没有可 DELETE 的传输层
        #  会话。spec 明说这种形态就回 405。
        return Response(status_code=405, headers={"Allow": "POST"})
