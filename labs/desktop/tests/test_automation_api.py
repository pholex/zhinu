import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from labs.desktop.automation_api import install_automation_routes


class AutomationApiTests(unittest.TestCase):
    def test_creation_http_rejection_differs_from_uncertain_server_failure(self):
        import time
        from fastapi import FastAPI
        from starlette.responses import JSONResponse
        for code, expected in ((400,'failed'), (422,'failed'), (500,'unknown')):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as folder:
                root = Path(folder); app = FastAPI()
                app.state.execution_owner = SimpleNamespace(held=True)
                @app.get('/health')
                async def health():
                    return {'instance_id':'test-instance'}
                calls = []
                @app.post('/session')
                async def create():
                    calls.append('/session')
                    return JSONResponse({'detail':'fixture rejection'}, status_code=code)
                store, runtime = install_automation_routes(app, root/'automation.json', 'fixture', root, 'http://local', interval=60)
                task, state = store.configure(0, '', {'name':'task','workspace':str(root),'model':'scripted',
                    'prompt':'work','schedule':{'kind':'interval','seconds':60}})
                store.enable(state['revision'], task, True)
                async def check():
                    async with app.router.lifespan_context(app):
                        await runtime['runner'].tick(now=time.time()+61)
                        state = store.read()
                        self.assertEqual(next(iter(state['runs'].values()))['state'], expected)
                        self.assertFalse(state['tasks'][task]['enabled'])
                        self.assertEqual(calls, ['/session'])
                asyncio.run(check())


    def test_configure_is_disabled_stable_id_retry_and_explicit_enable(self):
        import httpx
        from fastapi import FastAPI
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();app=FastAPI();app.state.execution_owner=SimpleNamespace(held=True)
            store,runtime=install_automation_routes(app,root/'automation.json','local-test',root,'http://local')
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
                    self.assertEqual((await client.get('/desktop/automations')).status_code,401)
                    client.headers['Authorization']='Bearer local-test'
                    value={'name':'检查','workspace':str(root),'model':'scripted','prompt':'整理结果','schedule':{'kind':'interval','seconds':60}}
                    body={'revision':0,'task_id':'stable-request','definition':value,'create':True}
                    first=await client.post('/desktop/automations/configure',json=body)
                    self.assertEqual(first.status_code,200,first.text)
                    data=first.json();self.assertFalse(data['state']['tasks']['stable-request']['enabled'])
                    self.assertFalse(data['runner_active'])
                    again=await client.post('/desktop/automations/configure',json=body)
                    self.assertEqual(again.json(),data)
                    enabled=await client.post('/desktop/automations/stable-request/enabled',json={'revision':1,'enabled':True})
                    self.assertEqual(enabled.status_code,200,enabled.text)
                    same=await client.post('/desktop/automations/stable-request/enabled',json={'revision':1,'enabled':True})
                    self.assertEqual(same.json(),enabled.json())
                    create_retry=await client.post('/desktop/automations/configure',json=body)
                    self.assertTrue(create_retry.json()['state']['tasks']['stable-request']['enabled'])
                    self.assertEqual(create_retry.json()['state']['tasks']['stable-request']['next_due'],enabled.json()['state']['tasks']['stable-request']['next_due'])
                    wrong=await client.post('/desktop/automations/configure',json={**body,'definition':{**value,'prompt':'different'}})
                    self.assertEqual(wrong.status_code,409)
                    self.assertEqual((await client.post('/desktop/automations/configure',json={**body,'task_id':'outside','definition':{**value,'workspace':'/private/tmp'}})).status_code,403)
                    self.assertEqual((await client.post('/desktop/automations/configure',json={**body,'create':'true'})).status_code,422)
                    self.assertNotIn('local-test',json.dumps(data))
            asyncio.run(check())

    def test_unknown_acknowledgement_is_separate_from_enable(self):
        import httpx
        from fastapi import FastAPI
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=FastAPI();app.state.execution_owner=SimpleNamespace(held=True)
            store,_=install_automation_routes(app,root/'automation.json','local-test',root,'http://local')
            value={'name':'检查','workspace':str(root),'model':'scripted','prompt':'整理','schedule':{'kind':'interval','seconds':60}}
            tid,_=store.configure(0,'stable',value,now=100,create=True)
            store.enable(1,tid,True,now=100)
            rid,_=store.reserve_due(now=160);state=store.record(rid,state='unknown')
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local',headers={'Authorization':'Bearer local-test'}) as client:
                    route='/desktop/automation-runs/'+rid+'/acknowledge'
                    data={'revision':state['revision'],'note':'已核对原会话'}
                    first=await client.post(route,json=data)
                    self.assertEqual(first.status_code,200,first.text)
                    self.assertEqual(first.json()['state']['runs'][rid]['state'],'acknowledged')
                    self.assertFalse(first.json()['state']['tasks'][tid]['enabled'])
                    self.assertEqual((await client.post(route,json=data)).json(),first.json())
            asyncio.run(check())

    def test_state_owner_loss_keeps_get_but_rejects_new_configuration_enable_and_ack(self):
        import httpx
        from fastapi import FastAPI
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();app=FastAPI();app.state.execution_owner=SimpleNamespace(held=True)
            store,_=install_automation_routes(app,root/'automation.json','local-test',root,'http://local')
            value={'name':'检查','workspace':str(root),'model':'scripted','prompt':'整理','schedule':{'kind':'interval','seconds':60}}
            tid,_=store.configure(0,'stable',value,now=100,create=True)
            store.enable(1,tid,True,now=100)
            rid,_=store.reserve_due(now=160)
            saved=store.record(rid,state='unknown')
            _,saved=store.configure(saved['revision'],'disabled',value,now=100,create=True)
            app.state.execution_owner.held=False
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local',headers={'Authorization':'Bearer local-test'}) as client:
                    for route,data in (
                        ('/desktop/automations/configure',{'revision':saved['revision'],'task_id':'another','definition':value,'create':True}),
                        ('/desktop/automations/disabled/enabled',{'revision':saved['revision'],'enabled':True}),
                        ('/desktop/automation-runs/'+rid+'/acknowledge',{'revision':saved['revision'],'note':'已核对'}),
                    ):
                        result=await client.post(route,json=data)
                        self.assertEqual(result.status_code,503,result.text)
                        self.assertEqual(store.read(),saved)
                    self.assertEqual((await client.get('/desktop/automations')).json()['state'],saved)
            asyncio.run(check())

    def test_ack_requires_original_session_quiescence_and_rechecks_after_fleet_read(self):
        import httpx
        from fastapi import FastAPI
        from labs.desktop.automation import AutomationRunner
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=FastAPI();app.state.execution_owner=SimpleNamespace(held=True)
            store,runtime=install_automation_routes(app,root/'automation.json','local-test',root,'http://local')
            value={'name':'检查','workspace':str(root),'model':'scripted','prompt':'整理','schedule':{'kind':'interval','seconds':60}}
            tid,_=store.configure(0,'stable',value,now=100,create=True)
            store.enable(1,tid,True,now=100)
            rid,_=store.reserve_due(now=160)
            saved=store.record(rid,state='unknown',session_id='original')
            mode='busy';status_reads=0;calls=[]
            async def request(method,path,body=None,**kwargs):
                nonlocal status_reads
                calls.append((method,path))
                self.assertEqual(method,'GET')
                if path.endswith('/status'):
                    status_reads+=1
                    changed=mode=='changed' and status_reads>1
                    return {'busy':mode=='busy' or changed,'turns':2 if changed else 1,
                            'pending_approvals':[], 'writable':True}
                if '/fleets?' in path:
                    await asyncio.sleep(0)
                    members=[{'execution_state':'unknown','thread_alive':False}] if mode=='unknown-member' else []
                    return {'fleets':[{'board_id':'b','members':members}],'persisted':True}
                raise AssertionError(path)
            runtime['runner']=AutomationRunner(store,request)
            async def check():
                nonlocal mode,status_reads
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local',headers={'Authorization':'Bearer local-test'}) as client:
                    route='/desktop/automation-runs/'+rid+'/acknowledge'
                    data={'revision':saved['revision'],'note':'已核对原会话'}
                    for value in ('busy','unknown-member','changed'):
                        mode=value;status_reads=0
                        reply=await client.post(route,json=data)
                        self.assertEqual(reply.status_code,409,(value,reply.text))
                        self.assertEqual(store.read(),saved)
                    mode='idle';status_reads=0
                    reply=await client.post(route,json=data)
                    self.assertEqual(reply.status_code,200,reply.text)
                    state=reply.json()['state']
                    self.assertEqual(state['runs'][rid]['state'],'acknowledged')
                    self.assertFalse(state['tasks'][tid]['enabled'])
                    self.assertGreaterEqual(status_reads,2)
            asyncio.run(check())
