"""运行态查询必须只读内存，并遵循会话能力范围与显式权限规则。"""
import contextlib
import io
import json
import threading
from types import SimpleNamespace
from unittest import mock

from .test_agent_paths import AgentTestCase, call_fragment, chunk, usage_chunk
from xiaoyu.mcp import McpManager, McpView, ServerSpec
from xiaoyu.skills import Skill
from xiaoyu.tools import Tool


def manager():
    # 不启动进程、不加载用户配置；这里只构造真实管理器的内存状态。
    m = object.__new__(McpManager)
    m._lock = threading.Lock()
    m._closed = False
    m._specs = [ServerSpec(name=n, command='private-command', env={'TOKEN': 'private-token'},
                          url='https://private-url', disabled=n == 'disabled')
                for n in ('ready', 'cached', 'failed', 'loading', 'disabled')]
    m._states = {'ready': 'ready', 'cached': 'cached', 'failed': 'failed: private-token',
                 'loading': 'loading', 'disabled': 'loading'}
    m._registered = {'ready': {'lookup'}, 'cached': {'search'}}
    m._quarantined = {'ready': ['changed']}
    return m


class RuntimeStatusTest(AgentTestCase):
    def query(self, agent, **args):
        return json.loads(agent.toolbox.get('get_runtime_status').handler(**args))

    def test_manager_status_is_cached_metadata_and_view_scoped(self):
        m = manager()
        result = m.runtime_inventory()
        states = {s['name']: s for s in result['servers']}
        self.assertEqual(states['cached']['status'], 'cached')
        self.assertEqual(states['disabled']['status'], 'disabled')
        self.assertEqual(states['failed']['status'], 'failed')
        self.assertEqual(states['ready']['quarantined_tool_count'], 1)
        self.assertNotIn('private-', json.dumps(result))
        view = McpView(m, 'named', ('cached',))
        scoped = view.runtime_inventory()
        self.assertEqual([s['name'] for s in scoped['servers']], ['cached'])
        self.assertEqual([t['name'] for t in scoped['tools']], ['mcp__cached__search'])
        m._closed = True
        self.assertEqual(m.runtime_inventory()['servers'][0]['status'], 'closed')

    def test_query_no_io_or_availability_probes_and_no_secrets(self):
        agent = self.build([])
        agent.skills = [Skill('demo', 'private-description', self.root / 'secret.md')]
        agent._loaded_skills = {'demo'}
        probe = mock.Mock(side_effect=AssertionError('must not probe'))
        agent.toolbox.register(Tool('probe', 'private-description', {}, lambda: '', check_fn=probe))
        agent.toolbox._mcp = McpView(manager(), 'named', ('cached',))
        # 直接取已注册 handler，避免测试本身通过 get() 同步 MCP。
        handler = agent.toolbox._tools['get_runtime_status'].handler
        agent._step = SimpleNamespace(schemas=({'function': {'name': 'get_runtime_status'}},))
        with (mock.patch('builtins.open', side_effect=AssertionError('file IO')),
              mock.patch('pathlib.Path.open', side_effect=AssertionError('path IO')),
              mock.patch('subprocess.Popen', side_effect=AssertionError('process')),
              mock.patch('socket.socket', side_effect=AssertionError('network')),
              mock.patch.object(agent.toolbox, '_absorb_mcp', side_effect=AssertionError('sync'))):
            result = json.loads(handler())
        probe.assert_not_called()
        self.assertEqual(result['skills']['items'], [{'name': 'demo', 'plugin': None, 'body_loaded': True}])
        self.assertEqual(result['model_visible_tools']['items'], ['get_runtime_status'])
        self.assertEqual(result['mcp']['items'][0]['name'], 'cached')
        self.assertNotIn('private-', json.dumps(result))
        self.assertNotIn('secret.md', json.dumps(result))

    def test_model_inventory_does_not_initialize_provider_clients(self):
        from xiaoyu.providers import Provider, Registry
        agent = self.build([])
        agent.registry = Registry([
            Provider(name='first', base_url='https://private-url', api_key='private-token', models=('a', 'b')),
            Provider(name='second', base_url='https://private-url', api_key='private-token', models=('b', 'c')),
        ])
        with mock.patch.object(agent.registry, '_build_client', side_effect=AssertionError('client initialization')):
            result = self.query(agent, section='models')
        self.assertEqual(result['models']['items'], ['a', 'b', 'c'])
        self.assertEqual(agent.registry._clients, {})
        self.assertNotIn('private-', json.dumps(result))

    def test_pagination_validation_and_updated_model(self):
        agent = self.build([])
        result = self.query(agent, section='tools', limit=2)
        self.assertEqual(len(result['tools']['items']), 2)
        self.assertEqual(result['tools']['next_offset'], 2)
        self.assertGreater(result['tools']['total'], 2)
        agent.config.model = 'changed-model'
        self.assertEqual(self.query(agent, section='models')['current_model'], 'changed-model')
        agent.adopt_mode('auto')
        self.assertEqual(self.query(agent, section='workspace')['permission_mode'], 'auto')
        handler = agent.toolbox.get('get_runtime_status').handler
        for args in ({'section':'config'}, {'limit':True}, {'limit':101}, {'offset':-1}, {'path':'/tmp'}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                handler(**args)

    def test_default_and_plan_do_not_ask_but_explicit_deny_is_respected(self):
        from xiaoyu.permissions import parse_rule
        for mode, denied in [('default', False), ('plan', False), ('default', True)]:
            with self.subTest(mode=mode, denied=denied):
                self.config.auto_approve = False
                self.config.mode = mode
                approver = mock.Mock(return_value=False)
                agent = self.build([
                    [chunk(tool_calls=[call_fragment(0, 'status', 'get_runtime_status', '{"section":"mcp"}')]), usage_chunk(10, 5)],
                    [chunk('查询完成'), usage_chunk(20, 5)],
                ], approver=approver)
                if denied:
                    agent.permissions.rules.append(parse_rule('deny get_runtime_status'))
                with contextlib.redirect_stdout(io.StringIO()):
                    agent.send('当前加载了哪些 MCP？')
                approver.assert_not_called()
                outputs = [m['content'] for m in agent.messages if m['role'] == 'tool']
                self.assertEqual(len(outputs), 1)
                if denied:
                    self.assertIn('deny get_runtime_status', outputs[0])
                    self.assertEqual(agent.trace[0]['output'], 'DENIED_BY_RULE')
                else:
                    self.assertIn('"mcp"', outputs[0])
                    self.assertNotIn('ERROR', outputs[0])
