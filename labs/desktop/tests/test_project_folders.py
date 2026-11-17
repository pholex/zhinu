"""Project identities and folder scopes through real desktop and execution APIs."""
from pathlib import Path
import json
from unittest.mock import patch

from tests.test_serve import ServeCase
from labs.desktop.workspace import WorkspaceState, ProjectResolver, install_workspace_routes, project_id, project_state_path


class ProjectFoldersTests(ServeCase):
    mcp = False
    token = 'project-test-owner'

    def boot(self, script='text: ready\n'):
        self.state = Path(self.tmp) / 'desktop'
        resolver = ProjectResolver(self.state / 'workspace.json', self.root)
        self.start(script, state_dir=self.state, project_resolver=resolver, persist_empty_sessions=False)
        install_workspace_routes(self.client.app, self.state / 'workspace.json', self.token, self.root)
        self.headers = {'Authorization': 'Bearer ' + self.token}

    def get(self, path):
        return self.client.get(path, headers=self.headers)

    def post(self, path, body, **kwargs):
        return self.client.post(path, json=body, headers={**self.headers, **kwargs})

    def save_project(self, pid, name, folders, primary):
        state = self.get('/desktop/workspace').json()['state']
        response = self.post('/desktop/workspace', {'revision': state['revision'], 'group': 'projects',
            'key': pid, 'value': {'name': name, 'folders': folders, 'primary': primary, 'context': '说明'}})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_scheduled_project_follows_new_primary_through_real_session_api(self):
        import asyncio
        from labs.desktop.automation import AutomationStore, AutomationRunner
        self.boot()
        pid = 'c'*24
        old, new = self.root/'old', self.root/'new'
        old.mkdir(); new.mkdir()
        self.save_project(pid, 'project', [str(old)], str(old))
        store = AutomationStore(self.state/'automation.json')
        task, state = store.configure(0, '', {'name':'task','project_id':pid,'workspace':str(old),
            'model':'scripted','prompt':'work','schedule':{'kind':'interval','seconds':60}}, now=100)
        store.enable(state['revision'], task, True, now=100)
        self.save_project(pid, 'project', [str(new)], str(new))
        async def request(method, path, body=None, **kwargs):
            response = self.post(path, body, **{'Idempotency-Key':kwargs['key']})
            response.raise_for_status()
            return response.json()
        asyncio.run(AutomationRunner(store, request).tick(now=160))
        state = store.read()
        run, = state['runs'].values()
        self.assertEqual(run['state'], 'running')
        self.assertEqual(self.client.app.state.sessions[run['session_id']].workspace, new.resolve())
        self.assertTrue(state['tasks'][task]['enabled'])

    def test_empty_project_no_conversation_until_send_and_project_identity_survives_primary_change(self):
        self.boot()
        pid = 'a' * 24
        self.save_project(pid, '研究项目', [], '')
        self.assertEqual(self.get('/session').json()['sessions'], [])
        first = self.post('/session', {'project_id': pid}).json()
        sid = first['session_id']
        self.assertEqual(first['project_id'], pid)
        self.assertEqual(first['project_folders'], [])
        self.assertTrue(Path(first['workspace']).is_relative_to(self.state / 'project-workspaces'))
        self.assertFalse((self.state / 'sessions' / (sid + '.json')).exists())
        self.assertEqual(self.post(f'/session/{sid}/prompt', {'text':'开始'}).status_code, 200)
        other = Path(self.tmp) / '资料'
        other.mkdir()
        self.save_project(pid, '改名后的项目', [str(other)], str(other))
        second = self.post('/session', {'project_id': pid}).json()
        self.assertEqual(second['workspace'], str(other))
        self.assertNotEqual(second['workspace'], first['workspace'])
        self.assertEqual(self.get('/desktop/workspace').json()['catalogue']['sessions'][sid], pid)
        # Existing chats keep their working directory across service restarts.
        self.client.__exit__(None, None, None)
        self.boot('error: restore must not run the model\n')
        resumed = self.get('/session').json()['sessions']
        self.assertEqual(len(resumed), 1)
        self.assertEqual(resumed[0]['session_id'], sid)
        self.assertEqual(resumed[0]['workspace'], first['workspace'])
        self.assertEqual(resumed[0]['project_id'], pid)

    def test_multiple_folders_read_write_and_sandbox_are_session_scoped(self):
        secondary = Path(self.tmp) / 'docs'
        secondary.mkdir()
        (secondary / 'input.txt').write_text('reference', encoding='utf-8')
        (secondary / 'AGENTS.md').write_text('SECONDARY_INSTRUCTIONS_MUST_NOT_AUTOLOAD', encoding='utf-8')
        script = 'tool_call: ' + json.dumps({'name':'read_file','arguments':{'path':str(secondary / 'input.txt')}}) + '\n---\n'
        script += 'tool_call: ' + json.dumps({'name':'write_file','arguments':{'path':str(secondary / 'result.txt'),'content':'done'}}) + '\n---\ntext: done\n'
        self.boot(script)
        pid = project_id(str(self.root))
        self.save_project(pid, '应用与资料', [str(self.root), str(secondary)], str(self.root))
        response = self.post('/session', {'project_id':pid, 'mode':'auto'})
        self.assertEqual(response.status_code, 200, response.text)
        sid = response.json()['session_id']
        result = self.post(f'/session/{sid}/prompt', {'text':'整理'})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual((secondary / 'result.txt').read_text(), 'done')
        agent = self.client.app.state.sessions[sid].agent
        self.assertFalse(agent.toolbox.outside_workspace({'path':str(secondary / 'result.txt')}))
        self.assertTrue(agent.toolbox.outside_workspace({'path':str(Path(self.tmp) / 'elsewhere.txt')}))
        self.assertIsNotNone(agent.toolbox.guarded_config('write_file', {'path':str(secondary / '.env')}))
        self.assertIn(str(secondary), agent.messages[0]['content'])
        self.assertNotIn('SECONDARY_INSTRUCTIONS_MUST_NOT_AUTOLOAD', agent.messages[0]['content'])
        # Linked paths outside attached folders remain outside the scope.
        (secondary / 'escape').symlink_to(Path(self.tmp))
        self.assertTrue(agent.toolbox.outside_workspace({'path':str(secondary / 'escape' / 'elsewhere.txt')}))
        ordinary = self.post('/session', {}).json()['session_id']
        self.assertTrue(self.client.app.state.sessions[ordinary].agent.toolbox.outside_workspace({'path':str(secondary / 'result.txt')}))
        from xiaoyu import sandbox
        with patch.object(sandbox, 'available', return_value=True), patch.object(sandbox.sys, 'platform', 'darwin'):
            wrapped = sandbox.wrap(['sh','-c','true'], self.root, additional_workspaces=(secondary,))
        self.assertTrue(any(v.endswith('=' + str(secondary)) for v in wrapped))
        self.assertIn(str(secondary / '.xiaoyu'), ' '.join(wrapped))

    def test_project_membership_is_not_inferred_from_shared_folder_and_fork_keeps_identity(self):
        self.boot()
        for pid in ['a'*24, 'b'*24]:
            self.save_project(pid, pid[:1], [str(self.root)], str(self.root))
        one = self.post('/session', {'project_id':'a'*24}).json()['session_id']
        two = self.post('/session', {'project_id':'b'*24}).json()['session_id']
        fork = self.post('/session', {'fork_from':one}).json()
        self.assertEqual(fork['project_id'], 'a'*24)
        self.assertEqual(self.post('/session', {'fork_from':one, 'workspace':str(Path(self.tmp))}).status_code, 400)
        mapping = self.get('/desktop/workspace').json()['catalogue']['sessions']
        self.assertEqual(mapping[one], 'a'*24)
        self.assertEqual(mapping[two], 'b'*24)
        self.assertEqual(mapping[fork['session_id']], 'a'*24)

    def test_legacy_context_preserved_primary_validation_and_scope_not_client_controlled(self):
        self.boot()
        pid = project_id(str(self.root))
        saved = self.save_project(pid, '旧项目', [str(self.root)], str(self.root))
        reply = self.post('/desktop/workspace', {'revision':saved['state']['revision'], 'group':'projects',
                    'key':pid, 'value':{'context':'新的说明'}})
        self.assertEqual(reply.json()['state']['projects'][pid]['name'], '旧项目')
        response = self.post('/session', {'project_id':pid, 'workspace':str(Path(self.tmp))})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.post('/session', {'project_id':'f'*24}).status_code, 400)
        self.assertEqual(self.post('/session', {'workspace':str(Path(self.tmp))}).status_code, 400)
        state = reply.json()['state']
        bad = self.post('/desktop/workspace', {'revision':state['revision'],'group':'projects','key':pid,
                    'value':{'name':'invalid','context':'','folders':[str(self.root)],'primary':str(Path(self.tmp))}})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.get('/desktop/workspace').json()['state'],state)

    def test_metadata_upgrade_keeps_legacy_file_for_rollback(self):
        directory = Path(self.tmp) / 'migration'
        legacy = WorkspaceState(directory / 'workspace.json')
        pid = project_id(str(self.root))
        old = legacy.update(0, 'projects', pid, {'context':'旧说明'})
        original = legacy.path.read_bytes()
        target = project_state_path(directory)
        upgraded = WorkspaceState(target)
        self.assertEqual(upgraded.read(), old)
        new = upgraded.update(old['revision'], 'projects', pid,
            {'name':'独立项目', 'folders':[], 'primary':'', 'context':'旧说明'})
        self.assertEqual(project_state_path(directory), target)
        self.assertEqual(upgraded.read(), new)
        self.assertEqual(legacy.path.read_bytes(), original)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_files_browse_attached_roots_and_empty_project_never_browses_cwd(self):
        from labs.desktop.content_api import install_content_routes
        self.boot()
        install_content_routes(self.client.app, self.state / 'content.json', self.token, self.root,
                               workspace_path=self.state / 'workspace.json')
        other = Path(self.tmp) / 'references'
        other.mkdir()
        (other / 'note.txt').write_text('reference', encoding='utf-8')
        pid = 'a'*24
        self.save_project(pid, '资料', [str(self.root), str(other)], str(self.root))
        response = self.client.get('/desktop/content/files', headers=self.headers,
                                  params={'project_id':pid, 'folder':str(other)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['folder'], str(other))
        self.assertIn('note.txt', str(response.json()['entries']))
        denied = self.client.get('/desktop/content/files', headers=self.headers,
                                  params={'project_id':pid, 'folder':str(Path(self.tmp))})
        self.assertEqual(denied.status_code, 403)
        self.save_project(pid, '资料', [], '')
        empty = self.client.get('/desktop/content/files', headers=self.headers, params={'project_id':pid})
        self.assertEqual(empty.status_code, 200, empty.text)
        self.assertEqual(empty.json()['entries'], [])
        self.assertEqual(self.client.get('/desktop/content/file', headers=self.headers,
            params={'project_id':pid,'path':'pyproject.toml'}).status_code, 404)

    def test_automation_stores_project_identity_and_primary_outside_service_root(self):
        from labs.desktop.automation_api import install_automation_routes
        self.boot()
        install_automation_routes(self.client.app, self.state / 'automation.json', self.token, self.root, 'http://local')
        other = Path(self.tmp) / 'automation-project'
        other.mkdir()
        pid = 'a'*24
        self.save_project(pid, '定时整理', [str(other)], str(other))
        value = {'name':'检查','workspace':'','project_id':pid,'model':'scripted','prompt':'整理结果',
                 'schedule':{'kind':'interval','seconds':60}}
        reply = self.post('/desktop/automations/configure',
            {'revision':0,'task_id':'stable-project-task','definition':value,'create':True})
        self.assertEqual(reply.status_code,200,reply.text)
        self.assertIn(str(other), reply.text)
        self.assertIn(pid,reply.text)
