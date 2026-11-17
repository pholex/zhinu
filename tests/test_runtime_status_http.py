"""桌面与 CLI 共用的会话查询通路：真实 HTTP、脚本模型、不联网。"""
import json
from .test_serve import ServeCase


class RuntimeStatusHTTPTest(ServeCase):
    mcp = False

    def test_query_finishes_in_ask_mode_without_permission_request(self):
        self.token = 't'
        c = self.start('tool_call: {"name":"get_runtime_status","arguments":{"section":"mcp"}}\n---\ntext: 查询完成\n')
        sid = self.new_session()
        result = c.post(f'/session/{sid}/prompt', json={'text':'当前加载了哪些 MCP？'}, headers=self.headers())
        self.assertEqual(result.status_code, 200, result.text)
        events = self.events(sid, limit=2000)
        self.assertFalse(any('permission' in e['kind'] or 'approval' in e['kind'] for e in events))
        history = c.get(f'/session/{sid}/history?include_activity=true', headers=self.headers()).json()
        tools = [m for m in history['messages'] if m['role'] == 'tool']
        self.assertEqual([m['name'] for m in tools], ['get_runtime_status'])
        self.assertTrue(tools[0]['ok'])
        self.assertIn('"mcp"', tools[0]['content'])
        self.assertEqual(history['messages'][-1]['content'], '查询完成')
