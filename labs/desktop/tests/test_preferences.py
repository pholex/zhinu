import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from labs.desktop.preferences import DEFAULTS, PreferenceStore, PreferenceConflict, validate_values
from labs.desktop.preferences_api import install_preferences_routes
from tests.test_serve import ServeCase


class PreferencesTests(unittest.TestCase):
    def test_legacy_send_key_migrates_once_and_later_explicit_choice_survives(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'prefs.json'
            legacy = copy.deepcopy(DEFAULTS)
            legacy['ui'].pop('send_key_version', None)
            legacy['ui']['send_key'] = 'mod-enter'
            legacy['ui']['theme'] = 'dark'
            raw = json.dumps({'revision': 3, 'values': legacy}).encode()
            path.write_bytes(raw)
            store = PreferenceStore(path)
            migrated = store.read()
            self.assertEqual(migrated['values']['ui']['send_key'], 'enter')
            self.assertNotIn('send_key_version', migrated['values']['ui'])
            self.assertEqual(migrated['values']['ui']['theme'], 'dark')
            self.assertEqual(path.read_bytes(), raw)
            store.save(2, migrated['values'])
            self.assertFalse(store.keyboard_migration.exists())
            self.assertEqual(store.read()['values']['ui']['send_key'], 'enter')
            saved = store.save(3, migrated['values'])
            self.assertTrue(store.keyboard_migration.is_file())
            explicit = copy.deepcopy(saved['values'])
            explicit['ui']['send_key'] = 'mod-enter'
            store.save(saved['revision'], explicit)
            self.assertEqual(store.read()['values']['ui']['send_key'], 'mod-enter')


    def test_strict_values_cas_retry_and_loss_before_replace(self):
        with tempfile.TemporaryDirectory() as d:
            held = True
            def guard():
                if not held:
                    raise OSError('lost')
            store = PreferenceStore(Path(d)/'prefs.json', write_guard=guard)
            values = copy.deepcopy(DEFAULTS)
            values['ui']['session_meta'] = {'s': {'group': '工程', 'read_seq': 4}}
            first = store.save(0, values)
            self.assertEqual(store.save(0, values), first)
            changed = copy.deepcopy(values); changed['ui']['theme'] = 'dark'
            with self.assertRaises(PreferenceConflict):
                store.save(0, changed)
            held = False
            with self.assertRaises(OSError):
                store.save(1, changed)
            self.assertEqual(store.read(), first)
            held = True
            original = os.fsync
            def lose(fd):
                nonlocal held
                original(fd); held = False
            with patch('os.fsync', lose), self.assertRaises(OSError):
                store.save(1, changed)
            self.assertEqual(store.read(), first)
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_no_arbitrary_permission_or_invalid_metadata(self):
        for field, value in [('default_mode', 'allow_all'), ('timezone', '../UTC')]:
            data = copy.deepcopy(DEFAULTS); data[field] = value
            with self.assertRaises(ValueError): validate_values(data)
        for field, value in [('font_size', True), ('nav_order', ['projects']), ('session_meta', {'x': {'group': 'g', 'read_seq': -1}})]:
            data = copy.deepcopy(DEFAULTS); data['ui'][field] = value
            with self.assertRaises(ValueError): validate_values(data)

    def test_api_ownership_auth_source_and_no_live_changes(self):
        import httpx
        from fastapi import FastAPI
        with tempfile.TemporaryDirectory() as d:
            app = FastAPI(); app.state.execution_owner = SimpleNamespace(held=True)
            app.state.sessions = {}; app.state.serve_config = SimpleNamespace(model='scripted', mode='default', base_url=None)
            store = install_preferences_routes(app, Path(d)/'prefs', 'secret', root=Path(d))
            config = SimpleNamespace(model='scripted', mode='default')
            registry = SimpleNamespace(listing=lambda: [], resolve=lambda name: name)
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
                    self.assertEqual((await client.get('/desktop/preferences')).status_code, 401)
                    client.headers['Authorization'] = 'Bearer secret'
                    first = (await client.get('/desktop/preferences')).json()
                    self.assertEqual(first['effective']['model_source'], 'service_override')
                    changed = copy.deepcopy(first['values']); changed['default_mode']='plan'
                    result = await client.post('/desktop/preferences', json={'revision':0,'values':changed})
                    self.assertEqual(result.status_code,200,result.text)
                    self.assertEqual(result.json()['scope'],'new_sessions')
                    self.assertEqual(config.mode,'default')
                    app.state.execution_owner.held=False
                    changed['default_mode']='auto'
                    self.assertEqual((await client.post('/desktop/preferences',json={'revision':1,'values':changed})).status_code,503)
                    self.assertEqual((await client.get('/desktop/preferences')).json()['values']['default_mode'],'plan')
            with patch('xiaoyu.config.Config.from_env', return_value=config), patch('xiaoyu.providers.build', return_value=registry):
                asyncio.run(check())


class PreferencesServeTests(ServeCase):
    def test_real_tool_history_is_inline_without_changing_plain_history(self):
        self.token='t'
        (self.root/'version.txt').write_text('version 1.2.3')
        c=self.start('text: Checking\n'+'tool_call: '+json.dumps({'name':'read_file','arguments':{'path':'version.txt'}})+'\n---\ntext: Version 1.2.3\n')
        sid=self.new_session()
        result=c.post(f'/session/{sid}/prompt',json={'text':'check version'},headers=self.headers())
        self.assertEqual(result.status_code,200,result.text)
        plain=c.get(f'/session/{sid}/history',headers=self.headers()).json()
        self.assertTrue(all(m['role'] in ('user','assistant') for m in plain['messages']))
        expanded=c.get(f'/session/{sid}/history?include_activity=true',headers=self.headers()).json()
        tools=[m for m in expanded['messages'] if m['role']=='tool']
        self.assertEqual(len(tools),1)
        self.assertEqual(tools[0]['name'],'read_file')
        self.assertEqual(tools[0]['args']['path'],'version.txt')
        self.assertIn('1.2.3',tools[0]['content'])
        self.assertTrue(tools[0]['ok'])
        self.assertEqual(expanded['messages'][-1]['role'],'assistant')
        ids=[]
        for cursor in range(expanded['total']):
            page=c.get(f'/session/{sid}/history?include_activity=true&cursor={cursor}&limit=1',headers=self.headers()).json()
            ids.append(page['messages'][0]['id'])
        self.assertEqual(ids,[m['id'] for m in expanded['messages']])


    def test_model_selection_is_durable_cas_and_applies_only_on_next_turn(self):
        self.token = 't'
        state=self.root/'model-state'
        c=self.start('text: first\n---\ntext: second\n',state_dir=state)
        install_preferences_routes(c.app,self.root/'prefs','t',root=self.root)
        sid=self.new_session();session=c.app.state.sessions[sid]
        route=f'/desktop/sessions/{sid}/model'
        headers={**self.headers(),'X-Xiaoyu-Instance':c.app.state.instance_id}
        old=session.agent.config.model
        self.assertEqual(c.post(route,json={'model':'second-model','previous':old}).status_code,401)
        with patch('xiaoyu.serve_state.SessionStore.save',side_effect=OSError('disk')):
            self.assertEqual(c.post(route,json={'model':'second-model','previous':old},headers=headers).status_code,503)
        self.assertFalse(getattr(session,'next_model',''))
        session.busy=True
        changed=c.post(route,json={'model':'second-model','previous':old},headers=headers)
        self.assertEqual(changed.status_code,200,changed.text)
        self.assertEqual(session.agent.config.model,old)
        self.assertEqual(session.next_model,'second-model')
        self.assertEqual(c.post(route,json={'model':'third','previous':old},headers=headers).status_code,409)
        self.assertEqual(c.post(route,json={'model':'second-model','previous':old},headers=headers).status_code,200)
        session.busy=False
        result=c.post(f'/session/{sid}/prompt',json={'text':'next'},headers=headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(session.agent.config.model,'second-model')
        self.assertFalse(session.next_model)
        self.assertEqual(session.agent_config['model'],'second-model')
        history=c.get(f'/session/{sid}/history?include_activity=true',headers=headers).json()['messages']
        self.assertEqual(history[0]['role'],'model')
        self.assertEqual(history[0]['model'],'second-model')
        self.assertEqual(session.runs[-1]['model'],'second-model')

        c.__exit__(None,None,None)
        c=self.start('text: restored\n',state_dir=state)
        install_preferences_routes(c.app,self.root/'prefs','t',root=self.root)
        restored=c.app.state.sessions[sid]
        self.assertEqual(restored.agent.config.model,'second-model')
        headers={**self.headers(),'X-Xiaoyu-Instance':c.app.state.instance_id}
        self.assertEqual(c.post(route,json={'model':'third-model','previous':'second-model'},headers=headers).status_code,200)
        c.__exit__(None,None,None)
        c=self.start('text: after restart\n',state_dir=state)
        restored=c.app.state.sessions[sid]
        self.assertEqual(restored.next_model,'third-model')
        self.assertEqual(restored.agent.config.model,'second-model')
        self.assertEqual(c.post(f'/session/{sid}/prompt',json={'text':'resume'},headers=self.headers()).status_code,200)
        self.assertEqual(restored.agent.config.model,'third-model')

    def test_rename_persists_export_excludes_system_and_fork_is_separate(self):
        self.token = 't'
        c = self.start('text: original answer\n')
        app = c.app
        install_preferences_routes(app, self.root/'prefs.json', 't', root=self.root)
        sid=self.new_session()
        result=c.post(f'/session/{sid}/prompt', json={'text':'original question'}, headers=self.headers())
        self.assertEqual(result.status_code,200,result.text)
        renamed=c.post(f'/desktop/sessions/{sid}/title', json={'title':'我的项目'},headers=self.headers())
        self.assertEqual(renamed.status_code,200,renamed.text)
        session=app.state.sessions[sid]
        self.assertEqual(session.title,'我的项目')
        export=c.get(f'/desktop/sessions/{sid}/export?format=json',headers=self.headers()).json()
        decoded=json.loads(export['content'])
        self.assertEqual([m['role'] for m in decoded['messages']],['user','assistant'])
        self.assertEqual(decoded['title'],'我的项目')
        source_before=list(session.agent.messages)
        fork=self.new_session(fork_from={'session_id':sid})
        self.assertNotEqual(fork,sid)
        self.assertEqual(session.agent.messages,source_before)
        self.assertNotEqual(app.state.sessions[fork].agent.session_log.path,session.agent.session_log.path)
        session.agent.session_log.release()
        self.assertEqual(c.post(f'/desktop/sessions/{sid}/title',json={'title':'禁止写'},headers=self.headers()).status_code,503)

    def test_title_disk_failure_and_restart_restore(self):
        state=self.root/'service-state';self.token='t'
        c=self.start('text: done\n',state_dir=state)
        install_preferences_routes(c.app,self.root/'prefs','t',root=self.root)
        sid=self.new_session()
        route=f'/desktop/sessions/{sid}/title'
        old=c.app.state.sessions[sid].title
        with patch('xiaoyu.serve_state.SessionStore.save',side_effect=OSError('disk')):
            self.assertEqual(c.post(route,json={'title':'failed title'},headers=self.headers()).status_code,503)
        self.assertEqual(c.app.state.sessions[sid].title,old)
        self.assertEqual(c.post(route,json={'title':'持久标题'},headers=self.headers()).status_code,200)
        c.__exit__(None,None,None)
        c2=self.start('text: never\n',state_dir=state)
        self.assertEqual(c2.app.state.sessions[sid].title,'持久标题')

    def test_skill_inventory_and_reload_uses_existing_agent_trust_and_idle_gate(self):
        from xiaoyu.skills import Skill
        self.token='t';c=self.start('text: done\n')
        install_preferences_routes(c.app,self.root/'prefs','t',root=self.root)
        sid=self.new_session();session=c.app.state.sessions[sid]
        skill=Skill('test-skill','safe fixture',self.root/'skill.md')
        session.agent.skills=[skill]
        data=c.get('/desktop/capabilities?session_id='+sid,headers=self.headers()).json()
        self.assertEqual(data['scope'],'session_loaded')
        self.assertEqual(data['skills'][0]['id'],'test-skill')
        self.assertNotIn(str(skill.path),json.dumps(data))
        route=f'/desktop/sessions/{sid}/skills/reload'
        self.assertEqual(c.post(route,json={},headers=self.headers()).status_code,409)
        headers={**self.headers(),'X-Xiaoyu-Instance':c.app.state.instance_id}
        # The existing disabled core configuration remains authoritative on reload.
        result=c.post(route,json={},headers=headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertEqual(result.json()['removed'],['test-skill'])
        self.assertFalse(session.agent.skills)
        session.management_busy=True
        self.assertEqual(c.post(route,json={},headers=headers).status_code,409)
        session.management_busy=False


class ShortcutPreferenceTests(unittest.TestCase):
    def test_legacy_preferences_gain_defaults_without_rewriting_file(self):
        from labs.desktop.preferences import SHORTCUTS
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'preferences.json'
            values=copy.deepcopy(DEFAULTS);values['ui'].pop('shortcuts')
            raw=json.dumps({'revision':9,'values':values},ensure_ascii=False).encode('utf-8');path.write_bytes(raw)
            store=PreferenceStore(path,write_guard=lambda: (_ for _ in ()).throw(AssertionError('read must not write')))
            state=store.read();self.assertEqual(state['revision'],9)
            self.assertEqual(state['values']['ui']['shortcuts'],SHORTCUTS)
            self.assertEqual(path.read_bytes(),raw)
            self.assertNotIn('shortcuts',values['ui'])

    def test_conflicts_reserved_and_unknown_actions_rejected_normalization_and_disable_allowed(self):
        from labs.desktop.preferences import SHORTCUTS,validate_shortcuts
        self.assertEqual(validate_shortcuts({**SHORTCUTS,'search':' Mod+Shift+J '})['search'],'mod+shift+j')
        self.assertEqual(validate_shortcuts({**SHORTCUTS,'search':'','settings':''})['search'],'')
        for changed in ({'search':'MOD+N'},{'search':'mod+enter'},{'search':'mod+q'},{'search':'mod+shift+c'},
                        {'search':'n'},{'search':'mod+alt+j'},{'unknown':'mod+j'},{'search':[]}):
            with self.subTest(changed=changed),self.assertRaises(ValueError):
                validate_shortcuts({**SHORTCUTS,**changed})

    def test_web_defaults_match_persistent_schema(self):
        import re
        from labs.desktop.preferences import SHORTCUTS
        source=(Path(__file__).parents[1]/'web'/'shortcuts.mjs').read_text(encoding='utf-8',errors='replace')
        match=re.search(r'export const DEFAULT_SHORTCUTS = (\{[^\n]+\});',source)
        self.assertIsNotNone(match)
        self.assertEqual(json.loads(match.group(1)),SHORTCUTS)
