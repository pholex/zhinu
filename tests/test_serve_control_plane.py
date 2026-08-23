"""serve 控制面：agent 对象（版本钉定）、会话预算硬闸、清单落盘与重启恢复。

驱动方式与 test_serve 同源（真 app + 真 Agent + scripted 桩，进程内 TestClient）。
另有一组不经 HTTP 的纯逻辑用例直接打 serve_state（预算结算、fail closed 规则）。
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from tests.test_serve import HAS_FASTAPI, ServeCase
from xiaoyu.serve_state import AgentStore, Budget, StateError, budget_breach, spend_of

SIMPLE = 'usage: {"prompt_tokens": 100, "completion_tokens": 50}\ntext: 干完了\n'


class TestSpendLogic(unittest.TestCase):
    def test_token_budget(self):
        spend = spend_of({"p/m": (1, 100, 50)}, {}, want_usd=False)
        self.assertEqual(spend.tokens, 150)
        self.assertIsNone(spend.usd)
        self.assertEqual(budget_breach(Budget(tokens=150), spend)[:5], "token")
        self.assertEqual(budget_breach(Budget(tokens=151), spend), "")
        self.assertEqual(budget_breach(None, spend), "")

    def test_usd_budget_prices_by_fqn_then_bare_name(self):
        pricing = {"m": {"input": 10.0, "output": 20.0}}
        spend = spend_of({"p/m": (1, 1_000_000, 500_000)}, pricing, want_usd=True)
        self.assertAlmostEqual(spend.usd, 20.0)
        self.assertEqual(budget_breach(Budget(usd=20.0), spend)[:2], "花费")
        self.assertEqual(budget_breach(Budget(usd=20.01), spend), "")

    def test_unpriced_model_fails_closed(self):
        #  钱的事上"算不出来"必须等于"已超支"，不能等于"不设限"
        spend = spend_of({"p/other": (1, 10, 10)}, {"m": {"input": 1, "output": 1}}, want_usd=True)
        self.assertIsNone(spend.usd)
        self.assertEqual(spend.unpriced, ("p/other",))
        self.assertIn("没有定价", budget_breach(Budget(usd=100.0), spend))
        #  只设 token 预算时不关心定价
        self.assertEqual(budget_breach(Budget(tokens=100), spend), "")

    def test_budget_parse_rejects_garbage(self):
        self.assertIsNone(Budget.parse(None))
        self.assertIsNone(Budget.parse({}))
        self.assertIsNone(Budget.parse({"tokens": None, "usd": None}))
        for bad in ({"tokens": 0}, {"tokens": True}, {"usd": -1}, {"coins": 3}, "5"):
            with self.assertRaises(StateError):
                Budget.parse(bad)

    def test_agent_store_versions_and_archive(self):
        store = AgentStore(None)
        record = store.create("写手", {"model": "a", "budget": {"tokens": 10}})
        self.assertEqual(record["version"], 1)
        store.update(record["agent_id"], {"model": "b"})
        _, version, config = store.resolve(record["agent_id"])
        self.assertEqual((version, config["model"], config["budget"]["tokens"]), (2, "b", 10))
        _, version, config = store.resolve({"id": record["agent_id"], "version": 1})
        self.assertEqual((version, config["model"]), (1, "a"))
        store.archive(record["agent_id"])
        with self.assertRaises(StateError):
            store.resolve(record["agent_id"])
        with self.assertRaises(StateError):
            store.update(record["agent_id"], {"model": "c"})
        with self.assertRaises(StateError):
            store.create("x", {"nope": 1})


@unittest.skipUnless(HAS_FASTAPI, "需要可选额外 [serve]（fastapi + uvicorn）")
class TestAgents(ServeCase):
    def create_agent(self, name: str = "a", **config: Any) -> dict[str, Any]:
        response = self.client.post(
            "/agent", json={"name": name, "config": config}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_session_pins_version_and_later_updates_do_not_leak(self):
        self.start(SIMPLE)
        agent = self.create_agent(mode="auto", append_system_prompt="你是写手")
        session_id = self.new_session(agent=agent["agent_id"])
        info = self.client.get(f"/session/{session_id}", headers=self.headers()).json()
        self.assertEqual(info["agent"], {"id": agent["agent_id"], "name": "a", "version": 1})
        self.assertEqual(info["mode"], "auto")
        #  更新 → 版本 2；老会话仍是版本 1 的配置
        updated = self.client.post(
            f"/agent/{agent['agent_id']}", json={"config": {"mode": "plan"}}, headers=self.headers()
        ).json()
        self.assertEqual(updated["version"], 2)
        self.assertEqual(updated["config"]["append_system_prompt"], "你是写手")  # 没给的沿用
        info = self.client.get(f"/session/{session_id}", headers=self.headers()).json()
        self.assertEqual((info["agent"]["version"], info["mode"]), (1, "auto"))
        #  新会话默认最新版；也可显式钉回 1
        latest = self.new_session(agent=agent["agent_id"])
        pinned = self.new_session(agent={"id": agent["agent_id"], "version": 1})
        self.assertEqual(self.client.get(f"/session/{latest}", headers=self.headers()).json()["mode"], "plan")
        self.assertEqual(self.client.get(f"/session/{pinned}", headers=self.headers()).json()["mode"], "auto")
        detail = self.client.get(
            f"/agent/{agent['agent_id']}", params={"versions": "true"}, headers=self.headers()
        ).json()
        self.assertEqual([item["version"] for item in detail["versions"]], [1, 2])

    def test_archived_agent_refuses_new_sessions_but_running_ones_survive(self):
        self.start(SIMPLE)
        agent = self.create_agent()
        session_id = self.new_session(agent=agent["agent_id"])
        archived = self.client.delete(f"/agent/{agent['agent_id']}", headers=self.headers()).json()
        self.assertTrue(archived["archived"])
        refused = self.client.post("/session", json={"agent": agent["agent_id"]}, headers=self.headers())
        self.assertEqual(refused.status_code, 400, refused.text)
        #  已有会话照跑
        response = self.client.post(
            f"/session/{session_id}/prompt", json={"text": "干活"}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["detail"], "finished")

    def test_agent_cannot_loosen_server_guardrails(self):
        self.start(SIMPLE)  # 服务端 approval=ask
        for config in ({"approval": "allow_all"}, {"sandbox": False}, {"sandbox_network": True}):
            response = self.client.post(
                "/agent", json={"name": "松", "config": config}, headers=self.headers()
            )
            self.assertEqual(response.status_code, 400, (config, response.text))
        #  收紧可以
        self.create_agent(approval="ask", sandbox=True, sandbox_network=False)
        #  更新时同样检查（合并后的配置）
        agent = self.create_agent()
        response = self.client.post(
            f"/agent/{agent['agent_id']}", json={"config": {"approval": "allow_all"}}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 400, response.text)

    def test_agent_plus_model_override_is_400_and_unknown_is_404(self):
        self.start(SIMPLE)
        agent = self.create_agent()
        response = self.client.post(
            "/session", json={"agent": agent["agent_id"], "mode": "auto"}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 400, response.text)
        response = self.client.post("/session", json={"agent": "agent-nope"}, headers=self.headers())
        self.assertEqual(response.status_code, 404, response.text)
        response = self.client.post(
            "/session", json={"agent": {"id": agent["agent_id"], "version": 9}}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 404, response.text)


@unittest.skipUnless(HAS_FASTAPI, "需要可选额外 [serve]（fastapi + uvicorn）")
class TestBudget(ServeCase):
    def prompt(self, session_id: str) -> Any:
        return self.client.post(
            f"/session/{session_id}/prompt", json={"text": "干活"}, headers=self.headers()
        )

    def test_token_budget_is_a_hard_gate_until_raised(self):
        self.start(SIMPLE + "---\n" + SIMPLE)
        session_id = self.new_session(budget={"tokens": 120})
        first = self.prompt(session_id)
        self.assertEqual(first.status_code, 200, first.text)
        body = first.json()
        #  本轮 150 token ≥ 120：轮末结账标成 budget_reached，reason 可读
        self.assertEqual((body["status"], body["detail"]), ("idle", "budget_reached"))
        self.assertEqual(body["spend"]["tokens"], 150)
        self.assertIn("已达预算", body["budget_reason"])
        self.assertIn("budget.reached", self.kinds(session_id))
        #  再跑是 409，不是静默烧钱
        second = self.prompt(session_id)
        self.assertEqual(second.status_code, 409, second.text)
        self.assertIn("预算已耗尽", second.json()["detail"])
        #  调高预算后放行
        raised = self.client.post(
            f"/session/{session_id}/budget", json={"budget": {"tokens": 1000}}, headers=self.headers()
        )
        self.assertEqual(raised.status_code, 200, raised.text)
        self.assertEqual(raised.json()["budget_reason"], "")
        self.assertEqual(self.prompt(session_id).json()["detail"], "finished")
        #  撤掉预算
        cleared = self.client.post(f"/session/{session_id}/budget", json={"budget": None}, headers=self.headers())
        self.assertIsNone(cleared.json()["budget"])

    def test_budget_interrupts_mid_turn(self):
        #  三轮模型调用（两次工具 + 收尾），每轮 200 token；预算 150 → 第一轮后即越线，
        #  后续被 interrupt 掉，而不是等整轮跑完再算账
        tool = 'tool_call: {"name": "write_file", "arguments": {"path": "a.txt", "content": "x\\n"}}\n'
        usage = 'usage: {"prompt_tokens": 100, "completion_tokens": 100}\n'
        self.start(usage + tool + "---\n" + usage + tool + "---\n" + usage + "text: 收尾\n")
        session_id = self.new_session(mode="auto", budget={"tokens": 150})
        body = self.prompt(session_id).json()
        self.assertEqual(body["detail"], "budget_reached")
        self.assertTrue(body["result"]["interrupted"])
        self.assertLess(body["spend"]["tokens"], 600)
        kinds = self.kinds(session_id)
        self.assertIn("budget.reached", kinds)
        self.assertEqual(kinds[-1], "run.completed")

    def test_usd_budget_fails_closed_without_pricing_and_works_with_it(self):
        self.start(SIMPLE + "---\n" + SIMPLE)
        #  先探出用量账本里的模型键，再按它配定价
        probe = self.new_session()
        used = list(self.prompt(probe).json()["usage"]["by_model"])
        self.assertEqual(len(used), 1)
        model = used[0]
        unpriced = self.client.post(
            "/agent", json={"name": "没定价", "config": {"budget": {"usd": 100}}}, headers=self.headers()
        ).json()
        session_id = self.new_session(agent=unpriced["agent_id"])
        body = self.prompt(session_id).json()
        self.assertEqual(body["detail"], "budget_reached")
        self.assertIn("没有定价", body["budget_reason"])
        self.assertEqual(body["spend"]["unpriced"], [model])
        priced = self.client.post(
            "/agent",
            json={
                "name": "有定价",
                #  100 prompt × $10/M + 50 completion × $30/M = $0.0025 一轮
                "config": {"pricing": {model: {"input": 10, "output": 30}}, "budget": {"usd": 0.004}},
            },
            headers=self.headers(),
        ).json()
        session_id = self.new_session(agent=priced["agent_id"])
        first = self.prompt(session_id).json()
        self.assertEqual(first["detail"], "finished")
        self.assertAlmostEqual(first["spend"]["usd"], 0.0025)
        second = self.prompt(session_id).json()
        self.assertEqual(second["detail"], "budget_reached")
        self.assertAlmostEqual(second["spend"]["usd"], 0.005)
        #  会话级预算覆盖 agent 默认预算
        roomy = self.new_session(agent=priced["agent_id"], budget={"usd": 1})
        self.assertEqual(self.prompt(roomy).json()["budget"], {"tokens": None, "usd": 1.0})


@unittest.skipUnless(HAS_FASTAPI, "需要可选额外 [serve]（fastapi + uvicorn）")
class TestPersistence(ServeCase):
    def test_sessions_and_agents_survive_restart(self):
        state = Path(self.tmp) / "state"
        self.start(SIMPLE + "---\n" + SIMPLE, state_dir=state)
        agent = self.client.post(
            "/agent", json={"name": "a", "config": {"mode": "auto", "budget": {"tokens": 1000}}}, headers=self.headers()
        ).json()
        kept = self.new_session(agent=agent["agent_id"])
        gone = self.new_session()
        first = self.client.post(f"/session/{kept}/prompt", json={"text": "第一轮"}, headers=self.headers()).json()
        self.assertEqual(first["detail"], "finished")
        next_seq = self.status(kept)["next_seq"]
        self.client.delete(f"/session/{gone}", headers=self.headers())
        #  清单 / agent / 日志都在 state_dir 下，文件即真相
        self.assertTrue((state / "sessions" / f"{kept}.json").is_file())
        self.assertFalse((state / "sessions" / f"{gone}.json").exists())
        self.assertTrue((state / "agents" / f"{agent['agent_id']}.json").is_file())
        self.assertTrue(list((state / "logs").glob("*.jsonl")))

        #  "重启"：关掉旧 app（lifespan 收尾落盘），同一 state_dir 起新 app
        self.client.__exit__(None, None, None)
        self.start(SIMPLE + "---\n" + SIMPLE, state_dir=state)
        listed = {item["session_id"]: item for item in self.client.get("/session", headers=self.headers()).json()["sessions"]}
        self.assertIn(kept, listed)
        self.assertNotIn(gone, listed)
        info = listed[kept]
        self.assertEqual(info["detail"], "recovered")
        self.assertEqual(info["agent"], {"id": agent["agent_id"], "name": "a", "version": 1})
        self.assertEqual((info["mode"], info["turns"], info["budget"]["tokens"]), ("auto", 1, 1000))
        #  历史接回来了（system + user + assistant），不是空会话
        self.assertGreater(info["context_tokens"], 0)
        #  游标接着编号：重启前的事件计入 dropped，seq 不倒流
        self.assertGreaterEqual(info["first_seq"], next_seq)
        self.assertEqual(info["dropped_events"], info["first_seq"] - 1)
        self.assertEqual(self.kinds(kept), ["session.recovered"])
        agents = self.client.get("/agent", headers=self.headers()).json()["agents"]
        self.assertEqual([item["agent_id"] for item in agents], [agent["agent_id"]])
        #  接着跑一轮：预算与配置都随会话回来了
        second = self.client.post(f"/session/{kept}/prompt", json={"text": "第二轮"}, headers=self.headers()).json()
        self.assertEqual((second["detail"], second["turns"]), ("finished", 2))
        #  同一会话日志续写（不是新开一份）；被关掉的会话日志作为留痕保留在盘上
        self.assertEqual(len(list((state / "logs").glob(f"*-id-{kept}.jsonl"))), 1)

    def test_no_persist_keeps_disk_clean(self):
        state = Path(self.tmp) / "state"
        self.start(SIMPLE, state_dir=state, persist=False)
        self.client.post("/agent", json={"name": "a"}, headers=self.headers())
        self.new_session()
        self.assertFalse((state / "agents").exists())
        self.assertFalse((state / "sessions").exists())
        self.assertFalse(self.client.get("/health").json()["persist"])


if __name__ == "__main__":
    unittest.main()


class TestAgentMcpServers(ServeCase):
    """agent 对象自带 MCP server（mcp_servers）：形状 400、服务端档位闸、会话私有 manager。"""

    def create(self, status: int = 200, **config: Any):
        response = self.client.post(
            "/agent", json={"name": "a", "config": config}, headers=self.headers()
        )
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def test_default_off_rejects(self):
        self.start(SIMPLE)
        body = self.create(status=400, mcp_servers={"r": {"url": "https://example.com/mcp"}})
        self.assertIn("--agent-mcp", body["detail"])

    def test_shape_errors_are_400_not_silent(self):
        self.start(SIMPLE, agent_mcp="all")
        self.create(status=400, mcp_servers=["not", "a", "dict"])
        body = self.create(status=400, mcp_servers={"bad": {"type": "sse", "url": "http://127.0.0.1/x"}})
        self.assertIn("bad", body["detail"])
        self.create(status=400, mcp_servers={"nothing": {}})

    def test_http_tier_rejects_stdio_and_plaintext_remote(self):
        self.start(SIMPLE, agent_mcp="http")
        body = self.create(status=400, mcp_servers={"s": {"command": "python", "args": ["x.py"]}})
        self.assertIn("--agent-mcp all", body["detail"])
        #  明文 http 只许回环（headers 里放凭据）
        self.create(status=400, mcp_servers={"r": {"url": "http://example.com/mcp"}})
        self.create(mcp_servers={"r": {"url": "https://example.com/mcp", "headers": {"X": "${NOT_EXPANDED}"}}})

    def test_stdio_server_is_session_private_and_closed_with_session(self):
        import os
        import sys
        import time

        from tests.test_mcp import write_fake_server

        self.start(SIMPLE, agent_mcp="all")
        #  ServeCase 的隔离环境关了 MCP 发现；本用例要真起 server
        os.environ["XIAOYU_ENABLE_MCP"] = "1"
        self.addCleanup(os.environ.__setitem__, "XIAOYU_ENABLE_MCP", "0")
        script = write_fake_server(self.root)
        agent = self.create(mcp_servers={"fake": {"command": sys.executable, "args": [str(script)]}})
        self.assertEqual(agent["version"], 1)
        session_id = self.new_session(agent=agent["agent_id"])
        #  通过 /tools 看不到（检索模式把 MCP 工具藏在 search_tool 后面），直接查会话对象
        session = None
        for _ in range(50):
            session = self._find_session(session_id)
            if session is not None and session.mcp_manager is not None and not session.mcp_manager.loading():
                break
            time.sleep(0.1)
        self.assertIsNotNone(session.mcp_manager)
        names = [tool.name for tool in session.mcp_manager.ready_tools()]
        self.assertTrue(any("fake" in name and "echo" in name for name in names), names)
        #  不带 agent 的会话看不到它（会话私有，不进进程级缓存）
        plain = self._find_session(self.new_session())
        self.assertIsNone(plain.mcp_manager)
        self.client.delete(f"/session/{session_id}", headers=self.headers())
        #  关会话即关 manager：子进程收掉、manager 标记 closed
        self.assertTrue(session.mcp_manager._closed)
        self.assertFalse(session.mcp_manager._servers)

    def _find_session(self, session_id: str):
        return self.client.app.state.sessions.get(session_id)


class TestOpsConsoleSample(ServeCase):
    """examples/ops-console 的 MCP server 走整条平台回路：agent 自带 server →
    use_tool 改派挂审批 → /permissions 放行 → output_schema 结构化收尾。
    样本若与内核脱节（协议形状、字段名），这里先红。"""

    def test_sample_server_end_to_end(self):
        import os
        import sys
        import time

        server = Path(__file__).resolve().parents[1] / "examples" / "ops-console" / "shipments_mcp.py"
        script = (
            'tool_call: {"name": "use_tool", "arguments": {"tool_name": "mcp__shipments__reroute_shipment", '
            '"tool_input": {"id": "SHP-1002", "carrier": "顺丰"}}}\n'
            "---\n"
            'tool_call: {"name": "structured_output", "arguments": {"actions": [{"shipment_id": "SHP-1002", '
            '"action": "reroute→顺丰", "result": "done"}], "summary": "改派 1 单"}}\n'
        )
        self.start(script, agent_mcp="all")
        os.environ["XIAOYU_ENABLE_MCP"] = "1"
        self.addCleanup(os.environ.__setitem__, "XIAOYU_ENABLE_MCP", "0")
        agent = self.client.post("/agent", json={"name": "ops", "config": {
            "mcp_servers": {"shipments": {"command": sys.executable, "args": [str(server)]}},
        }}, headers=self.headers()).json()
        sid = self.new_session(agent=agent["agent_id"])
        session = self.client.app.state.sessions[sid]
        for _ in range(100):
            if session.mcp_manager is not None and not session.mcp_manager.loading():
                break
            time.sleep(0.1)
        schema = {"type": "object", "properties": {"actions": {"type": "array"}, "summary": {"type": "string"}},
                  "required": ["actions", "summary"]}
        self.client.post(f"/session/{sid}/prompt_async", json={"text": "改派延误单", "output_schema": schema},
                         headers=self.headers())
        state = self._wait_for(sid, "waiting_for_approval")
        pending = state["pending_approvals"]
        self.assertEqual(pending[0]["tool"], "use_tool")
        self.client.post(f"/session/{sid}/permissions",
                         json={"request_id": pending[0]["request_id"], "decision": "allow"},
                         headers=self.headers())
        state = self._wait_for(sid, "finished")
        self.assertEqual(state["last_result"]["output"]["summary"], "改派 1 单")
        completed = [e for e in self.events(sid, limit=2000) if e["kind"] == "tool.completed"]
        self.assertIn('"ok": true', completed[0]["output"])
