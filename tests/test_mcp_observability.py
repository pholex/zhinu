"""MCP 配置与可观测：tools 名单 / cwd / 报错带日志尾部 / progress 与 logging 通知 /
`xiaoyu mcp probe`。假 server 走真子进程，不打网络。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tests.test_mcp import FAKE_SERVER
from xiaoyu import cli, mcp, render
from xiaoyu.config import Config
from xiaoyu.events import ToolProgress
from xiaoyu.tools import Toolbox

#  会"说话"的假 server：回报工作目录、按 progressToken 上报进度、发日志通知、
#  按要求死在启动期（stderr 留下线索，含一个该被脱敏的令牌）或调用中
NOTIFY_SERVER = textwrap.dedent(
    """
    import json, os, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    TOOLS = [
        {"name": "cwd", "description": "回报工作目录",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "slow", "description": "分三步上报进度",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "chatty", "description": "发三条日志通知",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "die", "description": "打一行日志后退出",
         "inputSchema": {"type": "object", "properties": {}}},
    ]

    if os.environ.get("DIE_AT_START"):
        sys.stderr.write("\\x1b[31mboom\\x1b[0m: missing env FOO\\n")
        sys.stderr.write("auth: Bearer secret-token-value\\n")
        sys.stderr.flush()
        sys.exit(3)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": msg["params"]["protocolVersion"],
                "capabilities": {"tools": {}, "logging": {}},
                "instructions": "先调 cwd 再调 slow",
                "serverInfo": {"name": "notify-server", "version": "0.1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = msg["params"]["name"]
            token = (msg["params"].get("_meta") or {}).get("progressToken")
            if name == "cwd":
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": os.getcwd()}]}})
            elif name == "slow":
                for step in (1, 2, 3):
                    if token is not None:
                        send({"jsonrpc": "2.0", "method": "notifications/progress",
                              "params": {"progressToken": token, "progress": step,
                                         "total": 3, "message": "step %d" % step}})
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text",
                                 "text": "done " + ("with-token" if token is not None else "no-token")}]}})
            elif name == "chatty":
                send({"jsonrpc": "2.0", "method": "notifications/message",
                      "params": {"level": "info", "logger": "x", "data": "quiet info"}})
                send({"jsonrpc": "2.0", "method": "notifications/message",
                      "params": {"level": "warning", "data": {"msg": "loud \\x1b[31mwarning"}}})
                send({"jsonrpc": "2.0", "method": "notifications/message",
                      "params": {"level": "error", "data": "bad thing"}})
                send({"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": "ok"}]}})
            elif name == "die":
                sys.stderr.write("fatal: died on purpose\\n")
                sys.stderr.flush()
                sys.exit(7)
    """
)


class _ServerCase(unittest.TestCase):
    """真子进程的公共底座：临时目录、用户配置目录打桩、manager 收尾。"""

    SCRIPT = NOTIFY_SERVER

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.script = self.root / "server.py"
        self.script.write_text(self.SCRIPT, encoding="utf-8")
        self.user_dir = self.root / "userconf"
        patcher = mock.patch.object(mcp, "user_config_dir", lambda: self.user_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def spec(self, name: str = "s", **overrides) -> mcp.ServerSpec:
        base = dict(name=name, command=sys.executable, args=[str(self.script)], timeout=15.0)
        base.update(overrides)
        return mcp.ServerSpec(**base)

    def manager(self, spec: mcp.ServerSpec, wait: bool = True) -> mcp.McpManager:
        #  不用 schema 缓存：同一测试里反复起 manager，缓存命中会跳过真实握手
        manager = mcp.McpManager([spec], use_cache=False)
        manager.start()
        self.addCleanup(manager.close)
        if wait:
            manager.wait_ready(20.0)
        return manager

    def tool(self, manager: mcp.McpManager, suffix: str) -> mcp.RemoteTool:
        for remote in manager.ready_tools():
            if remote.name.endswith(suffix):
                return remote
        raise AssertionError(f"没有以 {suffix} 结尾的工具：{[t.name for t in manager.ready_tools()]}")


class ToolsAllowlistParsingTest(unittest.TestCase):
    def parse(self, entry: dict, **kwargs):
        return mcp.parse_server_mapping({"s": entry}, **kwargs)

    def test_tools_and_cwd_parse_with_env_expansion(self):
        specs, problems = self.parse(
            {"command": "npx", "args": ["pkg"], "tools": ["a", "b", " a "], "cwd": "${env:PROBE_ROOT}/data"},
            extra_env={"PROBE_ROOT": "/srv"},
        )
        self.assertEqual(problems, [])
        self.assertEqual(specs[0].tools, ["a", "b"])  # 去重去空白
        self.assertEqual(specs[0].cwd, "/srv/data")
        self.assertTrue(specs[0].allows_tool("a"))
        self.assertFalse(specs[0].allows_tool("c"))

    def test_absent_tools_means_everything(self):
        specs, _ = self.parse({"command": "npx"})
        self.assertIsNone(specs[0].tools)
        self.assertTrue(specs[0].allows_tool("anything"))
        self.assertEqual(specs[0].cwd, "")

    def test_malformed_tools_is_reported_not_silently_opened(self):
        specs, problems = self.parse({"command": "npx", "tools": "a,b"})
        self.assertIsNone(specs[0].tools)
        self.assertTrue(any("tools" in p for p in problems))
        specs, problems = self.parse({"command": "npx", "tools": []})
        self.assertEqual(specs[0].tools, [])
        self.assertTrue(any("空列表" in p for p in problems))

    def test_remote_accepts_tools_but_not_cwd(self):
        specs, problems = self.parse({"url": "https://mcp.example.com/mcp", "tools": ["a"], "cwd": "/x"})
        self.assertEqual(specs[0].tools, ["a"])
        self.assertEqual(specs[0].cwd, "")
        self.assertTrue(any("cwd" in p for p in problems))

    def test_cwd_is_part_of_the_cache_fingerprint(self):
        one = mcp.ServerSpec(name="s", command="npx", cwd="/a")
        two = mcp.ServerSpec(name="s", command="npx", cwd="/b")
        self.assertNotEqual(mcp.spec_fingerprint(one), mcp.spec_fingerprint(two))


class ToolsAllowlistRuntimeTest(_ServerCase):
    SCRIPT = FAKE_SERVER

    def test_only_listed_tools_register_and_dispatch_refuses_the_rest(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            manager = self.manager(self.spec("fake", tools=["echo", "typo"]))
        self.assertEqual([t.name for t in manager.ready_tools()], ["mcp__fake__echo"])
        #  代际事务只对名单内比对：存档里只有 echo
        self.assertEqual([d["name"] for d in manager._declared["fake"]], ["echo"])
        #  名单里写错的名字告警一次，且把 server 实际提供的列出来
        self.assertEqual(err.getvalue().count("没有提供"), 1)
        self.assertIn("typo", err.getvalue())
        self.assertIn("boom", err.getvalue())
        server = manager._servers["fake"]
        #  第二道闸：拿着原名直接调也到不了 server
        self.assertIn("不在声明的 tools 名单里", server.call_tool("boom", {}))
        self.assertEqual(server.call_tool("echo", {"text": "hi"}), "echo: hi")
        self.assertIn("1 个工具", manager.describe())

    def test_listed_tool_changes_are_still_quarantined_but_unlisted_are_not(self):
        """名单外工具的描述怎么变都不隔离；名单内的照常走基线。"""
        manager = self.manager(self.spec("fake", tools=["echo"]))
        manager.close()
        #  boom 的描述变了：不在名单里，不该触发隔离
        self.script.write_text(FAKE_SERVER.replace("总是失败", "总是失败（新版）"), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            manager = self.manager(self.spec("fake", tools=["echo"]))
        self.assertEqual([t.name for t in manager.ready_tools()], ["mcp__fake__echo"])
        self.assertNotIn("隔离", err.getvalue())
        manager.close()
        self.script.write_text(FAKE_SERVER.replace("回显文本", "回显文本（改了）"), encoding="utf-8")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            manager = self.manager(self.spec("fake", tools=["echo"]))
        self.assertEqual(manager._quarantined.get("fake"), ["echo"])


class CwdTest(_ServerCase):
    def test_cwd_is_where_the_server_runs(self):
        sub = self.root / "data dir"
        sub.mkdir()
        manager = self.manager(self.spec(cwd=str(sub)))
        reported = self.tool(manager, "__cwd").handler()
        self.assertEqual(os.path.realpath(reported), os.path.realpath(sub))

    def test_missing_cwd_fails_before_spawn_with_a_clear_reason(self):
        manager = self.manager(self.spec(cwd=str(self.root / "nope")))
        state = manager._states["s"]
        self.assertTrue(state.startswith("failed: 工作目录不存在"), state)
        self.assertIn("nope", state)
        self.assertIn("cwd", state)


class LogTailTest(_ServerCase):
    def test_log_tail_keeps_last_lines_redacted_and_clean(self):
        path = self.root / "x.log"
        lines = [f"line {n}" for n in range(12)]
        lines.append("\x1b[31mauth: Bearer secret-token-value\x1b[0m " + "x" * 300)
        path.write_text("\n".join(lines) + "\n\n", encoding="utf-8")
        tail = mcp.log_tail(path)
        self.assertEqual(len(tail), 8)
        self.assertEqual(tail[0], "line 5")
        self.assertIn("[REDACTED]", tail[-1])
        self.assertNotIn("secret-token-value", tail[-1])
        self.assertNotIn("\x1b", tail[-1])
        self.assertTrue(tail[-1].endswith("…"))
        self.assertLessEqual(len(tail[-1]), mcp._LOG_TAIL_WIDTH + 1)
        self.assertEqual(mcp.log_tail(self.root / "missing.log"), [])
        self.assertEqual(mcp.tail_text(self.root / "missing.log"), "")

    def test_startup_failure_state_carries_the_log_tail(self):
        manager = self.manager(self.spec(env={"DIE_AT_START": "1"}))
        state = manager._states["s"]
        self.assertTrue(state.startswith("failed:"), state)
        self.assertIn("日志末", state)
        self.assertIn("boom: missing env FOO", state)
        self.assertIn("[REDACTED]", state)
        self.assertNotIn("secret-token-value", state)
        self.assertNotIn("\x1b", state)
        #  /mcp 的状态输出里同样看得到
        self.assertIn("missing env FOO", manager.describe())

    def test_calls_after_a_crash_quote_the_log(self):
        with mock.patch.dict(os.environ, {"XIAOYU_MCP_RECONNECT": "0"}):
            manager = self.manager(self.spec())
            server = manager._servers["s"]
            proc = server._proc
            first = self.tool(manager, "__die").handler()
            proc.wait(10)
            self.assertIn("ERROR", first)
            self.assertIn("died on purpose", first)
            #  进程已退出后的调用：同样带尾部，不只给路径
            second = self.tool(manager, "__cwd").handler()
            self.assertIn("进程已退出", second)
            self.assertIn("died on purpose", second)
            self.assertIn(str(server.log_path), second)


class NotificationTest(_ServerCase):
    def test_progress_only_flows_when_someone_listens(self):
        manager = self.manager(self.spec())
        slow = self.tool(manager, "__slow")
        seen: list[dict] = []
        with mcp.stop_scope(None, progress=seen.append):
            self.assertEqual(slow.handler(), "done with-token")
        self.assertEqual([(i["progress"], i["total"], i["message"]) for i in seen],
                         [(1.0, 3.0, "step 1"), (2.0, 3.0, "step 2"), (3.0, 3.0, "step 3")])
        #  没人接就不带 token：server 不发也不白算
        self.assertEqual(slow.handler(), "done no-token")
        #  调用结束后 token 注销，表里不残留
        self.assertEqual(manager._servers["s"]._progress_hooks, {})

    def test_logging_notifications_split_by_level(self):
        manager = self.manager(self.spec())
        surfaced: list[tuple[str, str, str]] = []
        manager.on_log = lambda name, level, text: surfaced.append((name, level, text))
        self.assertEqual(self.tool(manager, "__chatty").handler(), "ok")
        self.assertEqual([(n, lv) for n, lv, _ in surfaced], [("s", "warning"), ("s", "error")])
        self.assertIn("loud", surfaced[0][2])
        self.assertNotIn("\x1b", surfaced[0][2])
        log = manager._servers["s"].log_path.read_text(encoding="utf-8")
        #  全部级别都进日志文件，info 只在那里
        self.assertIn("[info x] quiet info", log)
        self.assertIn("[warning]", log)
        self.assertIn("bad thing", log)

    def test_unwired_manager_prints_to_stderr(self):
        manager = self.manager(self.spec())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.tool(manager, "__chatty").handler()
        self.assertIn("[MCP s warning]", err.getvalue())
        self.assertNotIn("quiet info", err.getvalue())


class ToolboxWiringTest(unittest.TestCase):
    """Toolbox 把进度/日志钩子接到 mcp 层的方式（不起子进程）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name).resolve()
        self.config = Config(base_url="x", model="x", workspace=root, enable_plugins=False, enable_mcp=False)

    def test_interruptible_hands_progress_to_the_scope(self):
        toolbox = Toolbox(self.config)
        seen: list[tuple[str, dict]] = []
        toolbox.mcp_progress_hook = lambda name, info: seen.append((name, info))

        def handler(**kwargs):
            hook = getattr(mcp._call_scope, "progress", None)
            hook({"progress": 1.0, "total": None, "message": "m"})
            return "ok"

        self.assertEqual(toolbox._interruptible(handler, "mcp__s__t")(), "ok")
        self.assertEqual(seen, [("mcp__s__t", {"progress": 1.0, "total": None, "message": "m"})])
        #  离开作用域后线程上不残留
        self.assertIsNone(getattr(mcp._call_scope, "progress", None))
        #  没接钩子：作用域里也没有回调（server 不会被要求发进度）
        toolbox.mcp_progress_hook = None
        captured = []
        toolbox._interruptible(lambda **kw: captured.append(getattr(mcp._call_scope, "progress", "unset")) or "x")()
        self.assertEqual(captured, [None])

    def test_attach_mcp_log_only_for_an_owned_manager(self):
        manager = mcp.McpManager([])
        self.addCleanup(manager.close)
        toolbox = Toolbox(self.config, mcp_view=manager)
        hook = lambda *a: None  # noqa: E731
        toolbox.attach_mcp_log(hook)
        self.assertIs(manager.on_log, hook)
        #  已有人接线的不覆盖
        toolbox.attach_mcp_log(lambda *a: None)
        self.assertIs(manager.on_log, hook)
        #  子 agent 拿的是视图：不接（父级已经接了，再接就是同一条告警两遍）
        manager.on_log = None
        Toolbox(self.config, mcp_view=mcp.McpView(manager)).attach_mcp_log(hook)
        self.assertIsNone(manager.on_log)


class ProgressTextTest(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(render.progress_text(3, 10, "下载中"), "3/10（30%） · 下载中")
        self.assertEqual(render.progress_text(2.5, None, ""), "2.5")
        self.assertEqual(render.progress_text(None, None, "只有话"), "只有话")
        self.assertEqual(render.progress_text(None, None, ""), "")
        self.assertEqual(render.progress_text(12, 10, ""), "12/10（100%）")

    def test_event_round_trips_as_json(self):
        event = ToolProgress(name="mcp__s__slow", message="step 1", progress=1, total=3)
        self.assertEqual(
            event.to_dict(),
            {"kind": "tool.progress", "name": "mcp__s__slow", "message": "step 1", "progress": 1, "total": 3},
        )


class ProbeCommandTest(_ServerCase):
    """`xiaoyu mcp probe`：不经模型的排障入口。"""

    def setUp(self):
        super().setUp()
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        (self.workspace / ".mcp.json").write_text(
            json.dumps({"mcpServers": {
                "notify": {"command": sys.executable, "args": [str(self.script)], "tools": ["cwd", "nope"]},
                "badcwd": {"command": sys.executable, "args": [str(self.script)], "cwd": str(self.root / "missing")},
            }}),
            encoding="utf-8",
        )
        patcher = mock.patch.object(cli.Path, "cwd", staticmethod(lambda: self.workspace))
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_probe(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.mcp_command(["probe", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_named_server_shows_handshake_tools_and_hidden_marks(self):
        code, out, _ = self.run_probe("notify")
        self.assertEqual(code, 0, out)
        self.assertIn("已连接 notify", out)
        self.assertIn("notify-server 0.1", out)
        self.assertIn("协议 " + mcp.PROTOCOL_VERSION, out)
        self.assertIn("能力：logging, tools", out)
        self.assertIn("先调 cwd 再调 slow", out)
        self.assertIn("工具（4），其中 3 个不在声明的 tools 名单里", out)
        self.assertIn("✗ slow", out)
        self.assertNotIn("✗ cwd", out)
        #  探测的日志不和会话里同名 server 的日志打架
        self.assertIn("mcp-probe-notify.log", out)

    def test_script_mode_emits_one_json_line_per_step(self):
        script = self.root / "steps.json"
        script.write_text(json.dumps({"steps": [
            {"op": "listTools"},
            {"op": "call_tool", "name": "cwd"},
            {"op": "callTool", "name": "chatty", "args": {}},
        ]}), encoding="utf-8")
        code, out, err = self.run_probe("--script", str(script), "--", sys.executable, str(self.script))
        self.assertEqual(code, 0, out + err)
        records = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["op"] for r in records], ["initialize", "listTools", "callTool", "callTool"])
        self.assertTrue(all(r["ok"] for r in records))
        self.assertEqual(records[0]["capabilities"], ["logging", "tools"])
        self.assertEqual(records[1]["tools"], ["cwd", "slow", "chatty", "die"])
        self.assertEqual(records[2]["name"], "cwd")
        self.assertIn("seconds", records[2])
        #  server 的日志通知直接上 stderr
        self.assertIn("warning] ", err)
        self.assertIn("bad thing", err)

    def test_script_mode_classifies_errors_and_exits_nonzero(self):
        script = self.root / "steps.json"
        script.write_text(json.dumps([{"op": "callTool", "name": "die"}]), encoding="utf-8")
        code, out, _ = self.run_probe("--script", str(script), "--timeout", "5", "--", sys.executable, str(self.script))
        self.assertEqual(code, 1)
        record = json.loads(out.splitlines()[-1])
        self.assertFalse(record["ok"])
        self.assertEqual(record["error_kind"], "dropped")
        self.assertIn("died on purpose", record["error"])

    def test_startup_failure_in_script_mode_includes_log_tail(self):
        script = self.root / "steps.json"
        script.write_text("[]", encoding="utf-8")
        with mock.patch.dict(os.environ, {"DIE_AT_START": "1"}):
            #  环境走白名单，DIE_AT_START 得显式点名透传
            (self.workspace / ".mcp.json").write_text(json.dumps({"mcpServers": {
                "dead": {"command": sys.executable, "args": [str(self.script)], "inheritEnv": ["DIE_AT_START"]},
            }}), encoding="utf-8")
            code, out, _ = self.run_probe("--script", str(script), "dead")
        self.assertEqual(code, 1)
        record = json.loads(out.splitlines()[0])
        self.assertFalse(record["ok"])
        self.assertEqual(record["op"], "initialize")
        self.assertTrue(any("missing env FOO" in line for line in record["log_tail"]))
        self.assertTrue(any("[REDACTED]" in line for line in record["log_tail"]))

    def test_missing_cwd_is_reported(self):
        code, _, err = self.run_probe("badcwd")
        self.assertEqual(code, 1)
        self.assertIn("工作目录不存在", err)

    def test_unknown_name_and_bad_script_exit_2(self):
        with mock.patch.object(cli.shutil, "which", lambda _: None):
            code, _, err = self.run_probe("ghost")
        self.assertEqual(code, 2)
        self.assertIn("ghost", err)
        self.assertIn("notify", err)  # 列出已声明的
        bad = self.root / "bad.json"
        bad.write_text(json.dumps([{"op": "fly"}]), encoding="utf-8")
        code, _, err = self.run_probe("--script", str(bad), "notify")
        self.assertEqual(code, 2)
        self.assertIn("op 只能是", err)
        self.assertEqual(self.run_probe()[0], 2)


if __name__ == "__main__":
    unittest.main()
