"""MCP 在压缩与恢复边界变化，下一次请求就应看见当前可用能力。"""

import contextlib
import io
import json
from types import SimpleNamespace

from xiaoyu.agent import Agent
from xiaoyu.mcp import RemoteTool
from xiaoyu.providers import Registry
from xiaoyu.session_log import SessionLog, load_messages
from xiaoyu.tools import Toolbox
from .test_agent_paths import AgentTestCase, FakeClient, chunk


class McpContextBoundaryTest(AgentTestCase):
    def remote(self, name="lookup", version="one", check=lambda: True):
        return RemoteTool(f"mcp__late__{name}", "本机查询夹具",
                          {"type": "object", "properties": {version: {"type": "string"}}},
                          lambda **_: "ok", check, server="late", fingerprint=version)

    def agent(self, ready, log, turns=3):
        client = FakeClient([[chunk(content="本轮完成。")]] * turns)
        view = SimpleNamespace(ready_tools=lambda: ready[:], loading=lambda: False)
        box = Toolbox(self.config, mcp_view=view)
        agent = Agent(self.config, box, registry=Registry.for_client(client), session_log=log)
        return agent, client

    def log(self, name):
        log = SessionLog(self.root / name)
        self.addCleanup(log.release)
        return log

    def test_late_connection_after_compaction_is_visible_on_next_request(self):
        for search in (False, True):
            with self.subTest(search=search), contextlib.redirect_stdout(io.StringIO()):
                self.config.mcp_tool_search = search
                ready = []
                log = self.log(f"late-{search}.jsonl")
                agent, client = self.agent(ready, log)
                agent.send("开始")
                for index in range(8):
                    agent._record({"role": "user", "content": f"旧问题{index}"})
                    agent._record({"role": "assistant", "content": "历史材料" * 500})
                agent.compactor.keep_recent = 2
                def summary(*_):
                    ready.append(self.remote())
                    return "已完成旧步骤，继续使用集成工具。"
                agent.compactor.summarizer = summary
                agent.maybe_compact(force=True)
                self.assertIn("已完成旧步骤", str(load_messages(log.path)))
                agent.send("现在有哪些工具可用")
                sent = client.completions.calls[-1]
                if search:
                    self.assertIn("MCP server「late」已连接", str(sent["messages"]))
                else:
                    self.assertIn("mcp__late__lookup", [t["function"]["name"] for t in sent["tools"]])
                    self.assertIn("新增 mcp__late__lookup", str(sent["messages"]))
                agent.send("继续说明")
                self.assertEqual(len(client.completions.calls), 3, "上线公告不能额外启动模型请求")
                if search:
                    self.assertEqual(str(agent.messages).count("MCP server「late」已连接"), 1)
                self.assertEqual(load_messages(log.path), agent.messages[1:])

    def test_resume_uses_changed_tool_set_and_schema_immediately(self):
        for search in (False, True):
            with self.subTest(search=search), contextlib.redirect_stdout(io.StringIO()):
                self.config.mcp_tool_search = search
                name = f"resume-{search}.jsonl"
                log = self.log(name)
                ready = [self.remote("retired"), self.remote()]
                agent, _ = self.agent(ready, log, turns=1)
                agent.send("记录当前工具")
                log.release()
                ready[:] = [self.remote(version="two")]
                restored, client = self.agent(ready, self.log(name), turns=1)
                restored.restore(load_messages(log.path), source=str(log.path), copy=False)
                restored.send("继续")
                sent = client.completions.calls[0]
                if search:
                    self.assertIn("MCP server「late」已连接（1 个工具）", str(sent["messages"]))
                    result = json.loads(restored.toolbox.run("search_tool", {"query": "lookup"}))
                    self.assertEqual(result["total_hidden_tools"], 1)
                    self.assertIn("two", result["results"][0]["input_schema"]["properties"])
                else:
                    schemas = {t["function"]["name"]: t["function"] for t in sent["tools"]}
                    self.assertNotIn("mcp__late__retired", schemas)
                    self.assertIn("two", schemas["mcp__late__lookup"]["parameters"]["properties"])
                self.assertEqual(load_messages(log.path), restored.messages[1:])
