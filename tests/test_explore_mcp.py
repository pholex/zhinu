"""检索扩展：复用连接、精确筛选、真实子 agent 审批链路。"""
import contextlib
import io
import json
from unittest import mock

from xiaoyu.agent import Agent
from xiaoyu.mcp import McpView
from xiaoyu.providers import Registry
from xiaoyu.tools import Toolbox
from .test_agent_paths import AgentTestCase, FakeClient
from .test_subagent_knobs import FakeManager, remote, tool_turn, text_turn


class ExploreMcpTest(AgentTestCase):
    def setUp(self):
        super().setUp()
        self.selected = remote('code', 'definition')
        self.other = remote('code', 'rename')
        self.selected.handler = mock.Mock(return_value='calc.py:1 def add(a, b):')
        self.other.handler = mock.Mock(return_value='changed')
        self.manager = FakeManager([self.selected, self.other])
        self.config.explore_mcp_tools = [self.selected.name]
        self.config.enable_mcp = False

    def run_child(self, script, approver=None, permissions=None):
        client = FakeClient(script)
        parent = Toolbox(self.config, mcp_view=McpView(self.manager))
        agent = Agent(self.config, parent, registry=Registry.for_client(client), approver=approver, permissions=permissions)
        with mock.patch('xiaoyu.mcp.launch', side_effect=AssertionError('不应新建连接')), contextlib.redirect_stdout(io.StringIO()):
            result = parent.get('explore').handler(question='查 add 的定义')
        return result, client

    def test_selected_tool_works_in_both_modes(self):
        for search in (True, False):
            with self.subTest(search=search):
                self.config.mcp_tool_search = search
                name = 'use_tool' if search else self.selected.name
                args = {'tool_name': self.selected.name, 'tool_input': {}} if search else {}
                result, client = self.run_child([tool_turn('a', name, args), text_turn('定位完成')])
                self.assertIn('定位完成', result)
                self.assertIn('calc.py:1', json.dumps(client.completions.calls[-1]['messages']))
        self.assertEqual(self.selected.handler.call_count, 2)

    def test_hidden_tool_cannot_be_called_via_dispatch(self):
        self.run_child([tool_turn('a', 'use_tool', {'tool_name': self.other.name}), text_turn('退回文件检索')])
        self.other.handler.assert_not_called()

    def test_explicit_selection_does_not_grant_approval(self):
        self.config.auto_approve = False
        approve = mock.Mock(return_value=False)
        self.run_child([tool_turn('a', 'use_tool', {'tool_name': self.selected.name}), text_turn('已拒绝')], approve)
        approve.assert_called_once()
        self.selected.handler.assert_not_called()

    def test_plan_mode_cannot_bypass_via_explore(self):
        self.config.mode = 'plan'
        self.run_child([tool_turn('a', 'use_tool', {'tool_name': self.selected.name}), text_turn('退回文件检索')])
        self.selected.handler.assert_not_called()

    def test_empty_selection_preserves_default_tools(self):
        self.config.explore_mcp_tools = []
        _, client = self.run_child([text_turn('完成')])
        names = {t['function']['name'] for t in client.completions.calls[0]['tools']}
        self.assertEqual(names, set(Toolbox.READONLY) | {"get_context_remaining", "wait_for_event"})

    def test_view_preserves_parent_restriction_and_tracks_changes(self):
        parent = McpView(self.manager, mode='named', names=('other',))
        self.assertEqual(McpView(parent, tool_names=(self.selected.name,)).ready_tools(), [])
        view = McpView(self.manager, tool_names=(self.selected.name,))
        self.assertEqual(view.ready_tools(), [self.selected])
        self.manager._remotes = [self.other]
        self.assertEqual(view.ready_tools(), [])
        self.assertEqual(McpView(self.manager, tool_names=('mcp__code__*',)).ready_tools(), [])

    def test_parent_deny_survives_auto_approval(self):
        from xiaoyu.permissions import Permissions, parse_rule
        permissions = Permissions(self.root, [parse_rule("deny " + self.selected.name)])
        self.run_child([tool_turn('a', 'use_tool', {'tool_name': self.selected.name}), text_turn('被拒绝')], permissions=permissions)
        self.selected.handler.assert_not_called()

    def test_env_names_are_trimmed_and_empty_items_ignored(self):
        import os
        from xiaoyu.config import Config
        with mock.patch.dict(os.environ, {"XIAOYU_EXPLORE_MCP_TOOLS": " mcp__code__definition, ,mcp__code__hover "}):
            config = Config.from_env(workspace=self.root)
        self.assertEqual(config.explore_mcp_tools, ["mcp__code__definition", "mcp__code__hover"])
