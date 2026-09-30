"""MCP 客户端的失败路径：熔断只数 server 的问题、坏帧与启动异常不留半截状态、
HTTP 往返有总时限、宿主打断停得下在飞的调用。

stdio 部分用 sys.executable 起一个 stdlib 写的假 server（FAKE_SERVER），HTTP 部分
起本机假 server，全部带超时——坏实现只会让用例变红，不会把测试挂死。不打外网。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from xiaoyu import mcp

#  假 stdio server。收到的每条消息追加写进 argv[1]（用例据此看 server 收到了什么）。
#  工具：echo 回显 / strict 缺 text 就答 -32602 / never 永不应答 /
#  late 过 after 秒才应答 / nest 先吐一帧深嵌套 JSON 再正常应答。
#  argv[2] == "deep" 时 tools/list 里多一个 schema 嵌套 300 层的工具
FAKE_SERVER = textwrap.dedent(
    r"""
    import json, sys, threading

    LOG = sys.argv[1]
    lock = threading.Lock()

    def send(obj):
        with lock:
            sys.stdout.write(json.dumps(obj) + "\n")
            sys.stdout.flush()

    def note(msg):
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(msg) + "\n")

    def text(mid, value):
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"content": [{"type": "text", "text": value}]}})

    TOOLS = [
        {"name": name, "description": name,
         "inputSchema": {"type": "object", "properties": {}}}
        for name in ("echo", "strict", "never", "late", "nest")
    ]
    if sys.argv[2:] == ["deep"]:
        schema = {"type": "string"}
        for _ in range(300):
            schema = {"type": "array", "items": schema}
        TOOLS.append({"name": "deep", "description": "deep",
                      "inputSchema": {"type": "object", "properties": {"x": schema}}})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        note(msg)
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = msg["params"]["name"]
            args = msg["params"].get("arguments") or {}
            if name == "strict" and "text" not in args:
                send({"jsonrpc": "2.0", "id": mid,
                      "error": {"code": -32602, "message": "Invalid params: 缺 text"}})
            elif name == "never":
                pass
            elif name == "late":
                timer = threading.Timer(
                    float(args.get("after", 1.0)), text, (mid, "迟到的应答" + "x" * 1000))
                timer.daemon = True
                timer.start()
            elif name == "nest":
                with lock:
                    sys.stdout.write("[" * 200000 + "]" * 200000 + "\n")
                    sys.stdout.flush()
                text(mid, "坏帧之后的应答")
            else:
                text(mid, "echo: " + str(args.get("text", "")))
    """
)


class _StdioCase(unittest.TestCase):
    """真子进程的假 stdio server 夹具（本类不含用例）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.script = self.root / "fake_server.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.log = self.root / "received.jsonl"
        patcher = mock.patch.object(mcp, "user_config_dir", lambda: self.root / "userconf")
        patcher.start()
        self.addCleanup(patcher.stop)

    def spec(self, timeout: float = 10.0, mode: str = "") -> mcp.ServerSpec:
        return mcp.ServerSpec(
            name="fake",
            command=sys.executable,
            args=[str(self.script), str(self.log), *([mode] if mode else [])],
            timeout=timeout,
        )

    def make_server(self, timeout: float = 10.0) -> mcp.McpServer:
        server = mcp.McpServer(self.spec(timeout), log_path=self.root / "fake.log")
        self.addCleanup(server.close)
        server.bootstrap()
        return server

    def received(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def wait_until(self, predicate, timeout: float = 10.0, message: str = "等待超时"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(message)


def _stub_server(test: unittest.TestCase) -> mcp.McpServer:
    """不起进程的 server：假装进程活着，请求层由用例打桩。"""
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    server = mcp.McpServer(mcp.ServerSpec(name="s", command="x"), log_path=Path(tmp.name) / "log")
    server._proc = mock.Mock()
    server._proc.poll.return_value = None
    return server


class BreakerCountsOnlyServerTroubleTest(_StdioCase):
    """熔断数的是"server 没能好好应答"。server 明确答复"这次请求不行"
    （JSON-RPC error、HTTP 4xx）说明它是健康的，错在调用方。"""

    def test_three_rejected_calls_do_not_lock_out_a_healthy_server(self):
        server = self.make_server()
        for _ in range(mcp.McpServer._BREAKER_THRESHOLD):
            out = server.call_tool("strict", {})
            self.assertIn("-32602", out)
        #  第四次把参数写对了：必须真的到达 server
        self.assertEqual(server.call_tool("strict", {"text": "对了"}), "echo: 对了")

    def test_answered_error_carries_its_own_kind(self):
        server = self.make_server()
        with self.assertRaises(mcp.McpError) as caught:
            server._request("tools/call", {"name": "strict", "arguments": {}}, timeout=10.0)
        self.assertEqual(caught.exception.kind, "rpc")
        self.assertFalse(caught.exception.outcome_unknown)

    def test_http_4xx_is_not_counted(self):
        server = _stub_server(self)
        rejected = mcp.McpError("HTTP 422 Unprocessable Entity", kind="http", status=422)
        with mock.patch.object(server, "_request", side_effect=rejected):
            for _ in range(mcp.McpServer._BREAKER_THRESHOLD + 1):
                out = server.call_tool("t", {})
                self.assertIn("HTTP 422", out)
                self.assertNotIn("熔断", out)

    def test_server_side_failures_still_open_the_breaker(self):
        for kind in ("timeout", "dropped", "server", "malformed", ""):
            with self.subTest(kind=kind):
                server = _stub_server(self)
                failure = mcp.McpError("坏了", kind=kind)
                with mock.patch.object(server, "_request", side_effect=failure):
                    for _ in range(mcp.McpServer._BREAKER_THRESHOLD):
                        self.assertIn("MCP 调用失败", server.call_tool("t", {}))
                    self.assertIn("熔断", server.call_tool("t", {}))

    def test_an_answer_resets_the_failure_streak(self):
        """server 刚明确答复过一次，之前的连败就不再是"连"败。"""
        server = _stub_server(self)
        timeout = mcp.McpError("超时", kind="timeout")
        rejected = mcp.McpError("Invalid params（code -32602）", kind="rpc")
        outcomes = [timeout, timeout, rejected, timeout, timeout]
        with mock.patch.object(server, "_request", side_effect=outcomes):
            for _ in outcomes:
                server.call_tool("t", {})
        with mock.patch.object(server, "_request", return_value={"content": []}):
            self.assertNotIn("熔断", server.call_tool("t", {}))


DEEP_FRAME = "[" * 200000 + "]" * 200000


class ReaderSurvivesBadFramesTest(_StdioCase):
    """读线程是这条连接唯一的耳朵：一帧解析不了只丢这一帧；真读不下去了
    就按断线收场，不能留下"进程活着、没人读、也没判死"的状态。"""

    def test_deeply_nested_frame_costs_one_frame_not_the_server(self):
        server = self.make_server(timeout=5.0)
        started = time.monotonic()
        self.assertEqual(server.call_tool("nest", {}), "坏帧之后的应答")
        self.assertLess(time.monotonic() - started, 4.0, "调用是等满超时才回来的")
        #  读线程还在：之后的调用照常有人接
        self.assertEqual(server.call_tool("echo", {"text": "还在"}), "echo: 还在")

    def test_reader_that_cannot_go_on_is_treated_as_a_disconnect(self):
        server = self.make_server(timeout=8.0)
        disconnected = threading.Event()
        server.on_disconnect = disconnected.set
        real_loads = json.loads

        def loads(text, *args, **kwargs):
            if isinstance(text, str) and "poison" in text:
                raise RuntimeError("解析器内部出错")
            return real_loads(text, *args, **kwargs)

        started = time.monotonic()
        with mock.patch.object(mcp.json, "loads", side_effect=loads):
            out = server.call_tool("echo", {"text": "poison"})
        self.assertLess(time.monotonic() - started, 6.0, "在等的请求没被唤醒，干等到了超时")
        self.assertIn("进程已退出", out)
        self.assertTrue(disconnected.wait(5.0), "没有报断线：没人会去重连")
        self.wait_until(lambda: not server.alive(), message="没人读的进程还留着")

    def test_unparseable_sse_event_is_skipped(self):
        reply = {"jsonrpc": "2.0", "id": 1, "result": {}}
        body = f"data: {DEEP_FRAME}\n\ndata: {json.dumps(reply)}\n\n".encode()
        self.assertEqual(list(mcp._read_sse(io.BytesIO(body))), [reply])


def _deep_tool(depth: int) -> dict:
    schema: dict = {"type": "string"}
    for _ in range(depth):
        schema = {"type": "array", "items": schema}
    return {"name": "deep", "description": "d",
            "inputSchema": {"type": "object", "properties": {"x": schema}}}


class DeclarationDepthTest(unittest.TestCase):
    PLAIN = {"name": "plain", "description": "d",
             "inputSchema": {"type": "object", "properties": {}}}

    def test_realistic_nesting_is_accepted(self):
        self.assertIsNone(mcp.declared_violation([self.PLAIN, _deep_tool(20)]))

    def test_excessive_nesting_rejects_the_whole_list(self):
        #  5000 层：量深度的函数自己要是递归的，这里先栈溢出
        reason = mcp.declared_violation([self.PLAIN, _deep_tool(5000)])
        self.assertIsNotNone(reason)
        self.assertIn("嵌套", reason)
        self.assertIn("deep", reason)


class BootThreadFailureTest(_StdioCase):
    """启动线程出任何事都要落到一个终态：状态 failed 带原因、子进程收掉、
    不留"基线写了、工具没注册"的半截。"""

    def boot(self, mode: str = "", wait: float = 8.0) -> tuple[mcp.McpManager, list[mcp.McpServer]]:
        created: list[mcp.McpServer] = []

        class Recording(mcp.McpServer):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                created.append(self)

        manager = mcp.McpManager([self.spec(mode=mode)])
        self.addCleanup(manager.close)
        with mock.patch.object(mcp, "McpServer", Recording):
            manager.start()
            manager.wait_ready(wait)
        return manager, created

    def baseline_file(self) -> Path:
        return self.root / "userconf" / "mcp-approved.json"

    def assert_reaped(self, servers: list[mcp.McpServer]) -> None:
        self.assertEqual(len(servers), 1)
        proc = servers[0]._proc
        self.assertIsNotNone(proc)
        self.wait_until(lambda: proc.poll() is not None, message="启动失败的 server 进程没被收掉")

    def test_over_deep_schema_fails_the_server_with_a_reason(self):
        manager, servers = self.boot(mode="deep")
        state = manager._states["fake"]
        self.assertTrue(state.startswith("failed"), state)
        self.assertIn("嵌套", state)
        #  整代拒绝：不是丢掉那一个工具后带着其余的继续
        self.assertEqual(manager.ready_tools(), [])
        self.assertFalse(self.baseline_file().exists(), "非法的一代不该落进基线")
        self.assert_reaped(servers)

    def test_unexpected_error_while_registering_lands_in_failed_state(self):
        with mock.patch.object(mcp, "_make_remote_tool", side_effect=RuntimeError("建工具时出错")):
            manager, servers = self.boot(wait=6.0)
        self.assertFalse(manager.loading(), "状态停在 loading：wait_ready 每次都要等满超时")
        state = manager._states["fake"]
        self.assertTrue(state.startswith("failed"), state)
        self.assertIn("建工具时出错", state)
        self.assertEqual(manager.ready_tools(), [])
        self.assertEqual(manager._baseline, {})
        self.assertFalse(self.baseline_file().exists(), "工具没建成，基线却已经落盘")
        self.assert_reaped(servers)

    def test_failed_build_leaves_the_previous_generation_and_baseline_untouched(self):
        manager, _ = self.boot()
        self.assertEqual(manager._states["fake"], "ready")
        server = manager._servers["fake"]
        names = [tool.name for tool in manager.ready_tools()]
        on_disk = self.baseline_file().read_text(encoding="utf-8")
        in_memory = json.loads(json.dumps(manager._baseline))
        grown = [*server.live_declared,
                 {"name": "brand_new", "description": "新工具",
                  "inputSchema": {"type": "object", "properties": {}}}]
        with mock.patch.object(mcp, "_make_remote_tool", side_effect=RuntimeError("建工具时出错")):
            with manager._lock, self.assertRaises(RuntimeError):
                manager._swap_generation_locked("fake", server, grown)
        self.assertEqual(manager._baseline, in_memory)
        self.assertEqual(self.baseline_file().read_text(encoding="utf-8"), on_disk)
        self.assertEqual(manager._declared["fake"], server.live_declared)
        self.assertEqual([tool.name for tool in manager.ready_tools()], names)
        self.assertEqual(manager.ready_tools()[0].handler(text="上一代还在"), "echo: 上一代还在")

    def test_a_good_generation_still_writes_the_baseline(self):
        """落笔挪到建工具之后，成功路径照样要把首见的指纹记下来。"""
        manager, _ = self.boot()
        recorded = json.loads(self.baseline_file().read_text(encoding="utf-8"))
        self.assertEqual(
            sorted(recorded["fake"]), ["echo", "late", "nest", "never", "strict"]
        )
        self.assertEqual(len(manager.ready_tools()), 5)


class _Handler(BaseHTTPRequestHandler):
    """假的 Streamable HTTP MCP server。每个 tools/call 从 script 弹一个动作：

    plain 直接回 JSON / linger SSE 里立刻给应答、流拖着不关 / silent 只发心跳
    永不应答 / ping 先在流里反向发一个 ping、等到我方答复才给应答 /
    notify 先发 list_changed 再给应答、流拖着不关 / no_reply 发一条通知就关流 /
    slow 压着响应头不发（等 release）。
    """

    protocol_version = "HTTP/1.1"
    script: list = []
    seen: list = []
    ping_answered = threading.Event()
    release = threading.Event()

    def log_message(self, *args):
        pass

    def _status(self, code: int) -> None:
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, payload: dict, session: str = "") -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if session:
            self.send_header("Mcp-Session-Id", session)
        self.end_headers()
        self.wfile.write(body)

    def _open_stream(self) -> None:
        #  不给长度：流到连接关闭为止，什么时候关由这边说了算
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _event(self, payload: dict) -> None:
        self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
        self.wfile.flush()

    def _heartbeats(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not type(self).release.is_set():
            self.wfile.write(b": keep-alive\n\n")
            self.wfile.flush()
            time.sleep(0.05)

    def do_POST(self) -> None:
        cls = type(self)
        length = int(self.headers.get("Content-Length") or 0)
        message = json.loads(self.rfile.read(length) or b"{}")
        cls.seen.append(message)
        method, mid = message.get("method"), message.get("id")
        if method is None:
            #  我方对反向请求的答复
            if mid == "srv-ping":
                cls.ping_answered.set()
            self._status(202)
        elif method == "initialize":
            self._json({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake-http", "version": "1"}}}, session="sess-1")
        elif mid is None:
            self._status(202)
        elif method == "tools/list":
            self._json({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": "echo", "description": "d",
                 "inputSchema": {"type": "object", "properties": {}}}]}})
        else:
            #  客户端拿到想要的就挂断，写端撞上断开的连接属预期
            try:
                self._call(cls.script.pop(0) if cls.script else "plain", mid)
            except OSError:
                pass

    def _call(self, action: str, mid) -> None:
        cls = type(self)

        def reply(text: str = "应答") -> dict:
            return {"jsonrpc": "2.0", "id": mid,
                    "result": {"content": [{"type": "text", "text": text}]}}

        if action == "plain":
            self._json(reply())
        elif action == "slow":
            cls.release.wait(6)
            self._json(reply("迟到的应答"))
        elif action == "linger":
            self._open_stream()
            self._event(reply())
            self._heartbeats(4)
        elif action == "silent":
            self._open_stream()
            self._heartbeats(6)
        elif action == "ping":
            self._open_stream()
            self._event({"jsonrpc": "2.0", "id": "srv-ping", "method": "ping"})
            answered = cls.ping_answered.wait(4)
            self._event(reply("ping 已答" if answered else "ping 没人答"))
        elif action == "notify":
            self._open_stream()
            self._event({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
            self._event(reply())
            self._heartbeats(4)
        elif action == "no_reply":
            self._open_stream()
            self._event({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})

    def do_GET(self) -> None:
        self._status(405)

    def do_DELETE(self) -> None:
        self._status(200)


class _HttpCase(unittest.TestCase):
    """本机假 HTTP MCP server 的夹具（本类不含用例）。"""

    def setUp(self):
        _Handler.script = []
        _Handler.seen = []
        _Handler.ping_answered = threading.Event()
        _Handler.release = threading.Event()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/mcp"
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        def teardown():
            _Handler.release.set()
            self.httpd.shutdown()
            self.httpd.server_close()

        self.addCleanup(teardown)

    def make_server(self, timeout: float) -> mcp.McpServer:
        spec = mcp.ServerSpec(name="remote", command="", url=self.url, timeout=timeout)
        server = mcp.McpServer(spec, Path(self.tmp.name) / "remote.log")
        self.addCleanup(server.close)
        server.bootstrap()
        return server

    def readers(self) -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name.startswith("xiaoyu-mcp-post-")]

    def wait_until(self, predicate, timeout: float = 10.0, message: str = "等待超时"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(message)


class HttpRoundtripTest(_HttpCase):
    """一次 HTTP 调用多久回来由两件事决定：应答到了没有、总时限到了没有。
    流什么时候关、server 发不发心跳，都不该算进去。"""

    def test_returns_when_the_answer_arrives_not_when_the_stream_ends(self):
        server = self.make_server(timeout=10.0)
        _Handler.script = ["linger"]
        started = time.monotonic()
        self.assertEqual(server.call_tool("echo", {}), "应答")
        self.assertLess(time.monotonic() - started, 2.0, "等到流关闭才返回")

    def test_heartbeats_do_not_extend_the_total_time_limit(self):
        server = self.make_server(timeout=1.0)
        _Handler.script = ["silent"]
        started = time.monotonic()
        out = server.call_tool("echo", {})
        elapsed = time.monotonic() - started
        self.assertIn("超时", out)
        self.assertIn("调用结果不确定", out)
        self.assertLess(elapsed, 3.0, "心跳一直在给超时续期")
        self.assertTrue(server.alive(), "一次超时不能把 server 判死")
        #  那条流还在发心跳：读线程不该留在上面，写锁也不该还被占着
        self.wait_until(lambda: not self.readers(), 3.0, "读线程还挂在没人要的流上")
        started = time.monotonic()
        self.assertEqual(server.call_tool("echo", {}), "应答")
        self.assertLess(time.monotonic() - started, 2.0, "下一个调用被上一条流挡住了")

    def test_server_request_inside_the_stream_is_answered_mid_call(self):
        """server 在应答流里夹一个 ping、等我方答了才给结果：答复必须在这次
        往返当中发出去，而不是等流结束（那时 server 早等不及了）。"""
        server = self.make_server(timeout=8.0)
        _Handler.script = ["ping"]
        self.assertEqual(server.call_tool("echo", {}), "ping 已答")

    def test_notifications_before_the_answer_are_still_dispatched(self):
        server = self.make_server(timeout=10.0)
        changed = threading.Event()
        server.on_tools_changed = changed.set
        _Handler.script = ["notify"]
        started = time.monotonic()
        self.assertEqual(server.call_tool("echo", {}), "应答")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(changed.is_set(), "应答之前的 list_changed 通知丢了")

    def test_stream_that_ends_without_the_answer_fails_without_waiting(self):
        server = self.make_server(timeout=6.0)
        _Handler.script = ["no_reply"]
        started = time.monotonic()
        out = server.call_tool("echo", {})
        self.assertLess(time.monotonic() - started, 3.0, "流都关了还在等应答")
        self.assertIn("没有给出", out)
        self.assertIn("调用结果不确定", out)
        self.assertTrue(server.alive())

    def test_plain_json_answer_still_works(self):
        server = self.make_server(timeout=5.0)
        self.assertEqual(server.call_tool("echo", {}), "应答")
        self.wait_until(lambda: not self.readers(), 3.0, "读线程没有随往返结束")


if __name__ == "__main__":
    unittest.main()
