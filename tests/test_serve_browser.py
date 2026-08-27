"""浏览器桥（/session/{id}/browser）：握手鉴权、按声明注册、调用往返、审批、超时、断线、图片。

与 test_serve 同一套驱动：TestClient 把 app 跑在自己的线程上，工具 handler 在工作线程里
阻塞等 result 的路径是真实发生的，测试用例扮演扩展那一端。
"""

from __future__ import annotations

import time
import unittest
from typing import Any

from tests import test_serve as _ts

HAS_FASTAPI = _ts.HAS_FASTAPI

if HAS_FASTAPI:
    from starlette.websockets import WebSocketDisconnect

#  1×1 透明 PNG
PNG_1PX = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def _quiet_exit(ws: Any) -> None:
    try:
        ws.__exit__(None, None, None)
    except Exception:  # noqa: BLE001 - 已关的连接再退出会抛，测试不关心
        pass


@unittest.skipUnless(HAS_FASTAPI, "需要可选额外 [serve]（fastapi + uvicorn）")
class BrowserCase(_ts.ServeCase):
    token = "s3cr3t"

    def connect(self, session_id: str, supports: list[str] | None = None, token: str | None = None):
        ws = self.client.websocket_connect(f"/session/{session_id}/browser")
        ws.__enter__()
        #  用例里可能已经 __exit__ 过（或服务端先关了），这里只兜底，重复退出无害
        self.addCleanup(lambda: _quiet_exit(ws))
        hello: dict[str, Any] = {"type": "hello", "token": self.token if token is None else token, "client": "test/0"}
        if supports is not None:
            hello["supports"] = supports
        ws.send_json(hello)
        return ws

    def toolbox_names(self, session_id: str) -> list[str]:
        return self.client.app.state.sessions[session_id].agent.toolbox.names()

    def wait_until(self, predicate, timeout: float = 5.0, what: str = "条件") -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(f"{timeout}s 内没等到{what}")

    def completed(self, session_id: str, tool: str) -> dict[str, Any]:
        items = [e for e in self.events(session_id) if e["kind"] == "tool.completed" and e["name"] == tool]
        self.assertEqual(len(items), 1, items)
        return items[0]


class TestHandshake(BrowserCase):
    def test_wrong_token_is_4401(self):
        self.start("text: 无所谓\n")
        session_id = self.new_session()
        ws = self.connect(session_id, token="wrong")
        self.assertEqual(ws.receive_json()["type"], "error")
        with self.assertRaises(WebSocketDisconnect) as ctx:
            ws.receive_json()
        self.assertEqual(ctx.exception.code, 4401)
        self.assertNotIn("browser_tabs", self.toolbox_names(session_id))

    def test_first_frame_must_be_hello(self):
        self.start("text: 无所谓\n")
        session_id = self.new_session()
        with self.client.websocket_connect(f"/session/{session_id}/browser") as ws:
            ws.send_json({"type": "result", "id": "x", "ok": True, "token": self.token})
            self.assertEqual(ws.receive_json()["type"], "error")
            with self.assertRaises(WebSocketDisconnect) as ctx:
                ws.receive_json()
            self.assertEqual(ctx.exception.code, 4401)

    def test_unknown_session_is_4404(self):
        self.start("text: 无所谓\n")
        ws = self.connect("sess-nope")
        self.assertEqual(ws.receive_json()["type"], "error")
        with self.assertRaises(WebSocketDisconnect) as ctx:
            ws.receive_json()
        self.assertEqual(ctx.exception.code, 4404)

    def test_registers_declared_subset_and_unregisters_on_disconnect(self):
        self.start("text: 无所谓\n")
        session_id = self.new_session()
        self.assertNotIn("browser_tabs", self.toolbox_names(session_id))
        ws = self.connect(session_id, supports=["browser_click", "browser_tabs", "bogus_tool"])
        ok = ws.receive_json()
        self.assertEqual(ok["type"], "hello.ok")
        #  注册顺序按服务端清单，不按扩展声明的顺序（工具顺序是 prompt cache 前缀）
        self.assertEqual(ok["registered"], ["browser_tabs", "browser_click"])
        self.assertEqual(ok["ignored"], ["bogus_tool"])
        names = self.toolbox_names(session_id)
        self.assertIn("browser_tabs", names)
        self.assertIn("browser_click", names)
        self.assertNotIn("browser_read_page", names)
        state = self.status(session_id)
        self.assertTrue(state["browser"]["connected"])
        self.assertEqual(state["browser"]["client"], "test/0")
        self.assertEqual(state["browser"]["tools"], ["browser_tabs", "browser_click"])
        kinds = [e["kind"] for e in self.events(session_id)]
        self.assertIn("browser.connected", kinds)

        ws.__exit__(None, None, None)
        self.wait_until(lambda: self.status(session_id)["browser"] is None, what="桥断开")
        self.assertNotIn("browser_tabs", self.toolbox_names(session_id))
        kinds = [e["kind"] for e in self.events(session_id)]
        self.assertIn("browser.disconnected", kinds)

    def test_newer_connection_replaces_older(self):
        self.start("text: 无所谓\n")
        session_id = self.new_session()
        first = self.connect(session_id, supports=["browser_tabs"])
        self.assertEqual(first.receive_json()["type"], "hello.ok")
        second = self.connect(session_id, supports=["browser_tabs", "browser_screenshot"])
        self.assertEqual(second.receive_json()["type"], "hello.ok")
        self.assertEqual(first.receive_json()["type"], "bye")
        with self.assertRaises(WebSocketDisconnect) as ctx:
            first.receive_json()
        self.assertEqual(ctx.exception.code, 4409)
        #  顶掉旧的不能把新的一起注销
        self.assertEqual(self.status(session_id)["browser"]["tools"], ["browser_tabs", "browser_screenshot"])
        self.assertIn("browser_screenshot", self.toolbox_names(session_id))
        second.__exit__(None, None, None)

    def test_closing_session_says_bye(self):
        self.start("text: 无所谓\n")
        session_id = self.new_session()
        ws = self.connect(session_id, supports=["browser_tabs"])
        self.assertEqual(ws.receive_json()["type"], "hello.ok")
        self.client.delete(f"/session/{session_id}", headers=self.headers())
        bye = ws.receive_json()
        self.assertEqual(bye["type"], "bye")
        self.assertEqual(bye["reason"], "session closed")
        with self.assertRaises(WebSocketDisconnect) as ctx:
            ws.receive_json()
        self.assertEqual(ctx.exception.code, 4410)


class TestCalls(BrowserCase):
    def test_readonly_call_round_trip_without_approval(self):
        self.start('tool_call: {"name": "browser_read_page", "arguments": {"mode": "text"}}\n---\ntext: 读到了\n')
        session_id = self.new_session()
        ws = self.connect(session_id, supports=["browser_read_page"])
        self.assertEqual(ws.receive_json()["type"], "hello.ok")
        self.client.post(f"/session/{session_id}/prompt_async", json={"text": "读页"}, headers=self.headers())
        call = ws.receive_json()
        self.assertEqual(call["type"], "call")
        self.assertEqual(call["tool"], "browser_read_page")
        self.assertEqual(call["args"], {"mode": "text"})
        self.assertEqual(call["timeout"], 60.0)
        ws.send_json({"type": "result", "id": call["id"], "ok": True, "content": "标题：首页\n正文……"})
        state = _ts.TestBasics._wait_for(self, session_id, "finished")
        self.assertEqual(state["last_result"]["text"], "读到了")
        done = self.completed(session_id, "browser_read_page")
        self.assertTrue(done["ok"])
        self.assertIn("标题：首页", done["output"])
        kinds = [e["kind"] for e in self.events(session_id)]
        self.assertNotIn("permission.requested", kinds)
        ws.__exit__(None, None, None)

    def test_write_call_waits_for_approval_and_reports_extension_error(self):
        self.start('tool_call: {"name": "browser_click", "arguments": {"ref": "e1"}}\n---\ntext: 完\n')
        session_id = self.new_session()
        ws = self.connect(session_id, supports=["browser_click"])
        self.assertEqual(ws.receive_json()["type"], "hello.ok")
        self.client.post(f"/session/{session_id}/prompt_async", json={"text": "点一下"}, headers=self.headers())
        state = _ts.TestBasics._wait_for(self, session_id, "waiting_for_approval")
        request = state["pending_approvals"][0]
        self.assertEqual(request["tool"], "browser_click")
        self.client.post(
            f"/session/{session_id}/permissions",
            json={"request_id": request["request_id"], "decision": "allow"},
            headers=self.headers(),
        )
        call = ws.receive_json()
        self.assertEqual(call["tool"], "browser_click")
        self.assertEqual(call["args"], {"ref": "e1"})
        ws.send_json({"type": "result", "id": call["id"], "ok": False, "error": "没有 ref e1 这个元素"})
        _ts.TestBasics._wait_for(self, session_id, "finished")
        done = self.completed(session_id, "browser_click")
        self.assertFalse(done["ok"])
        self.assertIn("没有 ref e1", done["output"])
        ws.__exit__(None, None, None)

    def test_denied_write_call_never_reaches_extension(self):
        self.start('tool_call: {"name": "browser_type", "arguments": {"ref": "e2", "text": "hi"}}\n---\ntext: 完\n')
        session_id = self.new_session()
        ws = self.connect(session_id, supports=["browser_type"])
        self.assertEqual(ws.receive_json()["type"], "hello.ok")
        self.client.post(f"/session/{session_id}/prompt_async", json={"text": "输入"}, headers=self.headers())
        state = _ts.TestBasics._wait_for(self, session_id, "waiting_for_approval")
        self.client.post(
            f"/session/{session_id}/permissions",
            json={"request_id": state["pending_approvals"][0]["request_id"], "decision": "deny", "reason": "别输"},
            headers=self.headers(),
        )
        _ts.TestBasics._wait_for(self, session_id, "finished")
        kinds = [e["kind"] for e in self.events(session_id)]
        self.assertIn("tool.denied", kinds)
        self.assertNotIn("tool.completed", kinds)
        #  扩展那边一帧 call 都不该收到：关连接时若有未读帧会在这里冒出来
        ws.__exit__(None, None, None)

    def test_timeout_is_a_tool_error_and_late_result_is_ignored(self):
        self.start('tool_call: {"name": "browser_tabs", "arguments": {}}\n---\ntext: 完\n', browser_timeout=1.0)
        session_id = self.new_session()
        ws = self.connect(session_id, supports=["browser_tabs"])
        self.assertEqual(ws.receive_json()["type"], "hello.ok")
        self.client.post(f"/session/{session_id}/prompt_async", json={"text": "列标签"}, headers=self.headers())
        call = ws.receive_json()
        self.assertEqual(call["timeout"], 1.0)
        _ts.TestBasics._wait_for(self, session_id, "finished")
        done = self.completed(session_id, "browser_tabs")
        self.assertFalse(done["ok"])
        self.assertIn("1s 内没有回应", done["output"])
        #  迟到的结果：工具已收场，丢掉即可，不能炸连接
        ws.send_json({"type": "result", "id": call["id"], "ok": True, "content": "太晚了"})
        self.assertTrue(self.status(session_id)["browser"]["connected"])
        ws.__exit__(None, None, None)

    def test_disconnect_mid_call_fails_the_call(self):
        self.start('tool_call: {"name": "browser_tabs", "arguments": {}}\n---\ntext: 完\n')
        session_id = self.new_session()
        ws = self.connect(session_id, supports=["browser_tabs"])
        self.assertEqual(ws.receive_json()["type"], "hello.ok")
        self.client.post(f"/session/{session_id}/prompt_async", json={"text": "列标签"}, headers=self.headers())
        ws.receive_json()  # call 帧
        ws.__exit__(None, None, None)
        _ts.TestBasics._wait_for(self, session_id, "finished")
        done = self.completed(session_id, "browser_tabs")
        self.assertFalse(done["ok"])
        self.assertIn("浏览器断开", done["output"])
        self.assertNotIn("browser_tabs", self.toolbox_names(session_id))

    def test_call_without_bridge_does_not_hang(self):
        #  没连桥时工具根本不在清单里：模型调它按未知工具处理（不进工具事件流），
        #  本轮照常结束，不会挂起等一个不存在的浏览器
        self.start('tool_call: {"name": "browser_tabs", "arguments": {}}\n---\ntext: 完\n')
        session_id = self.new_session()
        self.client.post(f"/session/{session_id}/prompt_async", json={"text": "列标签"}, headers=self.headers())
        state = _ts.TestBasics._wait_for(self, session_id, "finished")
        self.assertEqual(state["last_result"]["text"], "完")
        kinds = [e["kind"] for e in self.events(session_id)]
        self.assertNotIn("tool.completed", kinds)
        self.assertNotIn("permission.requested", kinds)

    def test_screenshot_image_is_attached_for_the_model(self):
        self.start('tool_call: {"name": "browser_screenshot", "arguments": {}}\n---\ntext: 看到了\n')
        session_id = self.new_session()
        ws = self.connect(session_id, supports=["browser_screenshot"])
        self.assertEqual(ws.receive_json()["type"], "hello.ok")
        self.client.post(f"/session/{session_id}/prompt_async", json={"text": "截图"}, headers=self.headers())
        call = ws.receive_json()
        ws.send_json(
            {
                "type": "result",
                "id": call["id"],
                "ok": True,
                "content": "已截取 1280×720",
                "image": {"media_type": "image/png", "data": PNG_1PX},
            }
        )
        _ts.TestBasics._wait_for(self, session_id, "finished")
        done = self.completed(session_id, "browser_screenshot")
        self.assertTrue(done["ok"])
        self.assertIn("已截取", done["output"])
        self.assertIn("截图见下一条", done["output"])
        #  图片以随后的 user 消息进历史（看得了图就是图片部件，看不了就是明确的说明）
        messages = self.client.app.state.sessions[session_id].agent.messages
        followups = [m for m in messages if m.get("role") == "user" and m is not messages[0]]
        self.assertTrue(followups, messages)
        content = followups[-1]["content"]
        if isinstance(content, list):
            self.assertTrue(any(p.get("type") == "image_url" for p in content), content)
        else:
            self.assertIn("图片", content)
        ws.__exit__(None, None, None)

    def test_bad_image_payload_degrades_to_text(self):
        self.start('tool_call: {"name": "browser_screenshot", "arguments": {}}\n---\ntext: 完\n')
        session_id = self.new_session()
        ws = self.connect(session_id, supports=["browser_screenshot"])
        self.assertEqual(ws.receive_json()["type"], "hello.ok")
        self.client.post(f"/session/{session_id}/prompt_async", json={"text": "截图"}, headers=self.headers())
        call = ws.receive_json()
        ws.send_json({"type": "result", "id": call["id"], "ok": True, "content": "截了", "image": {"data": "not-base64!"}})
        _ts.TestBasics._wait_for(self, session_id, "finished")
        done = self.completed(session_id, "browser_screenshot")
        self.assertTrue(done["ok"])
        self.assertIn("截图无法入库", done["output"])
        ws.__exit__(None, None, None)


if __name__ == "__main__":
    unittest.main()
