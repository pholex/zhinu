"""wire 模式：单进程 stdio JSON-RPC 2.0（按小羽同步架构收敛）。

`xiaoyu --wire` 后本进程即 headless agent server：外部 UI（编辑器插件、Web
壳、测试驱动器）经 stdin/stdout 一行一条 JSON 驱动它。这是「headless
前端」的协议层——**单进程、无常驻 server、无 HTTP**：当年"不做
client/server 拆分"的结论只针对进程拆分，stdio 子进程形态没有那些税。

协议（version 1.0）：

client → server（JSON-RPC request，带 id）：
    initialize   {}            → {protocol_version, server:{name,version}, model,
                                 workspace, messages}
                                 messages = 已在场的历史条数（`--session-id`
                                 续上老会话时非 0，新会话为 0）
    prompt       {text}        → 整轮结束后回 {status, result, usage}
                                 status ∈ finished | interrupted | error
                                 一次只跑一轮，忙时回错误码 -32000
    steer        {text}        → {}   运行中插话（Agent.steer 语义）
    cancel       {}            → {}   打断当前轮（Agent.interrupt 语义），
                                 并把所有挂起的审批按拒绝解决

server → client：
    事件（notification，无 id）：
        {"jsonrpc":"2.0","method":"event","params":{"kind":"text.delta", ...}}
        params 就是 events.py 的 to_dict()——与 --output-format stream-json
        同一套词汇，消费方不认识的 kind 跳过即可。
    审批（request，带 id，等响应；「Request 自带等待」的形态）：
        {"jsonrpc":"2.0","id":"srv-1","method":"request",
         "params":{"type":"approval","payload":{"name":"bash","args":{...}}}}
        client 回 {"id":"srv-1","result":{"verdict":"allow"|"deny",
                   "note":…, "reason":…, "updated_args":{…}}}
        语义与嵌入面 Allow/Deny 完全对齐（updated_args=批准并改写参数）；
        回包缺失/畸形一律 fail closed 按拒绝处理。

线程模型（全同步，无 asyncio）：主线程读 stdin 分发；prompt 在工作线程跑
agent.send()；审批在工作线程阻塞在 threading.Event 上，主线程收到响应后
resolve——工具侧等待与传输层解耦。stdout 写入全部过同一把锁，事件与响应
不会互相打断。

EOF / 连接关闭：挂起审批全部按拒绝解决、打断当前轮、等工作线程收尾——
关停路径同一条"安全默认值"纪律。
"""

from __future__ import annotations

import json
import sys
import threading
from typing import Any, TextIO

from . import __version__
from .agent import Agent, Allow, Deny, Interrupted
from .events import UIEvent

PROTOCOL_VERSION = "1.0"

#  JSON-RPC 标准错误码 + 自定义段
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INVALID_STATE = -32000


def verdict_from(payload: Any) -> Allow | Deny:
    """client 的审批回包 → Allow/Deny。任何看不懂的形态都 fail closed。"""
    if not isinstance(payload, dict):
        return Deny("wire client 返回了无法解析的审批结果")
    verdict = payload.get("verdict")
    if verdict == "allow":
        updated = payload.get("updated_args")
        if updated is not None and not isinstance(updated, dict):
            return Deny("wire client 的 updated_args 不是对象")
        return Allow(note=str(payload.get("note", "") or ""), updated_args=updated)
    if verdict == "deny":
        return Deny(str(payload.get("reason", "") or ""))
    return Deny("wire client 返回了未知的 verdict（只认 allow/deny）")


class WireSink:
    """事件 → notification。agent 的所有输出都从这里上线，stdout 不再有别的声音。"""

    def __init__(self, server: "WireServer") -> None:
        self._server = server

    def emit(self, event: UIEvent) -> None:
        self._server._notify("event", event.to_dict())


class WireServer:
    """stdio JSON-RPC server。构造顺序与 Tui 同款：先建 server（Agent 需要
    approver 与 sink），agent 引用用 attach() 回填。"""

    def __init__(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        self._stdin = stdin or sys.stdin
        self._stdout = stdout or sys.stdout
        self._write_lock = threading.Lock()
        self.sink = WireSink(self)
        self.agent: Agent | None = None
        #  当前轮的工作线程（同一时刻至多一个）
        self._turn: threading.Thread | None = None
        #  挂起的 server→client 审批：id → (Event, 结果槽)
        self._pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._request_seq = 0
        self._closed = False

    def attach(self, agent: Agent) -> None:
        self.agent = agent

    # ---------- 出站 ----------

    def _send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            self._stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            self._stdout.flush()

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _respond(self, req_id: Any, result: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _fail(self, req_id: Any, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})

    # ---------- 主循环 ----------

    def serve(self) -> int:
        assert self.agent is not None, "serve() 之前必须 attach(agent)"
        try:
            for raw in self._stdin:
                line = raw.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._fail(None, PARSE_ERROR, f"JSON 解析失败：{exc}")
                    continue
                if not isinstance(message, dict):
                    self._fail(None, INVALID_REQUEST, "必须是 JSON 对象")
                    continue
                if "method" in message:
                    self._handle_request(message)
                elif "id" in message:
                    self._resolve_pending(message)
                else:
                    self._fail(None, INVALID_REQUEST, "既不是请求也不是响应")
        except KeyboardInterrupt:
            pass
        finally:
            self._shutdown()
        return 0

    # ---------- client → server ----------

    def _handle_request(self, message: dict[str, Any]) -> None:
        req_id = message.get("id")
        method = message.get("method")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            self._fail(req_id, INVALID_PARAMS, "params 必须是对象")
            return

        if method == "initialize":
            assert self.agent is not None
            self._respond(
                req_id,
                {
                    "protocol_version": PROTOCOL_VERSION,
                    "server": {"name": "xiaoyu", "version": __version__},
                    "model": self.agent.config.model,
                    "workspace": str(self.agent.config.workspace),
                    #  已在场的历史条数：`--session-id` 续上老会话时非 0，
                    #  外部 UI 据此知道自己接的不是一张白纸（新增字段，老客户端忽略即可）。
                    #  减 1 是 messages[0] 的 system prompt——它任何会话都有，不算历史
                    "messages": len(self.agent.messages) - 1,
                },
            )
        elif method == "prompt":
            self._handle_prompt(req_id, params)
        elif method == "steer":
            text = str(params.get("text", "") or "")
            if not text.strip():
                self._fail(req_id, INVALID_PARAMS, "steer 需要非空 text")
                return
            assert self.agent is not None
            self.agent.steer(text)
            self._respond(req_id, {})
        elif method == "cancel":
            self._cancel_turn()
            self._respond(req_id, {})
        else:
            self._fail(req_id, METHOD_NOT_FOUND, f"未知方法 {method!r}")

    def _handle_prompt(self, req_id: Any, params: dict[str, Any]) -> None:
        text = str(params.get("text", "") or "")
        if not text.strip():
            self._fail(req_id, INVALID_PARAMS, "prompt 需要非空 text")
            return
        if self._turn is not None and self._turn.is_alive():
            self._fail(req_id, INVALID_STATE, "已有一轮在进行中（可 steer 插话或 cancel 打断）")
            return
        self._turn = threading.Thread(
            target=self._run_turn, args=(req_id, text), daemon=True, name="wire-turn"
        )
        self._turn.start()

    def _run_turn(self, req_id: Any, text: str) -> None:
        assert self.agent is not None
        agent = self.agent
        status, error = "finished", ""
        try:
            agent.send(text)
        except (Interrupted, KeyboardInterrupt):
            agent.close_open_tool_calls("本轮被 wire client 取消。")
            status = "interrupted"
        except Exception as exc:  # noqa: BLE001 - client 要结构化错误，不是 traceback
            status, error = "error", f"{type(exc).__name__}: {exc}"
            if agent.session_log:
                agent.session_log.event("error", error=error)
        result: dict[str, Any] = {
            "status": status,
            "result": agent.last_assistant_text(),
            "usage": agent.usage.to_dict(),
        }
        if error:
            result["error"] = error
        self._respond(req_id, result)

    def _cancel_turn(self) -> None:
        """打断当前轮：挂起审批按拒绝解决（工作线程正阻塞在上面），
        再置打断标志（下一个流式 chunk 边界收尾）。"""
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for event, slot in pending:
            slot["result"] = {"verdict": "deny", "reason": "用户取消了本轮"}
            event.set()
        if self.agent is not None:
            self.agent.interrupt()

    # ---------- server → client（审批） ----------

    def approve(self, name: str, args: dict[str, Any]) -> Allow | Deny:
        """Agent 的 approver（在工作线程被调用）：发 request，阻塞等 client 响应。

        连接已关闭时直接拒绝——fail closed 与嵌入面 AsyncApprover 同一条纪律。
        """
        if self._closed:
            return Deny("wire 连接已关闭，无人审批")
        self._request_seq += 1
        req_id = f"srv-{self._request_seq}"
        event = threading.Event()
        slot: dict[str, Any] = {}
        with self._pending_lock:
            self._pending[req_id] = (event, slot)
        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "request",
                "params": {"type": "approval", "payload": {"name": name, "args": args}},
            }
        )
        #  轮询式等待：既等响应，也随时发现连接关闭（shutdown 会 set 全部事件，
        #  这层判断只是双保险）
        while not event.wait(0.2):
            if self._closed:
                return Deny("wire 连接已关闭，无人审批")
        return verdict_from(slot.get("result"))

    def _resolve_pending(self, message: dict[str, Any]) -> None:
        req_id = message.get("id")
        with self._pending_lock:
            entry = self._pending.pop(str(req_id), None)
        if entry is None:
            #  迟到/重复的响应：静默丢弃（cancel 或 shutdown 已经替它做了决定）
            return
        event, slot = entry
        if "result" in message:
            slot["result"] = message["result"]
        #  error 响应或缺 result：slot 留空，verdict_from(None) fail closed
        event.set()

    # ---------- 收尾 ----------

    def _shutdown(self) -> None:
        self._closed = True
        self._cancel_turn()
        if self._turn is not None:
            self._turn.join(timeout=10.0)
