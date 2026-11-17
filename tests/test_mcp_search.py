"""MCP 工具检索模式的测试：分词/BM25、search_tool/use_tool、上线公告、schema 稳定。

单元部分不起进程；集成部分复用 test_mcp 的假 server 走完整链路。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xiaoyu import mcp, mcp_search
from xiaoyu.config import Config
from xiaoyu.tools import Toolbox

from .test_mcp import write_fake_server


class TokenizeTest(unittest.TestCase):
    def test_split_identifier(self):
        self.assertEqual(mcp_search.split_identifier("SearchDashboards"), ["Search", "Dashboards"])
        self.assertEqual(mcp_search.split_identifier("grafana-ai"), ["grafana", "ai"])
        self.assertEqual(mcp_search.split_identifier("mcp__linear__save_issue"),
                         ["mcp", "linear", "save", "issue"])
        #  全大写缩写不拆
        self.assertEqual(mcp_search.split_identifier("OSV"), ["OSV"])

    def test_tokenize_keeps_original_and_pieces(self):
        tokens = mcp_search.tokenize("save_issue quickly")
        self.assertIn("save_issue", tokens)
        self.assertIn("save", tokens)
        self.assertIn("issue", tokens)
        self.assertIn("quickly", tokens)


def entry(name: str, server: str, description: str, params: list[str] | None = None):
    return mcp_search.Entry(
        name=name,
        server=server,
        description=description,
        parameters={"type": "object", "properties": {key: {} for key in params or []}},
    )


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.entries = [
            entry("mcp__linear__save_issue", "linear", "Create or update a Linear issue",
                  ["title", "description"]),
            entry("mcp__linear__list_teams", "linear", "List teams in the workspace"),
            entry("mcp__slack__post_message", "slack", "Post a message to a Slack channel",
                  ["channel", "text"]),
        ]

    def test_exact_qualified_name_fast_path(self):
        ranked = mcp_search.search(self.entries, "mcp__slack__post_message")
        self.assertEqual(len(ranked), 1)
        self.assertEqual(ranked[0][0].name, "mcp__slack__post_message")
        self.assertEqual(ranked[0][1], 1.0)

    def test_exact_bare_name_fast_path(self):
        ranked = mcp_search.search(self.entries, "save_issue")
        self.assertEqual(ranked[0][0].name, "mcp__linear__save_issue")

    def test_relevance_ordering(self):
        ranked = mcp_search.search(self.entries, "linear create issue")
        self.assertEqual(ranked[0][0].name, "mcp__linear__save_issue")

    def test_no_match_and_empty(self):
        self.assertEqual(mcp_search.search(self.entries, "billing invoice"), [])
        self.assertEqual(mcp_search.search(self.entries, "  "), [])
        self.assertEqual(mcp_search.search([], "anything"), [])

    def test_ascii_only_query_unaffected_by_grams(self):
        """纯 ASCII 查询不产生 2-gram，排序与以前一致。"""
        self.assertEqual(mcp_search.char_grams("linear create issue"), [])
        ranked = mcp_search.search(self.entries, "slack message")
        self.assertEqual(ranked[0][0].name, "mcp__slack__post_message")


class CjkSearchTest(unittest.TestCase):
    """中文查询：以前分词吐零 token 直接返回空，检索对中文用户整体失效。"""

    def setUp(self):
        self.entries = [
            entry("mcp__lark__send_mail", "lark", "发送一封邮件给指定收件人", ["to", "subject"]),
            entry("mcp__grafana__search_dashboards", "grafana", "搜索 Grafana 仪表盘", ["query"]),
            entry("mcp__linear__save_issue", "linear", "Create or update a Linear issue"),
        ]

    def test_char_grams(self):
        self.assertEqual(mcp_search.char_grams("发邮件"), ["发邮", "邮件"])
        self.assertEqual(mcp_search.char_grams("邮"), ["邮"])
        #  全角标点/空格是分隔，不跨段成 gram
        self.assertEqual(mcp_search.char_grams("发送，邮件"), ["发送", "邮件"])
        self.assertEqual(mcp_search.tokenize("邮件"), ["邮件"])

    def test_chinese_query_hits_chinese_description(self):
        ranked = mcp_search.search(self.entries, "发邮件")
        self.assertTrue(ranked)
        self.assertEqual(ranked[0][0].name, "mcp__lark__send_mail")
        ranked = mcp_search.search(self.entries, "查一下仪表盘")
        self.assertEqual(ranked[0][0].name, "mcp__grafana__search_dashboards")

    def test_mixed_query(self):
        ranked = mcp_search.search(self.entries, "grafana 仪表盘")
        self.assertEqual(ranked[0][0].name, "mcp__grafana__search_dashboards")
        #  中文不相关 + 英文标识符命中：英文词仍起作用
        ranked = mcp_search.search(self.entries, "创建 linear issue")
        self.assertEqual(ranked[0][0].name, "mcp__linear__save_issue")

    def test_grams_are_capped(self):
        grams = mcp_search.char_grams("字" * 5000)
        self.assertEqual(len(grams), mcp_search._MAX_GRAMS)


class ToolboxSearchModeTest(unittest.TestCase):
    """经 .mcp.json + 假 server 的完整链路（检索模式=默认配置）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = Path(self.tmp.name).resolve()
        script = write_fake_server(self.workspace)
        (self.workspace / ".mcp.json").write_text(
            json.dumps(
                {"mcpServers": {"fake": {"command": sys.executable, "args": [str(script)]}}}
            ),
            encoding="utf-8",
        )
        patcher = mock.patch.object(mcp, "user_config_dir", lambda: self.workspace / "userconf")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(mcp.shutdown_all)
        self.config = Config(
            base_url="x", model="x", workspace=self.workspace, enable_plugins=False
        )

    def ready_box(self) -> Toolbox:
        box = Toolbox(self.config)
        self.assertIsNotNone(box._mcp)
        box._mcp.wait_ready(20.0)
        return box

    def test_mcp_tools_hidden_but_meta_tools_present(self):
        box = self.ready_box()
        names = [schema["function"]["name"] for schema in box.schemas()]
        self.assertIn("search_tool", names)
        self.assertIn("use_tool", names)
        self.assertFalse([name for name in names if name.startswith("mcp__")])
        #  就绪前后两次组装一致（prompt cache 纪律——这正是检索模式的卖点）
        self.assertEqual(box.schemas(), box.schemas())

    def test_search_then_use(self):
        box = self.ready_box()
        result = json.loads(box.run("search_tool", {"query": "echo"}))
        self.assertEqual(result["status"], "ready")
        self.assertGreaterEqual(result["total_hidden_tools"], 1)
        hit = result["results"][0]
        self.assertEqual(hit["tool_name"], "mcp__fake__echo")
        self.assertIn("input_schema", hit)
        output = box.run("use_tool", {"tool_name": "mcp__fake__echo", "tool_input": {"text": "hi"}})
        self.assertEqual(output, "echo: hi")

    def test_use_tool_error_paths(self):
        box = self.ready_box()
        native = box.run("use_tool", {"tool_name": "bash", "tool_input": {}})
        self.assertIn("内置工具", native)
        unqualified = box.run("use_tool", {"tool_name": "echo"})
        self.assertIn("全限定名", unqualified)
        missing = box.run("use_tool", {"tool_name": "mcp__fake__nope"})
        self.assertIn("search_tool", missing)
        bad_input = box.run(
            "use_tool", {"tool_name": "mcp__fake__echo", "tool_input": "not json"}
        )
        self.assertIn("JSON 对象", bad_input)
        #  字符串形态的 JSON 对象宽进
        ok = box.run(
            "use_tool",
            {"tool_name": "mcp__fake__echo", "tool_input": '{"text": "hi"}'},
        )
        self.assertEqual(ok, "echo: hi")

    def test_announcement_once_via_notify_hook(self):
        box = self.ready_box()
        notes: list[tuple[str, str]] = []
        box.notify_hook = lambda text, key: notes.append((text, key))
        box.schemas()
        box.schemas()
        self.assertEqual(len(notes), 1)
        text, key = notes[0]
        self.assertIn("「fake」", text)
        self.assertIn("search_tool", text)
        self.assertTrue(key.startswith("mcp-online-fake-"))


class _FakeMcpView:
    """不起进程的 MCP 视图：两个现成的远端工具。"""

    def __init__(self, calls: list[str]) -> None:
        def make(tool: str) -> mcp.RemoteTool:
            def handler(**kwargs) -> str:
                calls.append(tool)
                return f"{tool} ok"

            return mcp.RemoteTool(
                name=f"mcp__fake__{tool}", description=tool,
                parameters={"type": "object", "properties": {}},
                handler=handler, check_fn=lambda: True, server="fake", raw_name=tool,
            )

        self._tools = [make("list"), make("delete")]

    def ready_tools(self):
        return self._tools

    def loading(self) -> bool:
        return False

    def take_media(self):
        return []


class PermissionIdentityAcrossModesTest(unittest.TestCase):
    """权限认的是被调用的 MCP 工具本身，与它是直接挂进工具表还是经转发器调用无关。"""

    def run_calls(self, search_mode: bool, rules: list[str], tools: list[str], on_ask=None):
        import contextlib
        import io

        from xiaoyu.agent import Agent
        from xiaoyu.permissions import Permissions, parse_rule
        from xiaoyu.providers import Registry

        from .test_agent_paths import FakeClient, call_fragment, chunk

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name).resolve()
        config = Config(
            base_url="x", model="m", summary_model="m", explore_model="m", workspace=root,
            mode="default", auto_approve=False, mcp_tool_search=search_mode,
            enable_skills=False, enable_agents=False, enable_hooks=False, enable_plugins=False,
        )
        executed: list[str] = []
        asked: list[str] = []
        permissions = Permissions(root, [parse_rule(rule) for rule in rules])

        def approver(name, args):
            asked.append(name)
            return on_ask(permissions, name, args) if on_ask else False

        def call(index: int, tool: str) -> list:
            full = f"mcp__fake__{tool}"
            if search_mode:
                payload = ("use_tool", {"tool_name": full, "tool_input": {}})
            else:
                payload = (full, {})
            return [chunk(tool_calls=[call_fragment(0, f"c{index}", payload[0], json.dumps(payload[1]))])]

        script = [call(index, tool) for index, tool in enumerate(tools)] + [[chunk(content="完")]]
        agent = Agent(
            config, Toolbox(config, mcp_view=_FakeMcpView(executed)),
            registry=Registry.for_client(FakeClient(script)),
            approver=approver, permissions=permissions,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            agent.send("干活")
        return executed, len(asked), [item["output"] for item in agent.trace]

    def both_modes(self, *args, **kwargs):
        direct = self.run_calls(False, *args, **kwargs)
        forwarded = self.run_calls(True, *args, **kwargs)
        self.assertEqual(direct, forwarded)
        return forwarded

    def test_deny_on_one_tool_holds_in_both_modes(self):
        executed, asked, outputs = self.both_modes(
            ["deny mcp__fake__delete", "allow mcp__fake__list"], ["list", "delete"]
        )
        self.assertEqual(executed, ["list"])
        self.assertEqual(asked, 0)
        self.assertEqual(outputs[-1], "DENIED_BY_RULE")

    def test_session_grant_for_one_tool_does_not_free_the_other(self):
        """答一次「本会话允许」放行的是那一个工具：另一个照样要问。"""

        def grant_list_only(permissions, name, args):
            permissions.grant_session_call(name, args)
            return True

        executed, asked, _ = self.both_modes([], ["list", "list", "delete"], on_ask=grant_list_only)
        #  第一次 list 问、第二次免问、delete 仍然问
        self.assertEqual(asked, 2)
        self.assertEqual(executed, ["list", "list", "delete"])

    def test_hook_matcher_written_for_the_real_tool_fires_through_the_forwarder(self):
        from xiaoyu.hooks import Hook

        hook = Hook("PreToolUse", "true", matcher="^mcp__fake__delete$")
        self.assertFalse(hook.matches("use_tool"))
        self.assertEqual(
            Agent_hook_alias("use_tool", {"tool_name": "mcp__fake__delete", "tool_input": {}}),
            {"also": "mcp__fake__delete"},
        )
        self.assertEqual(Agent_hook_alias("mcp__fake__delete", {}), {})
        self.assertEqual(Agent_hook_alias("bash", {"command": "ls"}), {})


def Agent_hook_alias(name: str, args: dict) -> dict:
    from xiaoyu.agent import Agent

    return Agent._hook_alias(name, args)  # noqa: SLF001


class SearchToolWithoutMcpTest(unittest.TestCase):
    def test_meta_tools_absent_without_mcp(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Config(
                base_url="x", model="x", workspace=Path(tmp).resolve(),
                enable_plugins=False, enable_mcp=False,
            )
            box = Toolbox(config)
            names = [schema["function"]["name"] for schema in box.schemas()]
            self.assertNotIn("search_tool", names)
            self.assertNotIn("use_tool", names)


if __name__ == "__main__":
    unittest.main()
