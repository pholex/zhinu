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
