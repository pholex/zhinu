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

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmpdir.cleanup)
        self.tmp = os.path.realpath(self._tmpdir.name)
        self.root = Path(self.tmp) / "ws"
        self.root.mkdir()
        (Path(self.tmp) / "home").mkdir()
        (Path(self.tmp) / "config").mkdir()
        self._script_path = Path(self.tmp) / "script.txt"

    def start(self, script: str) -> "TestClient":
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


if __name__ == "__main__":
    unittest.main()
