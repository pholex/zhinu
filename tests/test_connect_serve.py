"""Staged CLI flags against the real serve app and scripted model execution."""
from __future__ import annotations

import contextlib
import io
import json
import time
from unittest import mock

from xiaoyu import cli_connect as connect
from tests.test_serve import ServeCase


class ConnectServeTest(ServeCase):
    token = 'local-contract-token'
    mcp = False

    def cli(self, sid=None, *flags):
        health = self.client.get('/health', headers=self.headers()).json()
        paired = connect.Pairing('http://127.0.0.1:8420', self.token, health['instance_id'], str(self.root))
        requests = []
        # Replace only the TCP hop; every endpoint/body is parsed and executed by
        # the real authenticated service, including its approval and input queues.
        def request(client, method, path, body=None, key=''):
            requests.append((method,path,body))
            headers = {'Authorization':f'Bearer {client.pairing.token}', 'X-Xiaoyu-Instance':client.pairing.instance_id}
            if key:
                headers['Idempotency-Key'] = key
            response = self.client.request(method, path, json=body, headers=headers)
            if not 200 <= response.status_code < 300:
                raise connect.ConnectError(f'HTTP {response.status_code}')
            return response.json()
        out, err = io.StringIO(), io.StringIO()
        argv = ['--workspace',str(self.root)] + (['--session',sid] if sid else []) + list(flags)
        with mock.patch.object(connect,'pair',return_value=paired), \
                mock.patch.object(connect.ConnectClient,'request',request), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = connect.connect_command(argv)
        return result, [json.loads(line) for line in out.getvalue().splitlines()], err.getvalue(), requests

    def test_prompt_steer_single_approval_and_history_use_existing_service(self):
        self.start('tool_call: {"name":"write_file","arguments":{"path":"result.txt","content":"ok"}}\n---\ntext: finished\n---\ntext: continued\n')
        sid = self.new_session()
        result, output, _, _ = self.cli(sid, '--prompt','create report','--idempotency-key','one-task')
        self.assertEqual(result, 0)
        self.assertTrue(output[-1]['accepted'])
        state = self._wait_for(sid, 'waiting_for_approval')
        turn = state['in_flight']['turn']
        result, output, _, _ = self.cli(sid, '--steer','核对结果', '--turn',str(turn))
        self.assertEqual(result, 0)
        self.assertTrue(output[-1]['steered'])
        self.assertNotIn('accepted', output[-1])
        pending = state['pending_approvals'][0]
        result, _, err, _ = self.cli(sid, '--approve',pending['request_id'],'--decision','allow')
        self.assertEqual(result, 0, err)
        self._wait_for(sid,'finished')
        self.assertEqual((self.root/'result.txt').read_text(), 'ok')
        result, pages, _, requests = self.cli(sid, '--history')
        self.assertEqual(result,0)
        messages = [m for page in pages for m in page['messages']]
        self.assertTrue(any('核对结果' in m['content'] for m in messages))
        self.assertTrue(all(method=='GET' for method,_,_ in requests))
        result, _, _, _ = self.cli(sid, '--prompt','create report','--idempotency-key','one-task')
        self.assertEqual(result, 0)
        self.assertEqual(self.status(sid)['turns'],1)

    def test_stale_turn_does_not_abort_or_steer_current_approval(self):
        self.start('tool_call: {"name":"write_file","arguments":{"path":"never.txt","content":"x"}}\n---\ntext: done\n')
        sid = self.new_session()
        self.cli(sid,'--prompt','work')
        state = self._wait_for(sid,'waiting_for_approval')
        turn = state['in_flight']['turn']
        result, output, _, _ = self.cli(sid,'--abort','--turn',str(turn+1))
        self.assertEqual(result,0)
        self.assertFalse(output[-1]['aborted'])
        self.assertTrue(self.status(sid)['busy'])
        result, _, _, _ = self.cli(sid,'--steer','stale','--turn',str(turn+1))
        self.assertEqual(result,2)
        self.assertTrue(self.status(sid)['busy'])
        result, output, _, _ = self.cli(sid,'--abort','--turn',str(turn))
        self.assertEqual(result,0)
        self.assertTrue(output[-1]['aborted'])
        self._wait_for(sid,'interrupted')
        self.assertFalse((self.root/'never.txt').exists())

    def test_answer_is_queued_at_idle_and_consumed_only_by_explicit_later_prompt(self):
        self.start('tool_call: {"name":"ask_user","arguments":{"questions":[{"question":"Color?","options":["Blue","Green"]}]}}\n---\ntext: waiting\n---\ntext: received\n')
        sid = self.new_session(questions={})
        result, _, _, _ = self.cli(sid,'--prompt','ask')
        self.assertEqual(result,0)
        self._wait_for(sid,'finished')
        question, = self.client.get(f'/session/{sid}/questions',headers=self.headers()).json()['questions']
        answers = json.dumps([{'item_id':question['items'][0]['item_id'],'selected':['Blue']}])
        flags = ('--answer',question['question_id'],'--answers-json',answers,'--idempotency-key','answer-one')
        result, output, err, _ = self.cli(sid,*flags)
        self.assertEqual(result,0,err)
        self.assertEqual(output[-1]['question']['state'],'queued')
        self.assertFalse(self.status(sid)['busy'])
        self.assertEqual(self.status(sid)['turns'],1)
        self.assertEqual(self.cli(sid,*flags)[1][-1]['question']['state'],'queued')
        self.cli(sid,'--prompt','continue')
        self._wait_for(sid,'finished')
        self.assertEqual(self.cli(sid,*flags)[1][-1]['question']['state'],'answered')
