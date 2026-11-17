"""Terminal and desktop share the same service, session and actual execution."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from labs.desktop.tests import test_weave_actions_contract as shared

ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('git'), 'native pairing')
class ConnectContract(unittest.TestCase):
    setUp = shared.WeaveActionsContractTests.setUp
    cleanup_children = shared.WeaveActionsContractTests.cleanup_children
    ready = shared.WeaveActionsContractTests.ready
    request = shared.WeaveActionsContractTests.request
    stop = shared.WeaveActionsContractTests.stop
    launch = shared.WeaveActionsContractTests.launch
    write = staticmethod(shared.WeaveActionsContractTests.write)

    def cli(self, workspace, *args, code=0):
        result = subprocess.run([sys.executable, '-P', '-m', 'xiaoyu', 'connect', '--workspace', workspace, *args],
            cwd=ROOT, env={'PATH': os.environ['PATH'], 'PYTHONPATH': str(ROOT), 'PYTHONNOUSERSITE':'1'},
            capture_output=True, encoding='utf-8', errors='replace', timeout=15)
        self.assertEqual(result.returncode, code, result.stderr)
        return [json.loads(line) for line in result.stdout.splitlines() if line]

    def test_real_private_pairing_prompt_approval_history_and_restart_same_session(self):
        child, info = self.launch(self.write('shared.txt','同一个执行者写入')+'---\ntext: 完成。\n')
        code,_,body=self.request(info,'/session',method='POST',body={'model':'lab-scripted'})
        self.assertEqual(code,200)
        sid=json.loads(body)['session_id']
        health=json.loads(self.request(info,'/health')[2]); workspace=health['root']
        sessions=self.cli(workspace)[0]['sessions'];self.assertEqual([s['session_id'] for s in sessions],[sid])
        result=self.cli(workspace,'--session',sid,'--prompt','写入交付文件','--idempotency-key','shared-turn')
        self.assertTrue(result[-1]['in_flight']['id'])
        for _ in range(200):
            state=json.loads(self.request(info,f'/session/{sid}/status')[2])
            if state['pending_approvals']:
                break
            time.sleep(.02)
        approval=state['pending_approvals'][0]
        self.cli(workspace,'--session',sid,'--history')
        self.assertTrue(json.loads(self.request(info,f'/session/{sid}/status')[2])['busy'])
        self.cli(workspace,'--session',sid,'--steer','保留成果','--turn',str(state['in_flight']['turn']))
        self.cli(workspace,'--session',sid,'--approve',approval['request_id'],'--decision','allow')
        for _ in range(200):
            state=json.loads(self.request(info,f'/session/{sid}/status')[2])
            if not state['busy']:
                break
            time.sleep(.02)
        self.assertFalse(state['busy'])
        self.assertEqual((Path(workspace)/'shared.txt').read_text(encoding='utf-8'),'同一个执行者写入')
        self.cli(workspace,'--session',sid,'--prompt','写入交付文件','--idempotency-key','shared-turn')
        self.assertEqual(len(json.loads(self.request(info,f'/session/{sid}/runs')[2])['runs']),1)
        history=self.cli(workspace,'--session',sid,'--history')
        self.assertIn('完成。',json.dumps(history,ensure_ascii=False))
        self.assertNotIn(info['token'],json.dumps(history))
        self.stop(child)
        child,info=self.launch('error: old submission must not execute again\n')
        current=self.cli(workspace)[0]['sessions'];self.assertEqual([s['session_id'] for s in current],[sid])
        self.cli(workspace,'--session',sid,'--prompt','写入交付文件','--idempotency-key','shared-turn',code=2)
        self.assertEqual(len(json.loads(self.request(info,f'/session/{sid}/runs')[2])['runs']),1)
        self.stop(child)
