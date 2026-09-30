"""MCP 客户端的失败路径：熔断只数 server 的问题、坏帧与启动异常不留半截状态、
HTTP 往返有总时限、宿主打断停得下在飞的调用。

stdio 部分用 sys.executable 起一个 stdlib 写的假 server（FAKE_SERVER），HTTP 部分
起本机假 server，全部带超时——坏实现只会让用例变红，不会把测试挂死。不打外网。
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import mcp

#  假 stdio server。收到的每条消息追加写进 argv[1]（用例据此看 server 收到了什么）。
#  工具：echo 回显 / strict 缺 text 就答 -32602 / never 永不应答 /
#  late 过 after 秒才应答 / nest 先吐一帧深嵌套 JSON 再正常应答
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

    def spec(self, timeout: float = 10.0, name: str = "fake") -> mcp.ServerSpec:
        return mcp.ServerSpec(
            name=name,
            command=sys.executable,
            args=[str(self.script), str(self.log)],
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


if __name__ == "__main__":
    unittest.main()
