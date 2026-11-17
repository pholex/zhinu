"""Automation orchestration against the real service and scripted agent model."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import threading
import time
from unittest import mock

from labs.desktop import automation
from tests.test_serve import ServeCase


class AutomationServeTests(ServeCase):
    mcp = False
    token = 'automation-contract-token'

    def setup_runner(self, script, *, once=True, created=None):
        self.start(script)
        self.store = automation.AutomationStore(Path(self.tmp)/'automation.json')
        self.calls = []
        self.created = created
        self.drop_prompt_reply = False
        async def request(method,path,body=None,**kwargs):
            self.calls.append((method,path,body,kwargs))
            headers = self.headers()
            if kwargs.get('key'):
                headers['Idempotency-Key'] = kwargs['key']
            if method=='POST':
                expected = 'creating' if path=='/session' else 'submitting'
                record, = self.store.read()['runs'].values()
                self.assertEqual(record['state'],expected)
            response = self.client.request(method,path,json=body,headers=headers)
            if response.status_code>=400:
                raise ValueError(response.text)
            value = response.json()
            if path=='/session' and self.created is not None:
                self.created(value['session_id'])
            if path.endswith('/prompt_async') and self.drop_prompt_reply:
                raise ConnectionError('accepted but response lost')
            return value
        self.request = request
        self.runner = automation.AutomationRunner(self.store,request)
        definition = {'name':'任务','workspace':str(self.root),'model':'scripted','prompt':'完成任务',
                      'schedule':{'kind':'once','at':120} if once else {'kind':'interval','seconds':60}}
        self.task,state = self.store.configure(0,'',definition,now=60 if not once else 100)
        self.store.enable(state['revision'],self.task,True,now=60 if not once else 100)

    def record(self):
        return next(iter(self.store.read()['runs'].values()))

    def test_real_prompt_receipt_approval_result_and_runner_restart(self):
        self.setup_runner('tool_call: {"name":"write_file","arguments":{"path":"report.txt","content":"ok"}}\n---\ntext: done\n')
        asyncio.run(self.runner.tick(now=120))
        record = self.record()
        self.assertEqual(record['state'],'running',self.runner.warning)
        sid=record['session_id']
        status=self._wait_for(sid,'waiting_for_approval')
        self.assertEqual(record['run_id'],status['in_flight']['id'])
        self.store.recover(now=200)
        self.runner=automation.AutomationRunner(self.store,self.request)
        asyncio.run(self.runner.tick(now=200))
        self.assertEqual(self.record()['state'],'running')
        self.client.post(f'/session/{sid}/permissions',json={'request_id':status['pending_approvals'][0]['request_id'],'decision':'allow'},headers=self.headers())
        self._wait_for(sid,'finished')
        asyncio.run(self.runner.tick(now=201))
        self.assertEqual(self.record()['state'],'completed',self.runner.warning)
        self.assertEqual((self.root/'report.txt').read_text(),'ok')
        self.assertEqual(sum(method=='POST' for method,*_ in self.calls),2)

    def test_idle_answer_then_interrupted_followup_cannot_become_completed(self):
        self.setup_runner('tool_call: {"name":"ask_user","arguments":{"questions":[{"question":"Color?","options":["Blue","Green"]}]}}\n---\ntext: waiting\n---\ntool_call: {"name":"write_file","arguments":{"path":"never.txt","content":"x"}}\n---\ntext: done\n')
        with mock.patch.object(automation,'QUESTION_TIMEOUT_SECONDS',0):
            asyncio.run(self.runner.tick(now=120))
        sid=self.record()['session_id']
        self._wait_for(sid,'finished')
        asyncio.run(self.runner.refresh())
        self.assertEqual(self.record()['state'],'running')
        question,=self.client.get(f'/session/{sid}/questions',headers=self.headers()).json()['questions']
        response=self.client.post(f'/session/{sid}/questions/{question["question_id"]}/answers',json={
            'answers':[{'item_id':question['items'][0]['item_id'],'selected':['Blue']}],'idempotency_key':'answer'},headers=self.headers())
        self.assertEqual(response.status_code,202,response.text)
        asyncio.run(self.runner.refresh())
        self.assertEqual(self.record()['state'],'running')
        self.client.post(f'/session/{sid}/prompt_async',json={'text':'continue'},headers=self.headers())
        state=self._wait_for(sid,'waiting_for_approval')
        self.client.post(f'/session/{sid}/abort',json={'turn':state['in_flight']['turn']},headers=self.headers())
        self._wait_for(sid,'interrupted')
        asyncio.run(self.runner.refresh())
        self.assertEqual(self.record()['state'],'interrupted',self.runner.warning)
        self.assertFalse((self.root/'never.txt').exists())

    def test_accepted_submission_lost_reply_is_unknown_and_not_replayed(self):
        self.setup_runner('text: done\n',once=False)
        self.drop_prompt_reply=True
        asyncio.run(self.runner.tick(now=120))
        self.assertEqual(self.record()['state'],'unknown')
        sid=self.record()['session_id']
        self._wait_for(sid,'finished')
        self.store.recover(now=1000)
        self.runner=automation.AutomationRunner(self.store,self.request)
        asyncio.run(self.runner.tick(now=1000))
        self.assertEqual(self.status(sid)['turns'],1)
        self.assertEqual(len(self.client.app.state.sessions),1)
        self.assertEqual(sum(method=='POST' for method,*_ in self.calls),2)

    def test_reservation_failure_has_no_service_side_effect(self):
        self.setup_runner('text: must not run\n')
        with mock.patch.object(self.store,'_save',side_effect=OSError('disk full')):
            asyncio.run(self.runner.tick(now=120))
        self.assertEqual(self.calls,[])
        self.assertEqual(len(self.client.app.state.sessions),0)

    def test_service_restart_recovers_same_session_without_another_prompt(self):
        self.setup_runner('text: complete once\n')
        asyncio.run(self.runner.tick(now=120))
        sid=self.record()['session_id']
        self._wait_for(sid,'finished')
        execution=self.record()['run_id']
        self.client.__exit__(None,None,None)
        self.start('text: must not be invoked\n')
        self.store.recover(now=200)
        self.runner=automation.AutomationRunner(self.store,self.request)
        asyncio.run(self.runner.tick(now=200))
        self.assertEqual(self.record()['state'],'completed',self.runner.warning)
        self.assertEqual(self.record()['run_id'],execution)
        self.assertEqual(self.status(sid)['turns'],1)
        self.assertEqual(sum(method=='POST' for method,*_ in self.calls),2)

    def test_background_worker_stays_running_after_parent_and_partial_is_not_success(self):
        from tests.test_agent_paths import FakeClient, chunk
        from tests.test_chenshu import git
        from xiaoyu.providers import Registry
        from xiaoyu.agent import Agent
        script='---\n'.join([
            'tool_call: '+json.dumps({'name':name,'arguments':args})+'\n'
            for name,args in [('chenshu_init',{}),('chenshu_plan',{'missions':[{'title':'survey','kind':'survey'}]}),
                              ('chenshu_spawn',{'name':'reader','kind':'worker','mission_id':'M1'})]
            ]+['text: parent done\n'])
        entered,release=threading.Event(),threading.Event()
        def created(sid):
            runtime=self.client.app.state.sessions[sid].agent.chenshu
            child=FakeClient([[chunk(content='partial worker answer')]])
            original=child.completions.create
            def gated(**kwargs):
                entered.set()
                if not release.wait(5):
                    raise AssertionError('worker not released')
                return original(**kwargs)
            child.completions.create=gated
            runtime.registry=Registry.for_client(child)
            self.runtime=runtime
        self.approval='allow_all'
        self.setup_runner(script,created=created)
        git(self.root,'init','-q','-b','main')
        (self.root/'README.md').write_text('fixture')
        git(self.root,'add','README.md')
        git(self.root,'commit','-qm','fixture')
        with mock.patch.dict(os.environ,{'XIAOYU_ENABLE_CHENSHU':'1'}):
            asyncio.run(self.runner.tick(now=120))
        sid=self.record()['session_id']
        try:
            self._wait_for(sid,'finished')
            self.assertTrue(entered.wait(2))
            asyncio.run(self.runner.refresh())
            self.assertEqual(self.record()['state'],'running')
            member=self.runtime.member('reader')
            self.runtime.member_control('stop',board_id=self.runtime._board_id,name='reader',run_id=member.run_id,operation_id='stop-worker')
        finally:
            release.set()
            for thread in self.runtime._threads.values():
                thread.join(5)
        for _ in range(100):
            asyncio.run(self.runner.refresh())
            if self.record()['state']!='running':
                break
            time.sleep(.01)
        self.assertEqual(self.record()['state'],'failed',self.runner.warning)
        self.assertEqual(sum(method=='POST' for method,*_ in self.calls),2)
