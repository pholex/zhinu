"""Desktop startup discovery uses the core index without creating a model run."""
from contextlib import contextmanager
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tests.test_serve import _isolated_env
from labs.desktop.preferences_api import install_preferences_routes


class DesktopSkillDiscoveryTests(unittest.TestCase):
    @contextmanager
    def fixture(self, *, enabled=None, override=False):
        from fastapi.testclient import TestClient
        from xiaoyu.serve import ServeConfig, create_app
        from xiaoyu.config import user_config_dir
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp).resolve(); root = base / 'workspace'; root.mkdir()
            script = base / 'script.txt'; script.write_text('text: unused\n', encoding='utf-8')
            env = _isolated_env(str(base), script)
            env.pop('XIAOYU_ENABLE_SKILLS')
            if enabled is not None:
                env['XIAOYU_ENABLE_SKILLS'] = enabled
            env['XIAOYU_SKILLS_DISABLED'] = 'excluded'
            if override:
                env['XIAOYU_SKILLS_DIR'] = str(base / 'explicit')
            with patch.dict(os.environ, env, clear=True):
                def skill(directory, name):
                    file = directory / name / 'SKILL.md'; file.parent.mkdir(parents=True, exist_ok=True)
                    file.write_text(f'---\nname: {name}\ndescription: offline test\n---\nBody\n', encoding='utf-8')
                shared = Path(env['HOME']) / '.agents' / 'skills'
                configured = user_config_dir() / 'skills'
                skill(shared, 'shared-skill'); skill(shared, 'excluded')
                skill(configured, 'configured-skill')
                skill(user_config_dir() / 'plugins' / 'bundle' / 'skills', 'bundle-skill')
                skill(root / '.agents' / 'skills', 'project-skill')
                skill(base / 'explicit', 'explicit-skill')
                # Startup discovery must not load executable workspace configuration.
                (root / '.env').write_text('XIAOYU_ENABLE_SKILLS=0\n', encoding='utf-8')
                app = create_app(ServeConfig(root=root, token='fixture-token', model='scripted',
                    mcp=False, persist=False, sandbox=False, state_dir=base / 'state'))
                install_preferences_routes(app, base / 'prefs.json', 'fixture-token', root=root)
                with TestClient(app, base_url='http://127.0.0.1', headers={'Authorization':'Bearer fixture-token'}) as client:
                    yield app, client, shared, configured

    def test_startup_discovers_default_shared_user_plugin_sources_without_session_or_project(self):
        with self.fixture() as (app, client, shared, configured):
            startup = app.state.desktop_skill_discovery
            self.assertEqual(startup['status'], 'ready')
            self.assertEqual(startup['count'], 4)
            self.assertFalse(app.state.sessions)
            self.assertIn(str(shared), [s['path'] for s in startup['sources']])
            self.assertIn(str(configured), [s['path'] for s in startup['sources']])
            data = client.get('/desktop/capabilities').json()
            self.assertTrue(data['skills_enabled'])
            self.assertEqual({s['name'] for s in data['skills']}, {'shared-skill','configured-skill','bundle:bundle-skill','excluded'})
            excluded = next(s for s in data['skills'] if s['name'] == 'excluded')
            self.assertTrue(excluded['toggle_locked']); self.assertFalse(excluded['enabled'])
            self.assertTrue(all(not s['loaded'] for s in data['skills']))
            self.assertEqual(data['startup_discovery'], startup)

    def test_new_session_automatically_loads_core_skills_without_project_trust(self):
        with self.fixture() as (app, client, *_):
            result = client.post('/session', json={})
            self.assertEqual(result.status_code, 200, result.text)
            sid = result.json()['session_id']; agent = app.state.sessions[sid].agent
            self.assertTrue(agent.config.enable_skills)
            self.assertFalse(agent.config.workspace_trusted)
            self.assertEqual({s.name for s in agent.skills}, {'shared-skill','configured-skill','bundle:bundle-skill'})
            data = client.get('/desktop/capabilities', params={'session_id':sid}).json()
            self.assertEqual(data['scope'], 'session_loaded')
            self.assertEqual({s['name'] for s in data['skills'] if s['loaded']}, {s.name for s in agent.skills})
            self.assertFalse(next(s for s in data['skills'] if s['name'] == 'excluded')['loaded'])
            self.assertEqual(app.state.sessions[sid].turns, 0)

    def test_directory_override_stays_isolated_and_core_plugin_namespace_remains(self):
        with self.fixture(override=True) as (app, client, shared, configured):
            data = client.get('/desktop/capabilities').json()
            self.assertEqual({s['name'] for s in data['skills']}, {'explicit-skill','bundle:bundle-skill'})
            self.assertNotIn(str(shared), [s['path'] for s in data['discovery']['sources']])
            self.assertNotIn(str(configured), [s['path'] for s in data['discovery']['sources']])

    def test_core_disable_keeps_inventory_visible_but_never_loads_skills(self):
        with self.fixture(enabled='0') as (app, client, *_):
            self.assertEqual(app.state.desktop_skill_discovery['status'], 'disabled')
            data = client.get('/desktop/capabilities').json()
            self.assertFalse(data['skills_enabled'])
            self.assertEqual(len(data['skills']), 4)
            self.assertTrue(all(s['toggle_locked'] and not s['enabled'] and not s['loaded'] for s in data['skills']))
            with patch('xiaoyu.skills.scan_skills', side_effect=AssertionError('disabled agent must not scan')):
                created = client.post('/session', json={})
            self.assertEqual(created.status_code, 200, created.text)
            self.assertFalse(app.state.sessions[created.json()['session_id']].agent.skills)

    def test_failed_startup_discovery_is_visible_and_can_be_refreshed(self):
        from xiaoyu.skills import scan_skills
        with patch('xiaoyu.skills.scan_skills', side_effect=OSError('unreadable')):
            with self.fixture() as (app, client, *_):
                self.assertEqual(app.state.desktop_skill_discovery['status'], 'error')
                with patch('xiaoyu.skills.scan_skills', scan_skills):
                    data = client.get('/desktop/capabilities').json()
                    self.assertEqual(data['discovery']['status'], 'ready')
                    self.assertEqual(data['startup_discovery']['status'], 'error')
