"""A scheduled task uses native execution, approvals and persisted run references."""
import json
import shutil
import sys
import time
import unittest
from pathlib import Path
from labs.desktop.tests import test_weave_actions_contract as shared


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('git'), 'native service')
class AutomationContract(unittest.TestCase):
    setUp = shared.WeaveActionsContractTests.setUp
    cleanup_children = shared.WeaveActionsContractTests.cleanup_children
    ready = shared.WeaveActionsContractTests.ready
    request = shared.WeaveActionsContractTests.request
    stop = shared.WeaveActionsContractTests.stop
    launch = shared.WeaveActionsContractTests.launch
    write = staticmethod(shared.WeaveActionsContractTests.write)

    def api(self,info,path,body=None):
        code,_,raw=self.request(info,path,method='GET' if body is None else 'POST',body=body)
        self.assertIn(code,(200,202),raw.decode('utf-8',errors='replace'))
        return json.loads(raw)

    def test_schedule_real_approval_delivery_and_restart_without_duplicate(self):
        child,info=self.launch(self.write('scheduled.txt','定时任务交付')+'---\ntext: 已完成定时任务。\n')
        root=self.api(info,'/health')['root']
        value={'name':'生成交付','workspace':root,'model':'lab-scripted','prompt':'生成一个文件',
               'schedule':{'kind':'once','at':time.time()+3}}
        created=self.api(info,'/desktop/automations/configure',{'revision':0,'task_id':'native-schedule','definition':value,'create':True})
        self.assertFalse(created['state']['tasks']['native-schedule']['enabled'])
        self.assertTrue(created['runner_active'])
        self.assertEqual(self.api(info,'/session')['sessions'],[])
        self.api(info,'/desktop/automations/native-schedule/enabled',{'revision':created['state']['revision'],'enabled':True})
        for _ in range(400):
            state=self.api(info,'/desktop/automations')['state']
            records=list(state['runs'].values())
            if records and records[0]['state']=='running':
                break
            time.sleep(.03)
        self.assertEqual(len(records),1);record=records[0]
        self.assertEqual(record['state'],'running')
        sid=record['session_id']
        for _ in range(200):
            status=self.api(info,f'/session/{sid}/status')
            if status['pending_approvals']:
                break
            time.sleep(.02)
        self.assertFalse((Path(root)/'scheduled.txt').exists())
        self.api(info,f'/session/{sid}/permissions',{'request_id':status['pending_approvals'][0]['request_id'],'decision':'allow'})
        for _ in range(200):
            final=self.api(info,'/desktop/automations')['state']
            if list(final['runs'].values())[0]['state']=='completed':
                break
            time.sleep(.03)
        self.assertEqual(list(final['runs'].values())[0]['state'],'completed')
        self.assertEqual((Path(root)/'scheduled.txt').read_text(encoding='utf-8'),'定时任务交付')
        runs=self.api(info,f'/session/{sid}/runs')['runs']
        self.assertEqual(runs[0]['id'],record['run_id']);self.assertEqual(runs[0]['result']['artifacts'][0]['path'],'scheduled.txt')
        self.stop(child)
        child,info=self.launch('error: never replay a completed schedule\n')
        time.sleep(1.2)
        recovered=self.api(info,'/desktop/automations')['state']
        self.assertEqual(recovered,final)
        self.assertEqual(len(self.api(info,'/session')['sessions']),1)
        self.assertEqual(len(self.api(info,f'/session/{sid}/runs')['runs']),1)
        self.stop(child)
