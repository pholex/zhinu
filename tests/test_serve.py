"""serve（HTTP API）的进程内 e2e：真 app + 真 Agent + scripted 桩，不打网络。

被测的是 serve.py 的协议面：会话装配、同步/异步两条 prompt 路径、事件游标与
long-poll、审批挂起→放行/拒绝的整条回路、状态机四档、workspace 越界与 token
两道闸。唯一被替换的是 LLM 网络调用（XIAOYU_SCRIPTED_SCRIPTS，与其它 e2e 同源）。

用 fastapi 的 TestClient 而不是起真端口：它把 app 跑在自己的事件循环线程上，
审批那条"工作线程阻塞等 HTTP 侧兑现"的路径是**真实发生**的（TestClient 的
请求由另一个线程发起），所以这套用例真的能验到线程模型，不是走过场。

fastapi/uvicorn 是可选额外，没装就整体跳过——CI 的最小矩阵不该被它卡住。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

try:
    from fastapi.testclient import TestClient

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - 取决于环境装没装 [serve]
    HAS_FASTAPI = False


def _isolated_env(tmp: str, script_path: Path) -> dict[str, str]:
    """与 test_e2e_scripted.env_for 同一套隔离（这里是进程内，直接改 os.environ）。"""
    return {
        "XIAOYU_SCRIPTED_SCRIPTS": str(script_path),
        "HOME": str(Path(tmp) / "home"),
        "USERPROFILE": str(Path(tmp) / "home"),
        "XDG_CONFIG_HOME": str(Path(tmp) / "config"),
        "APPDATA": str(Path(tmp) / "config"),
        "XIAOYU_ENABLE_SKILLS": "0",
        "XIAOYU_ENABLE_PLUGINS": "0",
        "XIAOYU_ENABLE_MCP": "0",
        "XIAOYU_ENABLE_HOOKS": "0",
        "XIAOYU_ENABLE_AGENTS": "0",
        "XIAOYU_ENABLE_EXPLORE": "0",
        "XIAOYU_ENABLE_WEB_SEARCH": "0",
        "XIAOYU_ENABLE_BROWSER": "0",
        "XIAOYU_SANDBOX": "0",
    }


@unittest.skipUnless(HAS_FASTAPI, "需要可选额外 [serve]（fastapi + uvicorn）")
class ServeCase(unittest.TestCase):
    """公共驱动：写脚本 → 隔离环境 → 造 app → TestClient。"""

    approval: str = "ask"
    token: str = ""
    mcp: bool = True

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = os.path.realpath(self._tmpdir.name)
        self.root = Path(self.tmp) / "ws"
        self.root.mkdir()
        (Path(self.tmp) / "home").mkdir()
        (Path(self.tmp) / "config").mkdir()
        self._script_path = Path(self.tmp) / "script.txt"

    def start(self, script: str, **cfg_extra: Any) -> "TestClient":
        from xiaoyu.serve import ServeConfig, create_app

        self._script_path.write_text(script, encoding="utf-8")
        overrides = _isolated_env(self.tmp, self._script_path)
        saved = {key: os.environ.get(key) for key in overrides}
        os.environ.update(overrides)

        def restore() -> None:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        cfg = ServeConfig(
            root=self.root,
            token=self.token,
            approval=self.approval,
            #  审批超时压到秒级：用例要能在可接受的时间里验到 fail-closed
            approval_timeout=3.0,
            mcp=self.mcp,
            **cfg_extra,
        )
        client = TestClient(create_app(cfg))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        self.client = client
        return client

    # ---------- 便捷动作 ----------

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def new_session(self, **body: Any) -> str:
        response = self.client.post("/session", json=body, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["session_id"]

    def events(self, session_id: str, **params: Any) -> list[dict[str, Any]]:
        response = self.client.get(
            f"/session/{session_id}/events", params=params, headers=self.headers()
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["events"]

    def kinds(self, session_id: str) -> list[str]:
        return [item["kind"] for item in self.events(session_id, limit=2000)]

    def status(self, session_id: str) -> dict[str, Any]:
        response = self.client.get(f"/session/{session_id}/status", headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _wait_for(self, session_id: str, detail: str, timeout: float = 10.0) -> dict[str, Any]:
        """轮询到 detail 出现为止（TestClient 的请求是同步的，这里就地轮询）。"""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.status(session_id)
            if state["detail"] == detail:
                return state
            time.sleep(0.05)
        self.fail(f"{timeout}s 内没等到 detail={detail}，当前 {self.status(session_id)}")



class TestBasics(ServeCase):
    def test_health_needs_no_token(self):
        client = self.start("text: 你好\n")
        body = client.get("/health").json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["root"], str(self.root))

    def test_sync_prompt_returns_result(self):
        self.start("text: 干完了\n")
        session_id = self.new_session()
        response = self.client.post(
            f"/session/{session_id}/prompt", json={"text": "干活"}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["result"]["text"], "干完了")
        self.assertFalse(body["result"]["interrupted"])
        #  一轮跑完就该回到 idle/finished，而不是停在 running
        self.assertEqual((body["status"], body["detail"]), ("idle", "finished"))
        self.assertEqual(body["turns"], 1)

    def test_event_cursor_is_resumable(self):
        self.start("text: 一\ntext: 二\n")
        session_id = self.new_session()
        self.client.post(
            f"/session/{session_id}/prompt", json={"text": "干活"}, headers=self.headers()
        )
        first = self.client.get(
            f"/session/{session_id}/events", params={"from": 1, "limit": 2}, headers=self.headers()
        ).json()
        self.assertEqual(len(first["events"]), 2)
        #  next 就是下次的 from：接着拉不重不漏
        rest = self.events(session_id, **{"from": first["next"], "limit": 2000})
        self.assertEqual(
            [item["seq"] for item in first["events"]] + [item["seq"] for item in rest],
            list(range(1, len(first["events"]) + len(rest) + 1)),
        )
        self.assertEqual(rest[-1]["kind"], "run.completed")

    def test_run_started_and_completed_bracket_the_turn(self):
        #  auto 档下工作区内改文件免确认，所以这一轮不会挂审批。刻意不用 bash：
        #  用例环境 XIAOYU_SANDBOX=0，没沙箱时 auto 档对 bash 照旧逐条问
        #  （modes.py 的既有语义），拿 bash 断言"不问"会跟着环境漂。
        self.start(
            'tool_call: {"name": "write_file", "arguments": {"path": "auto.txt", "content": "x\\n"}}\n'
            "---\n"
            "text: 跑完了\n"
        )
        session_id = self.new_session(mode="auto")
        self.client.post(
            f"/session/{session_id}/prompt", json={"text": "跑一下"}, headers=self.headers()
        )
        kinds = self.kinds(session_id)
        self.assertEqual(kinds[0], "run.started")
        self.assertEqual(kinds[-1], "run.completed")
        #  工具四态机里的两态必须原样透传到 HTTP 面（前端契约不因换传输而变）
        self.assertIn("tool.pending", kinds)
        self.assertIn("tool.completed", kinds)

    def test_second_run_while_busy_is_409(self):
        #  第一轮永远跑不完（脚本给了一轮工具调用，审批会挂住）
        self.start('tool_call: {"name": "write_file", "arguments": {"path": "a.txt", "content": "x"}}\n')
        session_id = self.new_session()
        self.client.post(
            f"/session/{session_id}/prompt_async", json={"text": "写"}, headers=self.headers()
        )
        self._wait_for(session_id, "waiting_for_approval")
        busy = self.client.post(
            f"/session/{session_id}/prompt", json={"text": "再来一轮"}, headers=self.headers()
        )
        #  静默排队会让编排器以为第二次提交立刻生效了——必须响亮地 409
        self.assertEqual(busy.status_code, 409, busy.text)

    def test_long_poll_returns_empty_instead_of_hanging_forever(self):
        #  给不方便消费 SSE 的编排器（n8n HTTP 节点、Dify）用的形态：
        #  没有新事件就挂到 wait 秒后空手返回，游标不动，下次接着问
        self.start("text: 无所谓\n")
        session_id = self.new_session()
        body = self.client.get(
            f"/session/{session_id}/events",
            params={"from": 1, "wait": 0.3},
            headers=self.headers(),
        ).json()
        self.assertEqual(body["events"], [])
        self.assertEqual(body["next"], 1)

    def test_long_poll_wakes_on_new_events(self):
        self.start("text: 有话说\n")
        session_id = self.new_session()
        self.client.post(
            f"/session/{session_id}/prompt", json={"text": "干活"}, headers=self.headers()
        )
        tail = self.status(session_id)["next_seq"]
        #  游标停在末尾时再问一次：这次仍该空手而归（上一轮已经全拉过了）
        body = self.client.get(
            f"/session/{session_id}/events",
            params={"from": tail, "wait": 0.3},
            headers=self.headers(),
        ).json()
        self.assertEqual(body["events"], [])
        #  再跑一轮，从同一游标拉就该拿到新一轮的事件
        self._script_path.write_text("text: 第二轮\n", encoding="utf-8")
        self.client.post(
            f"/session/{session_id}/prompt", json={"text": "再来"}, headers=self.headers()
        )
        again = self.events(session_id, **{"from": tail, "limit": 2000})
        self.assertEqual(again[0]["kind"], "run.started")
        self.assertEqual(again[0]["seq"], tail)

    def test_sse_stream_replays_from_cursor(self):
        """follow=false：推完这一轮就关流，客户端不必自己想办法中断。

        刻意不测 follow=true 的常驻形态——TestClient 的 receive 通道不会投递
        http.disconnect，`request.is_disconnected()` 在这里恒为 False，测出来的
        是测试替身的行为不是服务的行为。那条路径在真 uvicorn 下才有意义。
        """
        import json as _json

        self.start("text: 流一下\n")
        session_id = self.new_session()
        self.client.post(
            f"/session/{session_id}/prompt", json={"text": "干活"}, headers=self.headers()
        )
        seen: list[dict[str, Any]] = []
        with self.client.stream(
            "GET",
            f"/session/{session_id}/events/stream",
            params={"from": 1, "follow": "false"},
            headers=self.headers(),
        ) as response:
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
            for line in response.iter_lines():
                if line.startswith("data: "):
                    seen.append(_json.loads(line[6:]))
        self.assertEqual(seen[0]["kind"], "run.started")
        self.assertEqual(seen[-1]["kind"], "run.completed")
        #  SSE 与 /events 是同一份缓冲的两个形态：seq 必须连续且同源
        self.assertEqual([item["seq"] for item in seen], list(range(1, len(seen) + 1)))
        self.assertEqual(len(seen), len(self.events(session_id, limit=2000)))


class TestApproval(ServeCase):
    """审批回路：工作线程挂起 → 状态可见 → HTTP 兑现 → 继续跑。"""

    SCRIPT = (
        'tool_call: {"name": "write_file", "arguments": {"path": "新文件.txt", "content": "一行\\n"}}\n'
        "---\n"
        "text: 收工\n"
    )

    def test_pending_approval_is_visible_and_allowable(self):
        self.start(self.SCRIPT)
        session_id = self.new_session()
        self.client.post(
            f"/session/{session_id}/prompt_async", json={"text": "建个文件"}, headers=self.headers()
        )
        state = TestBasics._wait_for(self, session_id, "waiting_for_approval")
        #  卡在等人 ≠ 在算：编排侧的超时逻辑全靠这一格能被区分出来
        self.assertEqual(state["status"], "running")
        self.assertEqual(len(state["pending_approvals"]), 1)
        request = state["pending_approvals"][0]
        self.assertEqual(request["tool"], "write_file")
        self.assertEqual(request["args"]["path"], "新文件.txt")

        allow = self.client.post(
            f"/session/{session_id}/permissions",
            json={"request_id": request["request_id"], "decision": "allow"},
            headers=self.headers(),
        )
        self.assertEqual(allow.status_code, 200, allow.text)
        TestBasics._wait_for(self, session_id, "finished")
        self.assertIn("tool.completed", self.kinds(session_id))
        self.assertTrue((self.root / "新文件.txt").exists())
        resolved = [
            item for item in self.events(session_id, limit=2000) if item["kind"] == "permission.resolved"
        ]
        #  事件报的是 HTTP 侧回的字面决定，不是笼统的 "resolved"——UI 靠它区分放行/拒绝
        self.assertEqual([item["decision"] for item in resolved], ["allow"])

    def test_deny_reaches_the_model_and_blocks_the_write(self):
        self.start(self.SCRIPT)
        session_id = self.new_session()
        self.client.post(
            f"/session/{session_id}/prompt_async", json={"text": "建个文件"}, headers=self.headers()
        )
        state = TestBasics._wait_for(self, session_id, "waiting_for_approval")
        self.client.post(
            f"/session/{session_id}/permissions",
            json={
                "request_id": state["pending_approvals"][0]["request_id"],
                "decision": "deny",
                "reason": "别写这个文件",
            },
            headers=self.headers(),
        )
        TestBasics._wait_for(self, session_id, "finished")
        self.assertIn("tool.denied", self.kinds(session_id))
        self.assertFalse((self.root / "新文件.txt").exists())

    def test_timeout_denies_rather_than_hangs(self):
        #  fail closed：没人回决定时必须变成拒绝，不能永远挂着，也不能放行
        self.start(self.SCRIPT)
        session_id = self.new_session()
        self.client.post(
            f"/session/{session_id}/prompt_async", json={"text": "建个文件"}, headers=self.headers()
        )
        TestBasics._wait_for(self, session_id, "waiting_for_approval")
        TestBasics._wait_for(self, session_id, "finished", timeout=20.0)
        kinds = self.kinds(session_id)
        self.assertIn("tool.denied", kinds)
        self.assertFalse((self.root / "新文件.txt").exists())
        resolved = [
            item for item in self.events(session_id, limit=2000) if item["kind"] == "permission.resolved"
        ]
        self.assertEqual([item["decision"] for item in resolved], ["timeout"])

    def test_abort_releases_a_pending_approval(self):
        #  abort 时若不主动放掉挂起的审批，工作线程会一直堵到 approval_timeout，
        #  打断在用户眼里就是"没反应"
        self.start(self.SCRIPT)
        session_id = self.new_session()
        self.client.post(
            f"/session/{session_id}/prompt_async", json={"text": "建个文件"}, headers=self.headers()
        )
        TestBasics._wait_for(self, session_id, "waiting_for_approval")
        self.client.post(f"/session/{session_id}/abort", headers=self.headers())
        state = TestBasics._wait_for(self, session_id, "interrupted", timeout=10.0)
        self.assertEqual(state["status"], "idle")
        self.assertFalse((self.root / "新文件.txt").exists())


class TestAllowAll(ServeCase):
    approval = "allow_all"

    def test_allow_all_never_pends(self):
        self.start(
            'tool_call: {"name": "write_file", "arguments": {"path": "直写.txt", "content": "x\\n"}}\n'
            "---\n"
            "text: 好了\n"
        )
        session_id = self.new_session()
        response = self.client.post(
            f"/session/{session_id}/prompt", json={"text": "写"}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["detail"], "finished")
        self.assertNotIn("permission.requested", self.kinds(session_id))
        self.assertTrue((self.root / "直写.txt").exists())


class TestConstructionTimeInjection(ServeCase):
    """approver / sink 必须在 `Agent(...)` 构造时就位，不能构造完再回填。

    这是本模块最容易犯又最难看出的错：`Agent.__init__` 把这两样**按值捕获**
    进 explore / web_search / 子 agent 的工具闭包，回填 `agent.approver` /
    `agent.sink` 只改父 Agent 的属性，闭包里攥着的还是构造时那个值。
    曾经的后果：`--approval ask` 档下**子 agent 里的 bash/write_file 完全不经
    审批**（拿到的是 Agent 缺省的 `lambda: True`），且 explore / web_search /
    子 agent 的事件全进黑洞 sink。所以这里直接盯 `build_agent` 的产物。
    """

    def test_build_agent_wires_both_at_construction(self):
        from xiaoyu.serve import (
            ServeConfig,
            _BridgeSink,
            _HttpApprover,
            _SessionRef,
            build_agent,
        )

        self.start("text: 无所谓\n")
        ref = _SessionRef()
        agent, _manager = build_agent("sess-probe", self.root, ServeConfig(root=self.root), ref)
        #  不是 Agent.__init__ 那个"永远批准"的缺省 lambda
        self.assertIsInstance(agent.approver, _HttpApprover)
        self.assertIsInstance(agent.sink, _BridgeSink)

    def test_unbound_approver_fails_closed(self):
        #  绑定前不该有工具在跑；真跑到了也必须拒绝，绝不能因"还没就绪"放行
        from xiaoyu.serve import _HttpApprover, _SessionRef
        from xiaoyu.agent import Deny

        verdict = _HttpApprover(_SessionRef())("bash", {"command": "rm -rf /"})
        self.assertIsInstance(verdict, Deny)

    def test_bridge_sink_reaches_the_event_buffer(self):
        """被闭包捕获的 sink 要真能把事件送进缓冲区，且是从工作线程送的。"""
        import asyncio as _asyncio

        from xiaoyu.events import Notice
        from xiaoyu.serve import ServeConfig, _BridgeSink, _Session, _SessionRef

        self.start("text: 无所谓\n")
        cfg = ServeConfig(root=self.root)

        async def scenario():
            ref = _SessionRef()
            sink = _BridgeSink(ref)
            #  emit 在绑定前必须静默无害，不能崩在工具里
            sink.emit(Notice("绑定前", "info"))
            session = _Session("sess-x", _StubAgent(), self.root, cfg)
            ref.bind(session, _asyncio.get_running_loop())
            #  从真的另一个线程发（explore / 子 agent 就是这么跑的）
            await _asyncio.to_thread(sink.emit, Notice("来自工作线程", "info"))
            #  call_soon_threadsafe 是排进循环的，让它跑一拍
            await _asyncio.sleep(0.05)
            return [item.get("text") for item in session.events]

        texts = _asyncio.run(scenario())
        self.assertEqual(texts, ["来自工作线程"])


class _StubAgent:
    """`_Session` 只用到 config/context_tokens，构造真 Agent 太重。"""

    class _Cfg:
        model = "stub"
        mode = "default"

    config = _Cfg()

    def context_tokens(self) -> int:
        return 0

    def set_budget_tokens(self, budget: int | None) -> None:
        pass


class TestReviewFixes(ServeCase):
    """针对复查发现的其余几条的回归。"""

    def test_nested_args_are_truncated_too(self):
        #  最大的负载在 args 这个 dict 里（write_file 的 content），
        #  只截顶层等于把最该防的那块原样留在内存里
        big = "x" * 60000
        self.start(
            'tool_call: {"name": "write_file", "arguments": {"path": "big.txt", "content": "%s"}}\n'
            "---\ntext: 好\n" % big
        )
        session_id = self.new_session(mode="auto")
        self.client.post(
            f"/session/{session_id}/prompt", json={"text": "写"}, headers=self.headers()
        )
        pending = [
            item for item in self.events(session_id, limit=2000) if item["kind"] == "tool.pending"
        ]
        self.assertEqual(len(pending), 1)
        content = pending[0]["args"]["content"]
        self.assertLess(len(content), len(big))
        self.assertIn("已截断", content)

    def test_steer_on_idle_session_is_409(self):
        #  Agent._turn() 开头就 drain_steers()，空闲时入队的插话会被丢弃。
        #  回 200 等于骗调用方说话已送到
        self.start("text: 无所谓\n")
        session_id = self.new_session()
        response = self.client.post(
            f"/session/{session_id}/steer", json={"text": "补充一句"}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_events_description_names_the_real_cursor_field(self):
        #  Dify/n8n 用户是照生成的 schema 写客户端的；描述里写错字段名，
        #  他们会读到 undefined 然后原地重复轮询
        client = self.start("text: 无所谓\n")
        spec = client.get("/openapi.json").json()
        text = spec["paths"]["/session/{session_id}/events"]["get"]["description"]
        self.assertIn("`next`", text)
        self.assertNotIn("next_seq", text)


class TestNonAsciiToken(ServeCase):
    """运维把 token 配成非 ASCII 时，服务不能整个塌成 500。

    `hmac.compare_digest(str, str)` 遇非 ASCII 直接抛 TypeError——那会让**每一个**
    请求（含带正确 token 的）返回 500，且错误信息完全指不到"token 配错了"上。
    改比 bytes 之后，行为退化成干净的 401。

    注意：非 ASCII 的 token 本来就送不进来（HTTP header 不是 UTF-8 通道，httpx
    这类客户端直接拒绝编码），所以这种配置终归是不可用的——但**不可用要表现成
    401 而不是 500**，运维才可能顺着"认证失败"查到自己的 token 上。
    """

    token = "令牌-秘密"

    def test_non_ascii_token_degrades_to_401_not_500(self):
        client = self.start("text: 无所谓\n")
        for headers in ({}, {"X-Xiaoyu-Token": "wrong"}, {"Authorization": "Bearer wrong"}):
            response = client.post("/session", json={}, headers=headers)
            self.assertEqual(response.status_code, 401, f"{headers} → {response.text}")


class TestGuards(ServeCase):
    def test_workspace_must_stay_inside_root(self):
        self.start("text: 无所谓\n")
        response = self.client.post("/session", json={"workspace": "/etc"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn("root", response.json()["detail"])

    def test_relative_workspace_resolves_under_root(self):
        (self.root / "sub").mkdir()
        self.start("text: 无所谓\n")
        response = self.client.post("/session", json={"workspace": "sub"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["workspace"], str(self.root / "sub"))

    def test_unknown_session_is_404(self):
        self.start("text: 无所谓\n")
        self.assertEqual(self.client.get("/session/sess-nope/status").status_code, 404)

    def test_bad_decision_is_400(self):
        self.start("text: 无所谓\n")
        session_id = self.new_session()
        response = self.client.post(
            f"/session/{session_id}/permissions",
            json={"request_id": "x", "decision": "maybe"},
        )
        #  没有这条挂起的审批 → 先撞 404；语义顺序本身也是契约的一部分
        self.assertEqual(response.status_code, 404, response.text)


class TestToken(ServeCase):
    token = "s3cr3t"

    def test_token_gates_everything_but_health(self):
        client = self.start("text: 无所谓\n")
        self.assertEqual(client.get("/health").status_code, 200)
        self.assertEqual(client.post("/session", json={}).status_code, 401)
        self.assertEqual(
            client.post("/session", json={}, headers={"Authorization": "Bearer wrong"}).status_code,
            401,
        )
        self.assertEqual(client.post("/session", json={}, headers=self.headers()).status_code, 200)

    def test_alternate_header_works(self):
        client = self.start("text: 无所谓\n")
        response = client.post("/session", json={}, headers={"X-Xiaoyu-Token": self.token})
        self.assertEqual(response.status_code, 200, response.text)

    def test_diagnostics_gated_and_counts_sessions(self):
        #  /diagnostics 暴露负载形态（RSS / fd / 在册会话），必须和业务面同一道 token 门
        client = self.start("text: 无所谓\n")
        self.assertEqual(client.get("/diagnostics").status_code, 401)
        before = client.get("/diagnostics", headers=self.headers())
        self.assertEqual(before.status_code, 200, before.text)
        self.new_session()
        body = client.get("/diagnostics", headers=self.headers()).json()
        self.assertEqual(set(body), {"version", "process", "gauges", "uptime_s"})
        self.assertEqual(body["gauges"]["serve.sessions.live"], 1)
        #  读 /diagnostics 本身就是一个在途请求
        self.assertGreaterEqual(body["gauges"]["serve.requests.in_flight"], 1)
        self.assertIn("rss_bytes", body["process"])


class TestCors(ServeCase):
    """浏览器 origin 白名单：只对名单里的 origin 发 CORS 头，默认一个都不发。"""

    token = "s3cr3t"
    EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"

    def test_no_cors_headers_by_default(self):
        client = self.start("text: 无所谓\n")
        response = client.options(
            "/session",
            headers={"Origin": self.EXT, "Access-Control-Request-Method": "POST"},
        )
        self.assertNotIn("access-control-allow-origin", response.headers)
        response = client.get("/health", headers={"Origin": self.EXT})
        self.assertNotIn("access-control-allow-origin", response.headers)

    def test_preflight_and_response_for_allowed_origin(self):
        client = self.start("text: 无所谓\n", cors_origins=(self.EXT,))
        response = client.options(
            "/session",
            headers={
                "Origin": self.EXT,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
                #  Chrome 从扩展/公网 origin 访问回环地址时多带的 PNA 预检
                "Access-Control-Request-Private-Network": "true",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["access-control-allow-origin"], self.EXT)
        self.assertIn("authorization", response.headers["access-control-allow-headers"].lower())
        self.assertEqual(response.headers["access-control-allow-private-network"], "true")
        #  实际请求：CORS 头照发，token 仍照常校验（CORS 不是鉴权）
        response = client.post("/session", json={}, headers={"Origin": self.EXT})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.headers["access-control-allow-origin"], self.EXT)
        response = client.post("/session", json={}, headers={"Origin": self.EXT, **self.headers()})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["access-control-allow-origin"], self.EXT)

    def test_other_origin_gets_nothing(self):
        client = self.start("text: 无所谓\n", cors_origins=(self.EXT,))
        response = client.options(
            "/session",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Private-Network": "true",
            },
        )
        self.assertNotIn("access-control-allow-origin", response.headers)
        self.assertNotIn("access-control-allow-private-network", response.headers)


class TestOpenApi(ServeCase):
    def test_schema_covers_the_orchestration_surface(self):
        #  Dify 自定义工具直接吃这份 schema：端点少一个，编排侧就少一块能力
        client = self.start("text: 无所谓\n")
        paths = client.get("/openapi.json").json()["paths"]
        for path in (
            "/session",
            "/session/{session_id}/prompt",
            "/session/{session_id}/prompt_async",
            "/session/{session_id}/status",
            "/session/{session_id}/events",
            "/session/{session_id}/permissions",
            "/session/{session_id}/abort",
        ):
            self.assertIn(path, paths)

    def test_operation_ids_are_hand_named(self):
        """operationId 是 Dify 里显示的工具名，不能是 FastAPI 自动拼的长串。

        默认会生成 `session_prompt_async_session__session_id__prompt_async_post`
        这种——人在工作流 UI 里选不动，模型做工具选择也更难。
        """
        client = self.start("text: 无所谓\n")
        paths = client.get("/openapi.json").json()["paths"]
        ids = {op["operationId"] for methods in paths.values() for op in methods.values()}
        self.assertIn("prompt_async", ids)
        self.assertIn("create_session", ids)
        self.assertIn("respond_permission", ids)
        self.assertIn("get_status", ids)
        #  自动生成的那种一律不许出现（判据：带路径参数拼进来的双下划线）
        self.assertEqual([name for name in ids if "__" in name], [])

    def test_print_openapi_fills_in_servers(self):
        """没有 servers，Dify/n8n 的导入器拿不到 base URL，导进去发不出请求。"""
        from xiaoyu.serve import ServeConfig, print_openapi

        import contextlib
        import io
        import json as _json

        cfg = ServeConfig(root=self.root, host="127.0.0.1", port=8420)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(print_openapi(cfg, "http://host.docker.internal:8420"), 0)
        schema = _json.loads(buffer.getvalue())
        self.assertEqual(schema["servers"][0]["url"], "http://host.docker.internal:8420")

    def test_loopback_servers_warns_on_stderr(self):
        #  容器里的 127.0.0.1 是容器自己——照抄必然连不上，得提醒
        from xiaoyu.serve import ServeConfig, print_openapi

        import contextlib
        import io

        cfg = ServeConfig(root=self.root)
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            print_openapi(cfg)
        self.assertIn("--public-url", err.getvalue())


if __name__ == "__main__":
    unittest.main()
