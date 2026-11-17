import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from labs.desktop.skill_settings import SkillSettings
from labs.desktop.preferences import PreferenceConflict
from labs.desktop.preferences_api import install_preferences_routes
from tests.test_serve import ServeCase
from xiaoyu.skills import SkillSource, scan_skills
from xiaoyu.session_log import SessionWriteLock


class SkillSettingsTests(unittest.TestCase):
    def test_atomic_exact_names_restart_conflicts_and_failed_write(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'settings.json'
            first, second = SkillSettings(path), SkillSettings(path)
            first.save(0, ['aws-auth','lark-im'], False)
            self.assertEqual(second.disabled_names(), ('aws-auth','lark-im'))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(PreferenceConflict): second.save(0, ['aws-auth'], True)
            old = path.read_bytes()
            with patch('labs.desktop.skill_settings.os.replace', side_effect=OSError('disk')):
                with self.assertRaises(OSError): first.save(1, ['aws-auth'], True)
            self.assertEqual(path.read_bytes(), old)
            lease=SessionWriteLock(path)
            try:
                with self.assertRaises(PreferenceConflict): second.save(1, ['aws-auth'], True)
            finally: lease.close()
            second.save(1, ['aws-auth'], True)
            self.assertEqual(first.disabled_names(), ('lark-im',))
            path.write_text('{broken',encoding='utf-8')
            with self.assertRaises(ValueError): first.read()
            with self.assertRaises(ValueError): first.save(2,['lark-im'],True)
            self.assertEqual(path.read_text(),'{broken')

    def test_inventory_can_include_core_disabled_skills_without_enabling_them(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);directory=root/'aws-auth';directory.mkdir()
            (directory/'SKILL.md').write_text('---\nname: aws-auth\ndescription: Identity workflows\n---\nBody',encoding='utf-8')
            with patch('xiaoyu.skills.skill_sources',return_value=[SkillSource(root)]), patch.dict(os.environ,{'XIAOYU_SKILLS_DISABLED':'aws-*'}):
                self.assertEqual(scan_skills(),[])
                all_skills=scan_skills(include_disabled=True)
                self.assertEqual(all_skills[0].description,'Identity workflows')
                self.assertEqual(scan_skills(),[])


class SkillSettingsServeTests(ServeCase):
    def setup_skills(self):
        directory=self.root/'skill-fixtures';directory.mkdir()
        for name in ['aws-auth','aws-compute']:
            target=directory/name;target.mkdir()
            (target/'SKILL.md').write_text(f'---\nname: {name}\ndescription: Offline {name}\n---\nSkill body',encoding='utf-8')
        self.enterContext(patch('xiaoyu.skills.skill_sources',return_value=[SkillSource(directory)]))
        self.token='t'
        client=self.start('text: first\n---\ntext: second\n')
        self.enterContext(patch.dict(os.environ,{'XIAOYU_ENABLE_SKILLS':'1','XIAOYU_SKILLS_DISABLED':''}))
        install_preferences_routes(client.app,self.root/'prefs','t',root=self.root)
        headers={**self.headers(),'X-Xiaoyu-Instance':client.app.state.instance_id}
        return client,headers,directory

    def test_toggle_is_durable_reversible_and_applies_to_idle_and_new_sessions(self):
        client,headers,directory=self.setup_skills();sid=self.new_session();agent=client.app.state.sessions[sid].agent
        self.assertEqual(len(agent.skills),2)
        route='/desktop/skills/settings';body={'revision':0,'ids':['aws-auth'],'enabled':False,'session_id':sid}
        self.assertEqual(client.post(route,json=body).status_code,401)
        result=client.post(route,json=body,headers=headers);self.assertEqual(result.status_code,200,result.text)
        self.assertNotIn('aws-auth',[s.name for s in agent.skills]);self.assertTrue(agent._load_skill('aws-auth').startswith('ERROR:'))
        self.assertTrue((directory/'aws-auth/SKILL.md').is_file())
        inventory=client.get('/desktop/capabilities?session_id='+sid,headers=headers).json()
        row=next(s for s in inventory['skills'] if s['id']=='aws-auth');self.assertFalse(row['enabled']);self.assertFalse(row['loaded'])
        other=self.new_session();self.assertNotIn('aws-auth',[s.name for s in client.app.state.sessions[other].agent.skills])
        self.assertEqual(client.post(route,json=body,headers=headers).status_code,409)
        result=client.post(route,json={**body,'revision':1,'enabled':True},headers=headers)
        self.assertEqual(result.status_code,200,result.text)
        self.assertIn('aws-auth',[s.name for s in agent.skills]);self.assertIn('aws-auth',[s.name for s in client.app.state.sessions[other].agent.skills])
        self.assertEqual(SkillSettings(self.root/'skill-settings.json').disabled_names(),())

    def test_busy_session_waits_for_next_turn_and_core_disabled_cannot_be_enabled(self):
        client,headers,_=self.setup_skills();sid=self.new_session();session=client.app.state.sessions[sid]
        session.busy=True
        result=client.post('/desktop/skills/settings',json={'revision':0,'ids':['aws-auth'],'enabled':False,'session_id':sid},headers=headers)
        self.assertEqual(result.status_code,200,result.text);self.assertEqual(result.json()['pending_sessions'],1)
        self.assertIn('aws-auth',[s.name for s in session.agent.skills]);self.assertEqual(session.agent.config.disabled_skill_names,())
        session.busy=False
        result=client.post(f'/session/{sid}/prompt',json={'text':'hello'},headers=headers)
        self.assertEqual(result.status_code,200,result.text);self.assertNotIn('aws-auth',[s.name for s in session.agent.skills])
        with patch.dict(os.environ,{'XIAOYU_SKILLS_DISABLED':'aws-*'}):
            inventory=client.get('/desktop/capabilities',headers=headers).json();self.assertTrue(all(s['toggle_locked'] for s in inventory['skills']))
            result=client.post('/desktop/skills/settings',json={'revision':1,'ids':['aws-auth'],'enabled':True,'session_id':sid},headers=headers)
            self.assertEqual(result.status_code,422)

    def test_shared_file_updates_another_service_at_next_turn(self):
        client,headers,_=self.setup_skills();sid=self.new_session();session=client.app.state.sessions[sid]
        external=SkillSettings(self.root/'skill-settings.json');external.save(0,['aws-auth'],False)
        result=client.post(f'/session/{sid}/prompt',json={'text':'hello'},headers=headers)
        self.assertEqual(result.status_code,200,result.text);self.assertNotIn('aws-auth',[s.name for s in session.agent.skills])
        external.save(1,['aws-auth'],True)
        result=client.post(f'/session/{sid}/prompt',json={'text':'again'},headers=headers)
        self.assertEqual(result.status_code,200,result.text);self.assertIn('aws-auth',[s.name for s in session.agent.skills])
