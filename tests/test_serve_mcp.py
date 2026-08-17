"""serve 的 MCP 面（/mcp）的进程内 e2e：真 app + 真 Agent + scripted 桩。

被测的是 serve_mcp.py 的协议面：initialize 协商、tools/list、tools/call 的
SSE 应答与 progress 通知、xiaoyu → xiaoyu_reply → xiaoyu_close 的会话接力、
以及最要紧的**跨面审批回路**——MCP 侧 tools/call 挂在 waiting_for_approval，
REST 侧 POST /permissions 放行，两张脸共享同一会话层，这条回路必须真通。

复用 test_serve.ServeCase 的驱动（写脚本 → 隔离环境 → 造 app → TestClient）；
fastapi/uvicorn 没装就整体跳过，与 test_serve 同一纪律。
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from typing import Any

#  刻意不把 TestBasics import 进模块命名空间：unittest 按"模块里的 TestCase
#  子类"发现用例，导进来它那 8 个用例会在这个文件里再跑一遍
from tests.test_serve import HAS_FASTAPI, ServeCase


@unittest.skipUnless(HAS_FASTAPI, "需要可选额外 [serve]（fastapi + uvicorn）")
class McpCase(ServeCase):
    """MCP 侧的公共动作：发 JSON-RPC、消费 tools/call 的 SSE 流。"""

    def wait_for_detail(self, session_id: str, detail: str, timeout: float = 10.0):
        from tests.test_serve import TestBasics

        return TestBasics._wait_for(self, session_id, detail, timeout)

    def rpc(self, payload: dict[str, Any] | list[Any]):
        return self.client.post("/mcp", json=payload, headers=self.headers())

    def initialize(self, version: str = "2025-06-18") -> dict[str, Any]:
        response = self.rpc(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": version,
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            }
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        progress_token: Any = None,
        request_id: int = 7,
    ) -> list[dict[str, Any]]:
        """tools/call → 全部 SSE 帧（progress 通知们 + 最后一帧响应）。"""
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if progress_token is not None:
            params["_meta"] = {"progressToken": progress_token}
        frames: list[dict[str, Any]] = []
        with self.client.stream(
            "POST",
            "/mcp",
            json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params},
            headers=self.headers(),
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            for line in response.iter_lines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
        return frames

    def final(self, frames: list[dict[str, Any]], request_id: int = 7) -> dict[str, Any]:
        """流的最后一帧必须是本次调用的 JSON-RPC 响应。"""
        self.assertTrue(frames, "流里一帧都没有")
        last = frames[-1]
        self.assertEqual(last.get("id"), request_id, last)
        return last


class TestHandshake(McpCase):
    def test_initialize_echoes_supported_version(self):
        self.start("text: 无所谓\n")
        body = self.initialize("2025-03-26")
        self.assertEqual(body["result"]["protocolVersion"], "2025-03-26")
        self.assertIn("tools", body["result"]["capabilities"])
        self.assertEqual(body["result"]["serverInfo"]["name"], "xiaoyu")

    def test_unknown_version_falls_back_to_latest(self):
        #  spec 的协商规则：客户端要的版本没有，就报自己最新的，去留客户端定
        self.start("text: 无所谓\n")
        body = self.initialize("1999-01-01")
        self.assertEqual(body["result"]["protocolVersion"], "2025-06-18")

    def test_initialized_notification_gets_202(self):
        self.start("text: 无所谓\n")
        response = self.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")

    def test_initialize_is_idempotent(self):
        #  无状态 server：adapters 默认每次工具调用前都重新 initialize，
        #  发几次都得是同一个答案，不能第二次报"已初始化"
        self.start("text: 无所谓\n")
        self.assertEqual(self.initialize(), self.initialize())

    def test_ping_pongs(self):
        self.start("text: 无所谓\n")
        body = self.rpc({"jsonrpc": "2.0", "id": 3, "method": "ping"}).json()
        self.assertEqual(body["result"], {})


class TestToolsList(McpCase):
    def test_lists_the_three_tools_with_schemas(self):
        self.start("text: 无所谓\n")
        body = self.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()
        tools = {tool["name"]: tool for tool in body["result"]["tools"]}
        self.assertEqual(set(tools), {"xiaoyu", "xiaoyu_reply", "xiaoyu_close"})
        self.assertEqual(tools["xiaoyu"]["inputSchema"]["required"], ["prompt"])
        self.assertEqual(
            tools["xiaoyu_reply"]["inputSchema"]["required"], ["session_id", "prompt"]
        )
        #  声明了 outputSchema，消费方才能按结构化字段写解析
        for name in ("xiaoyu", "xiaoyu_reply", "xiaoyu_close"):
            self.assertIn("outputSchema", tools[name])


class TestToolCall(McpCase):
    def test_xiaoyu_runs_a_turn_and_returns_session_id(self):
        self.start("text: 干完了\n")
        frames = self.call_tool("xiaoyu", {"prompt": "干活"})
        result = self.final(frames)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["content"], [{"type": "text", "text": "干完了"}])
        structured = result["structuredContent"]
        self.assertEqual(structured["text"], "干完了")
        self.assertEqual((structured["status"], structured["detail"]), ("idle", "finished"))
        self.assertEqual(structured["turns"], 1)
        #  MCP 建的会话在 REST 面也看得见：同一个会话层的两张脸
        session_id = structured["session_id"]
        self.assertTrue(session_id)
        rest = self.client.get(f"/session/{session_id}", headers=self.headers())
        self.assertEqual(rest.status_code, 200, rest.text)

    def test_reply_continues_the_same_session(self):
        self.start("text: 第一轮\n---\ntext: 第二轮\n")
        first = self.final(self.call_tool("xiaoyu", {"prompt": "开工"}))["result"]
        session_id = first["structuredContent"]["session_id"]
        second = self.final(
            self.call_tool("xiaoyu_reply", {"session_id": session_id, "prompt": "接着"})
        )["result"]
        structured = second["structuredContent"]
        self.assertEqual(structured["session_id"], session_id)
        self.assertEqual(structured["text"], "第二轮")
        self.assertEqual(structured["turns"], 2)

    def test_reply_to_unknown_session_is_tool_error(self):
        #  执行错误走 result.isError（模型看得到原文，能自我纠偏），
        #  不是协议错误——调用本身发得没毛病
        self.start("text: 无所谓\n")
        result = self.final(
            self.call_tool("xiaoyu_reply", {"session_id": "sess-nope", "prompt": "在吗"})
        )["result"]
        self.assertTrue(result["isError"])
        self.assertIn("sess-nope", result["content"][0]["text"])
        #  isError 分支也要给全 outputSchema 的 required 字段
        self.assertEqual(result["structuredContent"]["status"], "error")

    def test_close_releases_the_session_on_both_faces(self):
        self.start("text: 好\n")
        session_id = self.final(self.call_tool("xiaoyu", {"prompt": "干"}))["result"][
            "structuredContent"
        ]["session_id"]
        closed = self.final(self.call_tool("xiaoyu_close", {"session_id": session_id}))["result"]
        self.assertFalse(closed["isError"])
        self.assertEqual(closed["structuredContent"], {"session_id": session_id, "closed": True})
        #  REST 面同步消失；再 reply 是干净的工具错误
        rest = self.client.get(f"/session/{session_id}", headers=self.headers())
        self.assertEqual(rest.status_code, 404)
        again = self.final(
            self.call_tool("xiaoyu_reply", {"session_id": session_id, "prompt": "还在吗"})
        )["result"]
        self.assertTrue(again["isError"])

    def test_progress_notifications_report_tool_lifecycle(self):
        self.start(
            'tool_call: {"name": "write_file", "arguments": {"path": "p.txt", "content": "x\\n"}}\n'
            "---\n"
            "text: 好了\n"
        )
        frames = self.call_tool("xiaoyu", {"prompt": "写", "mode": "auto"}, progress_token="tok-1")
        notifications = [item for item in frames if item.get("method") == "notifications/progress"]
        self.assertTrue(notifications, "带 progressToken 却一条 progress 都没有")
        for item in notifications:
            self.assertEqual(item["params"]["progressToken"], "tok-1")
        #  progress 必须单调递增（spec 要求），且工具生命周期报了站
        values = [item["params"]["progress"] for item in notifications]
        self.assertEqual(values, sorted(values))
        messages = [item["params"]["message"] for item in notifications]
        self.assertTrue(any(text.startswith("tool.pending") for text in messages), messages)
        self.assertTrue(any(text.startswith("run.completed") for text in messages), messages)

    def test_no_progress_without_token(self):
        #  spec：没给 progressToken 就不许发 progress 通知
        self.start("text: 静音\n")
        frames = self.call_tool("xiaoyu", {"prompt": "干"})
        self.assertEqual(
            [item for item in frames if item.get("method") == "notifications/progress"], []
        )

    def test_busy_session_is_a_tool_error_not_a_hang(self):
        #  第一轮挂在审批上（ask 档 + write_file），reply 必须立刻报忙
        self.start('tool_call: {"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}\n')
        session_id = self.new_session()
        self.client.post(
            f"/session/{session_id}/prompt_async", json={"text": "写"}, headers=self.headers()
        )
        self.wait_for_detail(session_id, "waiting_for_approval")
        result = self.final(
            self.call_tool("xiaoyu_reply", {"session_id": session_id, "prompt": "再来"})
        )["result"]
        self.assertTrue(result["isError"])
        self.client.post(f"/session/{session_id}/abort", headers=self.headers())

    def test_workspace_outside_root_is_a_tool_error(self):
        self.start("text: 无所谓\n")
        result = self.final(self.call_tool("xiaoyu", {"prompt": "干", "workspace": "/etc"}))["result"]
        self.assertTrue(result["isError"])
        self.assertIn("root", result["content"][0]["text"])


class TestCrossSurfaceApproval(McpCase):
    """这层的招牌能力：MCP 侧挂起等审批，REST 侧放行——同一会话层的跨面回路。"""

    SCRIPT = (
        'tool_call: {"name": "write_file", "arguments": {"path": "跨面.txt", "content": "过\\n"}}\n'
        "---\n"
        "text: 放行后干完了\n"
    )

    def test_rest_allow_unblocks_the_mcp_call(self):
        self.start(self.SCRIPT)
        failures: list[str] = []

        def approve_from_rest() -> None:
            #  另一个线程扮演 REST 侧的审批人：等到挂起出现，放行
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                listing = self.client.get("/session", headers=self.headers()).json()["sessions"]
                waiting = [s for s in listing if s["detail"] == "waiting_for_approval"]
                if waiting:
                    session = waiting[0]
                    response = self.client.post(
                        f"/session/{session['session_id']}/permissions",
                        json={
                            "request_id": session["pending_approvals"][0]["request_id"],
                            "decision": "allow",
                        },
                        headers=self.headers(),
                    )
                    if response.status_code != 200:
                        failures.append(response.text)
                    return
                time.sleep(0.05)
            failures.append("10s 内没等到 waiting_for_approval")

        approver = threading.Thread(target=approve_from_rest)
        approver.start()
        try:
            frames = self.call_tool("xiaoyu", {"prompt": "建个文件"}, progress_token="tok-x")
        finally:
            approver.join()
        self.assertEqual(failures, [])
        result = self.final(frames)["result"]
        self.assertFalse(result["isError"], result)
        self.assertEqual(result["structuredContent"]["text"], "放行后干完了")
        self.assertTrue((self.root / "跨面.txt").exists())
        #  挂起与放行都要在 progress 里报过站——编排侧全靠它知道该去放行
        messages = [
            item["params"]["message"]
            for item in frames
            if item.get("method") == "notifications/progress"
        ]
        self.assertTrue(any(text.startswith("permission.requested") for text in messages), messages)

    def test_nobody_answers_times_out_into_denial(self):
        #  fail closed 穿透到 MCP 面：没人放行 → 3s 超时按拒绝 → 一轮正常收尾、
        #  文件没写出来。调用方拿到的不是错误而是"干完了但被拦了"的事实
        self.start(self.SCRIPT)
        frames = self.call_tool("xiaoyu", {"prompt": "建个文件"})
        result = self.final(frames)["result"]
        self.assertFalse(result["isError"])
        self.assertEqual(result["structuredContent"]["detail"], "finished")
        self.assertFalse((self.root / "跨面.txt").exists())


class TestTransportGuards(McpCase):
    def test_get_and_delete_are_405(self):
        self.start("text: 无所谓\n")
        self.assertEqual(self.client.get("/mcp", headers=self.headers()).status_code, 405)
        self.assertEqual(self.client.delete("/mcp", headers=self.headers()).status_code, 405)

    def test_batch_is_rejected(self):
        self.start("text: 无所谓\n")
        response = self.rpc([{"jsonrpc": "2.0", "id": 1, "method": "ping"}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], -32600)

    def test_malformed_json_is_parse_error(self):
        self.start("text: 无所谓\n")
        response = self.client.post(
            "/mcp",
            content=b"{oops",
            headers={**self.headers(), "content-type": "application/json"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], -32700)

    def test_unknown_method_is_32601(self):
        self.start("text: 无所谓\n")
        body = self.rpc({"jsonrpc": "2.0", "id": 9, "method": "resources/list"}).json()
        self.assertEqual(body["error"]["code"], -32601)

    def test_unknown_tool_is_32602(self):
        self.start("text: 无所谓\n")
        frames = self.call_tool("nope", {})
        self.assertEqual(self.final(frames)["error"]["code"], -32602)

    def test_missing_prompt_is_32602(self):
        self.start("text: 无所谓\n")
        frames = self.call_tool("xiaoyu", {})
        self.assertEqual(self.final(frames)["error"]["code"], -32602)

    def test_mcp_stays_out_of_the_openapi_schema(self):
        #  /openapi.json 是给 Dify/n8n 的 REST 工具清单，混进 JSON-RPC 端点
        #  只会把导入器搞糊涂
        client = self.start("text: 无所谓\n")
        self.assertNotIn("/mcp", client.get("/openapi.json").json()["paths"])


class TestMcpToken(McpCase):
    token = "s3cr3t"

    def test_token_gates_mcp_like_rest(self):
        client = self.start("text: 无所谓\n")
        naked = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(naked.status_code, 401)
        self.assertEqual(self.rpc({"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code, 200)


class TestMcpDisabled(McpCase):
    mcp = False

    def test_no_mcp_flag_removes_the_endpoint(self):
        self.start("text: 无所谓\n")
        response = self.rpc({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
