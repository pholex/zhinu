"""浏览器桥：`xiaoyu serve` 的 `/session/{id}/browser` WebSocket（契约见 docs/browser-bridge.md）。

让 agent 反向操作用户**正在用的那个浏览器**。方向是定死的——浏览器扩展不能监听端口，
只能扩展主动连 serve；连上后它声明自己支持哪些工具，本模块把这些工具注册进**该会话**的
Toolbox，模型调用时经 socket 发 `call`、在工作线程里阻塞等 `result`（与审批同一等法）。

线程模型与 `_HttpApprover` 一样：socket 收发只在事件循环线程上；工具 handler 在工作线程里
跑，经 `run_coroutine_threadsafe` 发帧、在 `threading.Event` 上等结果。在途调用表单独配锁。
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import media
from .tools import Tool, Toolbox

DEFAULT_CALL_TIMEOUT = 60.0
#  等第一帧 hello 的上限：扩展连上就该立刻自报，磨蹭的连接不留
HELLO_TIMEOUT = 10.0

#  关闭码：4xxx 是应用自定义段
CLOSE_UNAUTHORIZED = 4401
CLOSE_NOT_FOUND = 4404
CLOSE_REPLACED = 4409
CLOSE_SESSION_CLOSED = 4410

_TAB = {"type": "integer", "description": "标签页 id（browser_tabs 里的），缺省 = 侧栏当前所在的标签页"}
_REF = {"type": "string", "description": "元素 ref（browser_read_page 的 interactive 模式里的 [eNN]）"}

#  v1 工具清单：名字、描述、schema、审批默认全由服务端定义；扩展只声明"我做得了哪些"。
#  顺序 = 注册顺序 = 请求里的工具顺序，会话内稳定，不要重排。
BROWSER_TOOLS: dict[str, dict[str, Any]] = {
    "browser_tabs": {
        "description": "列出浏览器里打开的标签页：每行 tab_id · 标题 · URL，当前标签页标 *。",
        "parameters": {"type": "object", "properties": {}, "required": []},
        "requires_approval": False,
    },
    "browser_open": {
        "description": "在用户的浏览器里开一个新标签页并加载 url，返回 tab_id。",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "要打开的 URL"},
                "active": {"type": "boolean", "description": "是否切到前台，默认 true"},
            },
            "required": ["url"],
        },
        "requires_approval": True,
    },
    "browser_navigate": {
        "description": "让某个标签页跳转到 url，等加载完成后返回页面标题。",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "目标 URL"}, "tab_id": _TAB},
            "required": ["url"],
        },
        "requires_approval": True,
    },
    "browser_read_page": {
        "description": (
            "读取标签页内容。mode=text 返回标题、URL 与正文；mode=interactive 额外列出可交互元素，"
            "每个带 ref（如 [e12] button \"提交\"），browser_click / browser_type 只认这个 ref。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "tab_id": _TAB,
                "mode": {"type": "string", "enum": ["text", "interactive"], "description": "默认 text"},
                "max_chars": {"type": "integer", "description": "正文最多返回多少字，默认 12000"},
            },
            "required": [],
        },
        "requires_approval": False,
    },
    "browser_click": {
        "description": "点击页面元素（用 browser_read_page interactive 模式给出的 ref），返回点击后标题 / URL 是否变化。",
        "parameters": {"type": "object", "properties": {"ref": _REF, "tab_id": _TAB}, "required": ["ref"]},
        "requires_approval": True,
    },
    "browser_type": {
        "description": "向输入元素（ref）键入文本；submit=true 时随后按回车。",
        "parameters": {
            "type": "object",
            "properties": {
                "ref": _REF,
                "text": {"type": "string", "description": "要输入的文本"},
                "submit": {"type": "boolean", "description": "输完是否按回车，默认 false"},
                "tab_id": _TAB,
            },
            "required": ["ref", "text"],
        },
        "requires_approval": True,
    },
    "browser_screenshot": {
        "description": "截取标签页当前可见区域，图片作为下一条消息附上。",
        "parameters": {"type": "object", "properties": {"tab_id": _TAB}, "required": []},
        "requires_approval": False,
    },
}


@dataclass
class _Call:
    """一次在途调用。`done` 由事件循环线程 set，工作线程在它上面阻塞。"""

    id: str
    tool: str
    created_at: float
    done: threading.Event = field(default_factory=threading.Event)
    ok: bool = False
    content: str = ""
    error: str = ""
    image: dict[str, Any] | None = None


class BrowserBridge:
    """一个会话的浏览器桥：一条 WebSocket + 在途调用表 + 注册进 Toolbox 的工具。

    生命周期：`attach()`（hello 已验过）→ `handle_frame()` 逐帧 → `detach()`。
    `call()` 是工具 handler，在工作线程里跑。
    """

    def __init__(
        self,
        session_id: str,
        toolbox: Toolbox,
        loop: asyncio.AbstractEventLoop,
        timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self.session_id = session_id
        self.toolbox = toolbox
        self.loop = loop
        self.timeout = timeout
        self.ws: Any = None
        self.client = ""
        self.registered: list[str] = []
        self.ignored: list[str] = []
        self.connected_at = 0.0
        self._calls: dict[str, _Call] = {}
        self._lock = threading.Lock()

    # ---------- 事件循环线程 ----------

    async def attach(self, ws: Any, hello: dict[str, Any]) -> None:
        self.ws = ws
        self.client = str(hello.get("client") or "")
        self.connected_at = time.time()
        supports = hello.get("supports")
        if not isinstance(supports, list):
            supports = list(BROWSER_TOOLS)
        wanted = {str(name) for name in supports}
        #  注册顺序按服务端清单、不按扩展声明的顺序：工具顺序是每轮请求的前缀
        #  （prompt cache 资产），同一批工具在任何扩展上都得是同一个顺序
        self.registered = [name for name in BROWSER_TOOLS if name in wanted]
        self.ignored = sorted(wanted - set(BROWSER_TOOLS))
        for name in self.registered:
            spec = BROWSER_TOOLS[name]
            self.toolbox.register(
                Tool(
                    name=name,
                    description=spec["description"],
                    parameters=spec["parameters"],
                    handler=self._handler(name),
                    requires_approval=spec["requires_approval"],
                )
            )
        await ws.send_json(
            {
                "type": "hello.ok",
                "session_id": self.session_id,
                "registered": list(self.registered),
                "ignored": list(self.ignored),
                "timeout": self.timeout,
            }
        )

    def handle_frame(self, frame: dict[str, Any]) -> None:
        """扩展来的一帧。只认 result；其它类型忽略（向前兼容）。"""
        if frame.get("type") != "result":
            return
        call_id = str(frame.get("id") or "")
        with self._lock:
            call = self._calls.get(call_id)
        if call is None:
            return  # 超时后才回来的结果：工具那边已经收场，丢掉
        call.ok = bool(frame.get("ok"))
        call.content = str(frame.get("content") or "")
        call.error = str(frame.get("error") or "")
        image = frame.get("image")
        call.image = image if isinstance(image, dict) else None
        call.done.set()

    def detach(self, reason: str) -> None:
        """注销工具、让在途调用以错误收场。幂等。"""
        self.ws = None
        for name in self.registered:
            self.toolbox.unregister(name)
        self.registered = []
        with self._lock:
            calls, self._calls = list(self._calls.values()), {}
        for call in calls:
            call.error = f"浏览器断开（{reason}）"
            call.done.set()

    async def close(self, reason: str, code: int = CLOSE_SESSION_CLOSED) -> None:
        ws, self.ws = self.ws, None
        self.detach(reason)
        if ws is None:
            return
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "bye", "reason": reason})
        with contextlib.suppress(Exception):
            await ws.close(code=code)

    @property
    def connected(self) -> bool:
        return self.ws is not None

    def info(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "client": self.client,
            "tools": list(self.registered),
            "in_flight": len(self._calls),
        }

    # ---------- 工作线程 ----------

    def _handler(self, name: str):
        def handler(**args: Any) -> str:
            return self.call(name, args)

        return handler

    def call(self, tool: str, args: dict[str, Any]) -> str:
        ws = self.ws
        if ws is None:
            return "ERROR: 浏览器未连接，换其它办法。"
        call = _Call(id=uuid.uuid4().hex[:8], tool=tool, created_at=time.time())
        with self._lock:
            self._calls[call.id] = call
        frame = {"type": "call", "id": call.id, "tool": tool, "args": args, "timeout": self.timeout}
        try:
            future = asyncio.run_coroutine_threadsafe(ws.send_json(frame), self.loop)
            future.result(timeout=5.0)
        except Exception as exc:  # noqa: BLE001 - 发不出去就是断了，回给模型让它换办法
            with self._lock:
                self._calls.pop(call.id, None)
            return f"ERROR: 发给浏览器失败：{type(exc).__name__}: {exc}"
        got = call.done.wait(timeout=self.timeout)
        with self._lock:
            self._calls.pop(call.id, None)
        if not got:
            return f"ERROR: 浏览器 {self.timeout:g}s 内没有回应 {tool}。"
        if not call.ok:
            return f"ERROR: {call.error or '浏览器执行失败（未说明原因）'}"
        return self._with_image(call)

    def _with_image(self, call: _Call) -> str:
        if not call.image:
            return call.content
        payload = str(call.image.get("data") or "")
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, TypeError):
            return f"{call.content}\n[截图无法入库：数据不是有效的 base64]".strip()
        ref, error = media.accept(data, "浏览器截图")
        if not ref:
            return f"{call.content}\n[截图无法入库：{error}]".strip()
        self.toolbox.push_media(media.image_part(ref))
        return f"{call.content}\n[截图见下一条消息里的图片]".strip()


def token_matches(offered: Any, expected: str) -> bool:
    """hello 帧里的 token 与服务端配置比对（常数时间，比 bytes——同 require_token）。"""
    if not expected:
        return True
    return hmac.compare_digest(str(offered or "").encode("utf-8"), expected.encode("utf-8"))
