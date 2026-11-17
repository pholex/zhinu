"""computer_use 工具：帧协议、自动拉起、树的增量表示、参数校验、截图回传与挂载门控。

不依赖真实的辅助 app：用一个线程里的假服务在临时 unix socket 上应答。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import computer_use
from xiaoyu.agent import Agent
from xiaoyu.config import Config
from xiaoyu.computer_use import (ACTIONS, API_VERSION, Client, ServiceError, make_computer_use_tool,
                                 tree_delta)
from xiaoyu.errors import Interrupted
from xiaoyu.providers import Registry
from xiaoyu.render import NullSink
from xiaoyu.tools import Toolbox

#  1×1 的 PNG：够让 media.accept 认出是图片
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class FakeService:
    """按方法名应答的假辅助进程；记录收到的每个请求。"""

    def __init__(self, path: Path):
        self.path = path
        self.requests: list[dict] = []
        #  设了就不走 replies：按请求原样造一帧字节回去（用来模拟坏应答）
        self.raw_message = None
        self.replies: dict[str, object] = {
            "ping": {"apiVersion": API_VERSION, "pid": 1},
            "list_apps": {"apps": [{"id": "com.apple.TextEdit", "name": "TextEdit", "pid": 7, "active": True}]},
        }
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.bind(str(path))
        self._server.listen(4)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self):
        self._stop = True
        try:
            self._server.close()
        except OSError:
            pass

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._server.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        with conn:
            while True:
                header = self._read(conn, 4)
                if header is None:
                    return
                (length,) = struct.unpack("<I", header)
                body = self._read(conn, length)
                if body is None:
                    return
                request = json.loads(body)
                self.requests.append(request)
                if self.raw_message is not None:
                    data = self.raw_message(request)
                    self._send(conn, data)
                    continue
                reply = self.replies.get(request["method"], {"code": -32601, "message": "unknown"})
                if callable(reply):
                    reply = reply(request.get("params") or {})
                if isinstance(reply, dict) and "code" in reply and "message" in reply and len(reply) == 2:
                    message = {"jsonrpc": "2.0", "id": request["id"], "error": reply}
                else:
                    message = {"jsonrpc": "2.0", "id": request["id"], "result": reply}
                data = json.dumps(message).encode()
                self._send(conn, data)

    @staticmethod
    def _send(conn, data):
        try:
            conn.sendall(struct.pack("<I", len(data)) + data)
        except OSError:
            #  客户端等不及先关了（超时、被叫停）：真服务忽略 SIGPIPE，这里也只当这条连接没了
            return

    @staticmethod
    def _read(conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf


def _state(tree: str, shot: bool = True, **extra):
    result = {
        "app": {"id": "com.apple.TextEdit", "name": "TextEdit", "pid": 7, "active": True},
        "window": {"title": "未命名", "x": 0, "y": 0, "width": 800, "height": 600},
        "space": {"width": 800, "height": 600, "scale": 1.0},
        "tree": tree,
        "elements": 2,
        "truncated": False,
    }
    if shot:
        result["screenshot"] = {"png_base64": base64.b64encode(_PNG).decode(), "width": 800, "height": 600}
    result.update(extra)
    return result


@unittest.skipUnless(hasattr(socket, "AF_UNIX"), "假服务走 unix socket，本平台没有")
class ServiceCase(unittest.TestCase):
    def setUp(self):
        #  macOS 对 sun_path 限长 104 字节，不能用默认的长临时目录
        self.dir = tempfile.mkdtemp(dir="/tmp" if os.path.isdir("/tmp") else None)
        self.sock = Path(self.dir) / "s"
        self.service = FakeService(self.sock)
        self.addCleanup(self.service.close)
        self.launches = 0

        def launcher():
            self.launches += 1

        self.client = Client(self.sock, launcher=launcher, timeout=5)
        self.addCleanup(self.client.close)
        self.config = Config(base_url="", model="m", workspace=Path("."))
        self.toolbox = Toolbox(self.config, only=[])
        self.tool = make_computer_use_tool(self.config, self.toolbox, NullSink(), client=self.client)


class TestClient(ServiceCase):
    def test_ping_then_call_without_launch(self):
        apps = self.client.call("list_apps")
        self.assertEqual(apps["apps"][0]["id"], "com.apple.TextEdit")
        self.assertEqual([r["method"] for r in self.service.requests], ["ping", "list_apps"])
        self.assertEqual(self.launches, 0)

    def test_launches_when_socket_missing_then_gives_up_with_hint(self):
        self.service.close()
        os.unlink(self.sock)
        launched = threading.Event()

        def launcher():
            launched.set()

        client = Client(self.sock, launcher=launcher, timeout=1)
        self.addCleanup(client.close)
        with mock.patch.object(computer_use.time, "monotonic", side_effect=[0, 0, 100, 100, 100]):
            with self.assertRaises(ServiceError) as ctx:
                client.call("list_apps")
        self.assertTrue(launched.is_set())
        self.assertIn("没有在", str(ctx.exception))

    def test_launch_command_hands_socket_path_to_the_helper(self):
        #  open 不传环境变量：socket 路径必须经 --env 交给辅助进程，否则两端各听各的
        client = Client(self.sock, launcher=lambda: None, timeout=1)
        self.addCleanup(client.close)
        command = client.launch_command(Path("/Applications/X.app"))
        self.assertEqual(command[:3], ["open", "-g", "--env"])
        self.assertEqual(command[3], f"XIAOYU_COMPUTER_USE_SOCKET={self.sock}")
        self.assertEqual(command[-1], "/Applications/X.app")

    def test_version_mismatch_is_refused(self):
        self.service.replies["ping"] = {"apiVersion": "xiaoyu-computer-use/0"}
        with self.assertRaises(ServiceError) as ctx:
            self.client.call("list_apps")
        self.assertIn("协议版本", str(ctx.exception))

    def test_error_reply_becomes_service_error(self):
        self.service.replies["get_state"] = {"code": -32002, "message": "找不到应用"}
        with self.assertRaises(ServiceError) as ctx:
            self.client.call("get_state", app="Nope")
        self.assertEqual(ctx.exception.code, -32002)

    def test_reply_split_across_recv_calls_is_reassembled(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        payload = b"0123456789"

        def writer():
            for i in range(0, len(payload), 3):
                b.sendall(payload[i:i + 3])
                time.sleep(0.03)

        threading.Thread(target=writer, daemon=True).start()
        client = Client(self.sock, launcher=lambda: None, timeout=2, stop_requested=lambda: False)
        self.assertEqual(client._read_exactly(a, len(payload), time.monotonic() + 2), payload)

    def test_timeout_during_action_says_it_may_have_happened_then_reconnects(self):
        self.service.replies["perform"] = lambda p: (time.sleep(0.8), {"ok": True})[1]
        client = Client(self.sock, launcher=lambda: None, timeout=0.3)
        self.addCleanup(client.close)
        tool = make_computer_use_tool(self.config, self.toolbox, NullSink(), client=client)
        out = tool.handler(action="click", app="TextEdit", element=1)
        self.assertTrue(out.startswith("ERROR: 与辅助进程的连接中断"), out)
        self.assertIn("动作可能已经执行过", out)
        self.assertIsNone(client._sock)
        #  只读调用不加这句
        self.service.replies["list_apps"] = lambda p: (time.sleep(0.8), {"apps": []})[1]
        out = tool.handler(action="list_apps")
        self.assertNotIn("动作可能已经执行过", out)
        #  连接断了下次调用自动重连
        self.service.replies["list_apps"] = {"apps": []}
        self.assertIn("没有运行中", tool.handler(action="list_apps"))

    def test_stop_request_interrupts_the_wait(self):
        self.service.replies["perform"] = lambda p: (time.sleep(1.0), {"ok": True})[1]
        client = Client(self.sock, launcher=lambda: None, timeout=5, stop_requested=lambda: True)
        self.addCleanup(client.close)
        started = time.monotonic()
        with self.assertRaises(Interrupted):
            client.call("perform", app="TextEdit", action={"kind": "click", "element": 1})
        self.assertLess(time.monotonic() - started, 2)
        self.assertIsNone(client._sock)

    def test_bad_replies_close_the_connection(self):
        cases = {
            "id 对不上": lambda req: json.dumps({"jsonrpc": "2.0", "id": 999, "result": {}}).encode(),
            "不是 JSON": lambda req: b"not json",
            "不是对象": lambda req: b"[1, 2]",
        }
        for label, raw in cases.items():
            with self.subTest(label):
                self.service.raw_message = raw
                with self.assertRaises(ServiceError) as ctx:
                    self.client.call("list_apps")
                self.assertTrue(ctx.exception.transport)
                self.assertIsNone(self.client._sock)
        #  error 不是对象也能变成可读的错误，且不算传输错误
        self.service.raw_message = lambda req: json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": "boom"}).encode()
        with self.assertRaises(ServiceError) as ctx:
            self.client.call("list_apps")
        self.assertEqual(str(ctx.exception), "boom")
        self.assertFalse(ctx.exception.transport)
        self.service.raw_message = None
        self.assertIn("apps", self.client.call("list_apps"))

    def test_frames_are_length_prefixed_little_endian(self):
        self.client.call("list_apps")
        raw = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw.connect(str(self.sock))
        body = json.dumps({"jsonrpc": "2.0", "id": 9, "method": "ping", "params": {}}).encode()
        raw.sendall(struct.pack("<I", len(body)) + body)
        header = raw.recv(4)
        (length,) = struct.unpack("<I", header)
        self.assertGreater(length, 0)
        raw.close()


class TestTreeDelta(unittest.TestCase):
    def test_first_observation_is_whole(self):
        text, whole = tree_delta(None, "a\nb")
        self.assertTrue(whole)
        self.assertEqual(text, "a\nb")

    def test_small_change_lists_removed_and_added(self):
        old = "\n".join(f"[{i}] button \"b{i}\"" for i in range(30))
        new = old.replace('[3] button "b3"', '[3] button "b3" (focused)') + '\n[31] textField "new"'
        text, whole = tree_delta(old, new)
        self.assertFalse(whole)
        self.assertIn('- [3] button "b3"', text)
        self.assertIn('+ [3] button "b3" (focused)', text)
        self.assertIn('+ [31] textField "new"', text)
        self.assertIn("索引保持不变", text)

    def test_big_change_falls_back_to_whole_tree(self):
        old = "\n".join(f"[{i}] x" for i in range(30))
        new = "\n".join(f"[{i}] y" for i in range(30))
        text, whole = tree_delta(old, new)
        self.assertTrue(whole)
        self.assertEqual(text, new)

    def test_reorder_or_indent_only_counts_as_no_change(self):
        lines = [f"[{i}] button \"b{i}\" @1,{i}" for i in range(1, 30)]
        before = "\n".join(lines)
        text, whole = tree_delta(before, "\n".join(lines[1:] + lines[:1]))
        self.assertFalse(whole)
        self.assertIn("只有元素顺序或层级变动", text)
        text, whole = tree_delta(before, "\n".join("  " + line for line in lines))
        self.assertIn("只有元素顺序或层级变动", text)
        #  真有增删的照常列出
        text, _ = tree_delta(before, "\n".join(lines[1:] + ["[99] button \"new\" @1,1"]))
        self.assertIn("- [1] button", text)
        self.assertIn("+ [99] button", text)

    def test_no_change_says_so(self):
        text, whole = tree_delta("a\nb", "a\nb")
        self.assertFalse(whole)
        self.assertIn("没有变化", text)


class TestTool(ServiceCase):
    def test_schema_and_gating(self):
        self.assertEqual(self.tool.name, "computer_use")
        self.assertTrue(self.tool.requires_approval)
        self.assertTrue(self.tool.untrusted)
        self.assertEqual(self.tool.parameters["properties"]["action"]["enum"], list(ACTIONS))
        with mock.patch.object(computer_use, "available", return_value=True):
            self.assertTrue(self.tool.available())
            self.config.enable_computer_use = False
            self.assertFalse(self.tool.available())
        self.config.enable_computer_use = True
        with mock.patch.object(computer_use, "available", return_value=False):
            self.assertFalse(self.tool.available())

    def test_list_apps(self):
        out = self.tool.handler(action="list_apps")
        self.assertIn("TextEdit（com.apple.TextEdit）", out)
        self.assertIn("前台", out)

    def test_state_pushes_screenshot_and_full_tree_first_time(self):
        self.service.replies["get_state"] = lambda p: _state('[1] button "保存" @10,20\n[2] textField "正文" (editable)')
        with mock.patch.object(computer_use.time, "sleep"):
            out = self.tool.handler(action="state", app="TextEdit")
        self.assertIn("[TextEdit（com.apple.TextEdit）窗口「未命名」 800×600 坐标单位]", out)
        self.assertIn('[1] button "保存" @10,20', out)
        self.assertIn("窗口截图见下一条消息", out)
        taken = self.toolbox.take_media()
        self.assertEqual(len(taken), 1)
        self.assertEqual(taken[0]["type"], "image_url")
        request = [r for r in self.service.requests if r["method"] == "get_state"][0]
        self.assertEqual(request["params"], {"app": "TextEdit", "screenshot": True})

    def test_second_state_is_delta_and_screenshot_can_be_skipped(self):
        trees = iter(["\n".join(f"[{i}] b" for i in range(30)),
                      "\n".join(f"[{i}] b" for i in range(30)) + "\n[30] c"])
        self.service.replies["get_state"] = lambda p: _state(next(trees), shot=False)
        with mock.patch.object(computer_use.time, "sleep"):
            self.tool.handler(action="state", app="TextEdit", screenshot=False)
            out = self.tool.handler(action="state", app="TextEdit", screenshot=False)
        self.assertIn("+ [30] c", out)
        self.assertNotIn("[0] b", out)
        self.assertEqual(self.toolbox.take_media(), [])
        requests = [r["params"] for r in self.service.requests if r["method"] == "get_state"]
        self.assertEqual(requests[-1]["screenshot"], False)

    def test_corrupt_screenshot_bytes_are_reported(self):
        self.service.replies["get_state"] = lambda p: _state("[1] x", shot=False,
                                                              screenshot={"png_base64": "@@@", "width": 1, "height": 1})
        out = self.tool.handler(action="state", app="TextEdit")
        self.assertIn("截图未能回传", out)
        self.assertIn("损坏", out)

    def test_full_tree_is_honoured_after_actions(self):
        tree = "\n".join(f"[{i}] button \"b{i}\" @1,{i}" for i in range(1, 30))
        self.service.replies["perform"] = {"ok": True}
        self.service.replies["get_state"] = lambda p: _state(tree, shot=False)
        with mock.patch.object(computer_use.time, "sleep"):
            self.tool.handler(action="state", app="TextEdit", screenshot=False)
            delta = self.tool.handler(action="click", app="TextEdit", element=1, screenshot=False)
            whole = self.tool.handler(action="click", app="TextEdit", element=1, screenshot=False, full_tree=True)
        self.assertIn("没有变化", delta)
        self.assertIn("[29] button", whole)

    def test_delta_cache_is_per_app(self):
        def state(p):
            app = p["app"]
            return _state("\n".join(f"[{i}] button \"{app}{i}\" @1,{i}" for i in range(1, 25)), shot=False,
                          app={"id": app, "name": app, "pid": 1, "active": True})

        self.service.replies["get_state"] = state
        with mock.patch.object(computer_use.time, "sleep"):
            self.tool.handler(action="state", app="A", screenshot=False)
            self.tool.handler(action="state", app="B", screenshot=False)
            again = self.tool.handler(action="state", app="A", screenshot=False)
        self.assertIn("没有变化", again)

    def test_screenshot_error_is_reported_not_fatal(self):
        self.service.replies["get_state"] = lambda p: _state("[1] x", shot=False, screenshot_error="未授权屏幕录制")
        with mock.patch.object(computer_use.time, "sleep"):
            out = self.tool.handler(action="state", app="TextEdit")
        self.assertIn("截图不可用：未授权屏幕录制", out)
        self.assertIn("[1] x", out)

    def test_click_sends_perform_then_observes(self):
        self.service.replies["perform"] = {"ok": True, "note": "已通过无障碍动作按下"}
        self.service.replies["get_state"] = lambda p: _state("[1] x", shot=False)
        with mock.patch.object(computer_use.time, "sleep") as sleep:
            out = self.tool.handler(action="click", app="TextEdit", element=1)
        perform = [r for r in self.service.requests if r["method"] == "perform"][0]
        self.assertEqual(perform["params"], {"app": "TextEdit", "action": {"kind": "click", "element": 1}})
        self.assertTrue(out.startswith("已执行 click：已通过无障碍动作按下。"))
        self.assertIn("[1] x", out)
        #  动作之后的观察要等界面稳定
        self.assertTrue(sleep.called)

    def test_argument_shapes(self):
        self.service.replies["perform"] = {"ok": True}
        self.service.replies["get_state"] = lambda p: _state("t", shot=False)
        with mock.patch.object(computer_use.time, "sleep"):
            self.tool.handler(action="click", app="A", x=10, y=20, button="right", count=5, modifiers="cmd")
            self.tool.handler(action="type", app="A", element=3, text="hi")
            self.tool.handler(action="key", app="A", key=" cmd+s ")
            self.tool.handler(action="scroll", app="A", direction="up", pages=99)
            self.tool.handler(action="drag", app="A", x=1, y=2, to_x=3, to_y=4)
            self.tool.handler(action="set_value", app="A", element=2, value="v")
            self.tool.handler(action="action", app="A", element=2, name="showMenu")
        sent = [r["params"]["action"] for r in self.service.requests if r["method"] == "perform"]
        self.assertEqual(sent[0], {"kind": "click", "x": 10.0, "y": 20.0, "button": "right", "count": 3, "modifiers": "cmd"})
        self.assertEqual(sent[1], {"kind": "type", "element": 3, "text": "hi"})
        self.assertEqual(sent[2], {"kind": "key", "key": "cmd+s"})
        self.assertEqual(sent[3], {"kind": "scroll", "direction": "up", "pages": 20.0})
        self.assertEqual(sent[4], {"kind": "drag", "from_x": 1.0, "from_y": 2.0, "to_x": 3.0, "to_y": 4.0})
        self.assertEqual(sent[5], {"kind": "set_value", "element": 2, "value": "v"})
        self.assertEqual(sent[6], {"kind": "action", "element": 2, "name": "showMenu"})

    def test_paste_and_select_text_shapes(self):
        self.service.replies["perform"] = {"ok": True, "note": "ok"}
        self.service.replies["get_state"] = lambda p: _state("[1] x", shot=False)
        with mock.patch.object(computer_use.time, "sleep"):
            self.tool.handler(action="paste", app="TextEdit", text="# 标题", format="markdown", element=1, screenshot=False)
            self.tool.handler(action="select_text", app="TextEdit", text="abc", occurrence=2, mode="end", screenshot=False)
            self.tool.handler(action="select_text", app="TextEdit", element=1, screenshot=False)
        sent = [r["params"]["action"] for r in self.service.requests if r["method"] == "perform"]
        self.assertEqual(sent, [
            {"kind": "paste", "element": 1, "text": "# 标题", "format": "markdown"},
            {"kind": "select_text", "text": "abc", "occurrence": 2, "mode": "end"},
            {"kind": "select_text", "element": 1},
        ])
        for bad in (dict(action="paste", app="TextEdit"), dict(action="paste", app="TextEdit", text="x", format="rtf"),
                    dict(action="select_text", app="TextEdit", text="x", mode="middle")):
            with self.subTest(bad):
                self.assertTrue(self.tool.handler(**bad).startswith("ERROR:"))
        self.assertEqual(len([r for r in self.service.requests if r["method"] == "perform"]), 3)

    def test_validation_errors_do_not_reach_service(self):
        cases = [
            dict(action="state"),
            dict(action="click", app="A"),
            dict(action="click", app="A", x=1, y=2, button="back"),
            dict(action="type", app="A"),
            dict(action="key", app="A"),
            dict(action="scroll", app="A", direction="sideways"),
            dict(action="drag", app="A", x=1, y=2),
            dict(action="set_value", app="A", element=1),
            dict(action="action", app="A", element=1),
            dict(action="fly", app="A"),
        ]
        for kwargs in cases:
            self.assertTrue(self.tool.handler(**kwargs).startswith("ERROR:"), kwargs)
        self.assertEqual([r["method"] for r in self.service.requests if r["method"] != "ping"], [])

    def test_service_error_is_relayed(self):
        self.service.replies["get_state"] = {"code": -32001, "message": "未授权：辅助功能"}
        with mock.patch.object(computer_use.time, "sleep"):
            out = self.tool.handler(action="state", app="TextEdit")
        self.assertEqual(out, "ERROR: 未授权：辅助功能")

    def test_disabled_flag_short_circuits(self):
        self.config.enable_computer_use = False
        out = self.tool.handler(action="list_apps")
        self.assertIn("已禁用", out)
        self.assertEqual(self.service.requests, [])


class TestDiscovery(unittest.TestCase):
    def test_app_path_prefers_env_then_user_applications(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "Xiaoyu Computer Use.app"
            fake.mkdir()
            with mock.patch.dict(os.environ, {"XIAOYU_COMPUTER_USE_APP": str(fake)}):
                self.assertEqual(computer_use.app_path(), fake)
            with mock.patch.dict(os.environ, {"XIAOYU_COMPUTER_USE_APP": str(Path(tmp) / "missing.app")}):
                self.assertIsNone(computer_use.app_path())
            with mock.patch.dict(os.environ, {"XIAOYU_COMPUTER_USE_APP": ""}), \
                    mock.patch.object(computer_use.Path, "home", return_value=Path(tmp)), \
                    mock.patch.object(computer_use, "_app_by_bundle_id", return_value=None) as by_id:
                self.assertIsNone(computer_use.app_path())
                by_id.assert_called_once()
                (Path(tmp) / "Applications").mkdir()
                (Path(tmp) / "Applications" / "Xiaoyu Computer Use.app").mkdir()
                self.assertEqual(computer_use.app_path(), Path(tmp) / "Applications" / "Xiaoyu Computer Use.app")
                #  桌面宿主代装的那份优先于 ~/Applications
                support = computer_use.support_dir() / "Xiaoyu Computer Use.app"
                support.mkdir(parents=True)
                self.assertEqual(computer_use.app_path(), support)

    def test_bundle_id_lookup_falls_back_from_spotlight_to_launch_services_and_caches(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "elsewhere" / "Xiaoyu Computer Use.app"
            real.mkdir(parents=True)
            calls: list[str] = []

            def fake_run(command, **kwargs):
                calls.append(command[0])
                out = "" if command[0] == "mdfind" else f"{real}/\n"
                return subprocess.CompletedProcess(command, 0, stdout=out, stderr="")

            computer_use._bundle_lookup.update(at=-computer_use.BUNDLE_LOOKUP_TTL, path=None)
            with mock.patch.object(computer_use.subprocess, "run", side_effect=fake_run):
                self.assertEqual(computer_use._app_by_bundle_id(), real)
                self.assertEqual(calls, ["mdfind", "osascript"])
                #  一分钟内不再问系统
                self.assertEqual(computer_use._app_by_bundle_id(), real)
                self.assertEqual(calls, ["mdfind", "osascript"])
            computer_use._bundle_lookup.update(at=-computer_use.BUNDLE_LOOKUP_TTL, path=None)
            #  命令不存在（非 macOS）也只是找不到，不抛
            with mock.patch.object(computer_use.subprocess, "run", side_effect=FileNotFoundError):
                self.assertIsNone(computer_use._app_by_bundle_id())
            computer_use._bundle_lookup.update(at=-computer_use.BUNDLE_LOOKUP_TTL, path=None)

    def test_bundle_id_matches_the_build_script(self):
        build = Path(__file__).resolve().parents[1] / "labs" / "computer-use" / "macos" / "build.py"
        if not build.exists():
            self.skipTest("源码树外没有构建脚本")
        self.assertIn(f'BUNDLE_ID = "{computer_use.BUNDLE_ID}"', build.read_text(encoding="utf-8"))

    def test_available_requires_darwin(self):
        with mock.patch.object(computer_use.sys, "platform", "linux"):
            self.assertFalse(computer_use.available())

    def test_socket_path_env_override(self):
        with mock.patch.dict(os.environ, {"XIAOYU_COMPUTER_USE_SOCKET": "/tmp/x.sock"}):
            self.assertEqual(computer_use.socket_path(), Path("/tmp/x.sock"))


class TestLazySocketPath(unittest.TestCase):
    def test_constructing_a_client_does_not_ask_for_the_home_directory(self):
        #  每个 Agent 初始化都会构造一个 Client；家目录定不出来时构造不能跟着失败
        with mock.patch.object(computer_use, "socket_path", side_effect=RuntimeError("no home")):
            client = Client()
        with mock.patch.object(computer_use, "socket_path", return_value=Path("x.sock")):
            self.assertEqual(client.path, Path("x.sock"))


class TestMounting(unittest.TestCase):
    def _agent(self, **overrides):
        config = Config(base_url="", model="m", workspace=Path("."), enable_web_search=False,
                        enable_web_fetch=False, enable_x_search=False, enable_deep_research=False,
                        enable_browser=False, enable_mcp=False, enable_plugins=False, enable_skills=False,
                        enable_hooks=False, enable_peers=False, **overrides)
        #  给一个假端点：这里只看工具挂没挂上，不能依赖跑测试的机器上配了模型密钥
        return Agent(config, sink=NullSink(), registry=Registry.for_client(mock.Mock()))

    def test_registered_when_enabled_and_hidden_when_helper_missing(self):
        agent = self._agent()
        self.assertIsNotNone(agent.toolbox.get("computer_use"))
        with mock.patch.object(computer_use, "available", return_value=False):
            names = [s["function"]["name"] for s in agent.toolbox.schemas()]
            self.assertNotIn("computer_use", names)
        with mock.patch.object(computer_use, "available", return_value=True):
            names = [s["function"]["name"] for s in agent.toolbox.schemas()]
            self.assertIn("computer_use", names)

    def test_not_registered_when_disabled(self):
        agent = self._agent(enable_computer_use=False)
        self.assertIsNone(agent.toolbox.get("computer_use"))

    def test_env_flag(self):
        with mock.patch.dict(os.environ, {"XIAOYU_ENABLE_COMPUTER_USE": "0"}):
            self.assertFalse(Config.from_env().enable_computer_use)


if __name__ == "__main__":
    unittest.main()
