import asyncio
import copy
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from labs.desktop.data_api import install_data_routes, usage_snapshot, occupancy, BACKUP_SCHEMA
from labs.desktop.preferences_api import install_preferences_routes
from xiaoyu.agent import Usage


class DataTests(unittest.TestCase):
    def test_usage_uses_actual_route_counters_and_no_billing_estimate(self):
        usage=Usage();usage.add('provider/m',12,7);usage.add('other/m',5,9)
        session=SimpleNamespace(title='task',agent=SimpleNamespace(usage=usage),history_warning='')
        data=usage_snapshot({'s':session})
        self.assertEqual(data['totals'],{'calls':2,'prompt_tokens':17,'completion_tokens':16})
        self.assertEqual(data['by_model']['provider/m']['prompt_tokens'],12)
        self.assertFalse(data['cost_available']);self.assertNotIn('cost',data['totals'])
        usage.add('provider/m',3,2)
        self.assertEqual(data['totals']['calls'],2)

    def test_storage_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'managed').mkdir();(root/'managed'/'a').write_bytes(b'123')
            (root/'outside').write_bytes(b'123456789')
            (root/'managed'/'link').symlink_to(root/'outside')
            row=occupancy({'app':root/'managed'})[0]
            self.assertEqual(row['bytes'],3);self.assertEqual(row['files'],1);self.assertTrue(row['warning'])

    def test_metadata_export_import_cas_owner_and_forbidden_receipts(self):
        import httpx
        from fastapi import FastAPI
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);app=FastAPI();app.state.execution_owner=SimpleNamespace(held=True);app.state.sessions={}
            app.state.serve_config=SimpleNamespace()
            prefs=install_preferences_routes(app,root/'preferences.json','t',root=root)
            workspace=install_data_routes(app,'t',workspace_path=root/'workspace.json',managed_paths={'app':root})
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local',headers={'Authorization':'Bearer t'}) as c:
                    first=(await c.get('/desktop/data/backup?kind=preferences')).json()
                    backup=json.loads(first['content']);self.assertEqual(set(backup),{'schema','kind','values'})
                    backup['values']['ui']['theme']='dark'
                    body={'kind':'preferences','revision':0,'backup':backup}
                    result=await c.post('/desktop/data/import',json=body)
                    self.assertEqual(result.status_code,200,result.text)
                    self.assertEqual(prefs.read()['values']['ui']['theme'],'dark')
                    altered=copy.deepcopy(body);altered['backup']['values']['ui']['theme']='light'
                    self.assertEqual((await c.post('/desktop/data/import',json=altered)).status_code,409)
                    value={'schema':BACKUP_SCHEMA,'kind':'workspace','values':{'projects':{'a'*24:{'context':'explicit project notes'}},'sessions':{}}}
                    restored=await c.post('/desktop/data/import',json={'kind':'workspace','revision':0,'backup':value})
                    self.assertEqual(restored.status_code,200,restored.text)
                    self.assertEqual(workspace.read()['projects']['a'*24]['context'],'explicit project notes')
                    value['values']['outbox']={'replay':'forbidden'}
                    self.assertEqual((await c.post('/desktop/data/import',json={'kind':'workspace','revision':1,'backup':value})).status_code,422)
                    app.state.execution_owner.held=False
                    altered['revision']=1
                    self.assertEqual((await c.post('/desktop/data/import',json=altered)).status_code,503)
                    self.assertEqual((await c.get('/desktop/data')).status_code,200)
                    self.assertEqual((await c.post('/desktop/data/clear-index',json={})).status_code,503)
            asyncio.run(check())


from tests.test_serve import ServeCase


class UsageRestoreTests(ServeCase):
    def test_repeated_service_restart_restores_latest_cumulative_usage_exactly_once(self):
        state=self.root/'service-state'
        script='usage: {"prompt_tokens": 100, "completion_tokens": 50}\ntext: completed\n'
        first=self.start(script,state_dir=state)
        sid=self.new_session()
        self.assertEqual(first.post(f'/session/{sid}/prompt',json={'text':'first'}).status_code,200)
        self.assertEqual(usage_snapshot(first.app.state.sessions)['totals'],{'calls':1,'prompt_tokens':100,'completion_tokens':50})
        first.__exit__(None,None,None)
        second=self.start(script,state_dir=state)
        self.assertEqual(usage_snapshot(second.app.state.sessions)['totals'],{'calls':1,'prompt_tokens':100,'completion_tokens':50})
        self.assertEqual(second.post(f'/session/{sid}/prompt',json={'text':'second'}).status_code,200)
        self.assertEqual(usage_snapshot(second.app.state.sessions)['totals'],{'calls':2,'prompt_tokens':200,'completion_tokens':100})
        second.__exit__(None,None,None)
        third=self.start(script,state_dir=state)
        self.assertEqual(usage_snapshot(third.app.state.sessions)['totals'],{'calls':2,'prompt_tokens':200,'completion_tokens':100})
        self.assertEqual(usage_snapshot(third.app.state.sessions)['totals'],{'calls':2,'prompt_tokens':200,'completion_tokens':100})
        fork=self.new_session(fork_from={'session_id':sid})
        self.assertEqual(third.app.state.sessions[fork].agent.usage.to_dict()['turns'],0)
        self.assertEqual(usage_snapshot(third.app.state.sessions)['totals'],{'calls':2,'prompt_tokens':200,'completion_tokens':100})
