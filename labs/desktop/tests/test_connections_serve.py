"""Real service + scripted model uses the attached SDK pool and normal approval."""
from pathlib import Path
import json
from urllib.parse import parse_qs, urlencode, urlsplit
import sys
import threading
from unittest import mock

from labs.desktop.connections_api import install_connection_routes
from labs.desktop.tests.test_connections import wait
from tests.test_mcp import write_fake_server
from tests.test_serve import ServeCase


class ConnectionServeTests(ServeCase):
    mcp = False
    token = 'local-connections-contract'

    def setup_connection(self, script='text: ready\n'):
        from xiaoyu.serve import create_app
        def build(config):
            app=create_app(config)
            self.registry=install_connection_routes(app,Path(self.tmp)/'connections.json',self.token,'http://127.0.0.1:8420')
            return app
        with mock.patch('xiaoyu.serve.create_app',side_effect=build):
            self.start(script)
        self.assertEqual(self.client.app.state.instance_id,
                         self.client.get('/health',headers=self.headers()).json()['instance_id'])
        self.sid=self.new_session()
        self.server=write_fake_server(self.root)
        response=self.client.post('/desktop/connections',json={'revision':0,'connection_id':'echo','definition':{
            'name':'Echo','transport':'stdio','command':sys.executable,'args':[str(self.server)]}},headers=self.headers())
        self.assertEqual(response.status_code,200,response.text)

    def manage(self, action, key=None):
        return self.client.post(f'/session/{self.sid}/connections/echo',json={'action':action,'configuration_revision':1},headers={**self.headers(),'Idempotency-Key':key or action,'X-Xiaoyu-Instance':self.client.app.state.instance_id})

    def test_scripted_discovery_real_mcp_invocation_requires_normal_approval(self):
        self.setup_connection('tool_call: {"name":"search_tool","arguments":{"query":"echo"}}\n---\n'
            'tool_call: {"name":"use_tool","arguments":{"tool_name":"mcp__desktop_echo__echo","tool_input":{"text":"approved call"}}}\n---\ntext: connected result\n')
        self.assertEqual(self.manage('mount').status_code,200)
        wait(lambda:self.registry.list(self.client.app.state.sessions[self.sid])['connections'][0]['state']=='ready')
        response=self.client.post(f'/session/{self.sid}/prompt_async',json={'text':'Use echo'},headers=self.headers())
        self.assertEqual(response.status_code,202,response.text)
        status=self._wait_for(self.sid,'waiting_for_approval')
        request=status['pending_approvals'][0]
        self.assertIn('echo',str(request))
        self.assertEqual(self.manage('stop').status_code,409)
        self.client.post(f'/session/{self.sid}/permissions',json={'request_id':request['request_id'],'decision':'allow'},headers=self.headers())
        self._wait_for(self.sid,'finished')
        events=self.events(self.sid)
        self.assertIn('echo: approved call',str(events))
        self.assertEqual(self.manage('stop').status_code,200)
        self.assertEqual(self.manage('remove').status_code,200)
        state=self.client.get(f'/session/{self.sid}/connections',headers=self.headers()).json()
        self.assertFalse(state['connections'][0]['mounted'])

    def test_api_auth_configuration_and_unknown_replay(self):
        self.setup_connection()
        self.assertEqual(self.client.get('/desktop/connections').status_code,401)
        self.assertEqual(self.manage('mount').status_code,200)
        session=self.client.app.state.sessions[self.sid]
        wait(lambda:self.registry.list(session)['connections'][0]['state']=='ready')
        pool=self.registry.sessions[self.sid]['pool']
        with mock.patch.object(pool,'reconnect',side_effect=RuntimeError('private-token-value')) as reconnect:
            response=self.manage('reconnect')
            self.assertEqual(response.status_code,409,response.text)
            replay=self.manage('reconnect')
            self.assertEqual(replay.status_code,200,replay.text)
            self.assertEqual(replay.json()['status'],'unknown')
            self.assertTrue(replay.json()['duplicate'])
            self.assertEqual(reconnect.call_count,1)
            self.assertNotIn('private-token-value',response.text)
        delete=self.client.post('/desktop/connections/echo/delete',json={'revision':1},headers=self.headers())
        self.assertEqual(delete.status_code,409)

    def test_management_gate_rejects_concurrent_management_until_worker_returns(self):
        self.setup_connection()
        self.assertEqual(self.manage('mount').status_code,200)
        session=self.client.app.state.sessions[self.sid]
        wait(lambda:self.registry.list(session)['connections'][0]['state']=='ready')
        pool=self.registry.sessions[self.sid]['pool']
        gate=threading.Event();entered=threading.Event();responses=[]
        def hold(name,**kwargs): entered.set();gate.wait(3)
        with mock.patch.object(pool,'reconnect',side_effect=hold):
            thread=threading.Thread(target=lambda:responses.append(self.manage('reconnect')));thread.start()
            self.assertTrue(entered.wait(2))
            try:
                self.assertTrue(session.management_busy)
                self.assertEqual(self.manage('stop').status_code,409)
                prompt=self.client.post(f'/session/{self.sid}/prompt_async',json={'text':'must wait'},headers=self.headers())
                self.assertEqual(prompt.status_code,503,prompt.text)
            finally:
                gate.set();thread.join(3)
        self.assertEqual(responses[0].status_code,200)
        self.assertFalse(session.management_busy)

    def test_public_oauth_callback_bound_to_job_and_sdk_state(self):
        from labs.desktop.tests.test_connections import ConnectionsTests
        self.setup_connection()
        self.session=self.client.app.state.sessions[self.sid]
        ConnectionsTests.provider(self)
        begin='/desktop/connections/oauth/authorizations'
        response=self.client.post(begin,json={'session_id':self.sid},headers=self.headers())
        self.assertEqual(response.status_code,200,response.text)
        aid=response.json()['authorization_id']
        path=begin+'/'+aid
        wait(lambda:self.registry.authorization('oauth',aid)['status']=='pending')
        again=self.client.post(begin,json={'session_id':self.sid},headers=self.headers())
        self.assertEqual(again.json()['authorization_id'],aid)
        self.assertEqual(self.client.get(path+'/open-url').status_code,401)
        url=self.client.get(path+'/open-url',headers=self.headers()).json()['url']
        fields=parse_qs(urlsplit(url).query)
        self.assertEqual(fields['redirect_uri'],['http://127.0.0.1:8420/desktop/oauth/callback/'+aid])
        callback='/desktop/oauth/callback/'+aid+'?'+urlencode({'state':fields['state'][0],'code':'local-code'})
        received=self.client.get(callback)
        self.assertEqual(received.status_code,200,received.text)
        self.assertEqual(received.headers['referrer-policy'],'no-referrer')
        wait(lambda:self.registry.authorization('oauth',aid)['status']=='authorized')
        status=self.client.get(path,headers=self.headers()).json()
        self.assertEqual(status['status'],'authorized')
        self.assertEqual(set(status),{'authorization_id','status','expires_at'})
        self.assertNotIn('local-access',json.dumps(status))
        self.assertEqual(self.client.get(callback).status_code,409)
        self.assertEqual(self.client.get(path+'/open-url',headers=self.headers()).status_code,409)
        rows=self.client.get(f'/session/{self.sid}/connections',headers=self.headers()).json()['connections']
        oauth=next(row for row in rows if row['id']=='oauth')
        self.assertFalse(oauth['mounted'])
        self.assertEqual(oauth['oauth_status'],'authorized')

    def test_close_during_management_retains_owner_until_process_exit(self):
        self.setup_connection()
        self.assertEqual(self.manage('mount').status_code,200)
        session=self.client.app.state.sessions[self.sid]
        wait(lambda:self.registry.list(session)['connections'][0]['state']=='ready')
        pool=self.registry.sessions[self.sid]['pool']
        entered=threading.Event();release=threading.Event();result=[]
        def hold(name,**kwargs):entered.set();release.wait(5)
        with mock.patch.object(pool,'reconnect',side_effect=hold):
            thread=threading.Thread(target=lambda:result.append(self.manage('reconnect')));thread.start()
            self.assertTrue(entered.wait(2))
            try:
                response=self.client.delete('/session/'+self.sid,headers=self.headers())
                self.assertEqual(response.status_code,200,response.text)
                wait(lambda:session.owner_uncertain)
                self.assertTrue(session.execution_owner.retained)
                self.assertTrue(session.agent.session_log.locked)
                self.assertFalse(session.management_worker_done.is_set())
            finally:
                release.set();thread.join(3)
        self.assertTrue(session.management_worker_done.is_set())
        self.assertTrue(session.execution_owner.retained)

    def test_lost_state_owner_cannot_save_delete_or_start_authorization(self):
        from types import SimpleNamespace
        from labs.desktop.tests.test_connections import ConnectionsTests
        self.setup_connection()
        self.session=self.client.app.state.sessions[self.sid]
        ConnectionsTests.provider(self)
        before=self.registry.store.read()
        with mock.patch.object(self.client.app.state,'execution_owner',SimpleNamespace(held=False)):
            changed=self.client.post('/desktop/connections',json={'revision':before['revision'],'connection_id':'new','definition':{'name':'new','transport':'http','url':'https://mcp.example.invalid'}},headers=self.headers())
            removed=self.client.post('/desktop/connections/echo/delete',json={'revision':before['revision']},headers=self.headers())
            begun=self.client.post('/desktop/connections/oauth/authorizations',json={'session_id':self.sid},headers=self.headers())
            self.assertEqual([changed.status_code,removed.status_code,begun.status_code],[503,503,503])
        self.assertEqual(self.registry.store.read(),before)
        self.assertEqual(self.registry.authorizations,{})

    def test_pool_close_timeout_retains_log_lock_while_sdk_worker_finishes(self):
        self.setup_connection()
        self.assertEqual(self.manage('mount').status_code,200)
        session=self.client.app.state.sessions[self.sid]
        wait(lambda:self.registry.list(session)['connections'][0]['state']=='ready')
        pool=self.registry.sessions[self.sid]['pool']
        entered=threading.Event();release=threading.Event()
        original=pool.close
        def close(**kwargs):entered.set();release.wait(5);return original(**kwargs)
        with mock.patch.object(pool,'close',side_effect=close):
            response=self.client.delete('/session/'+self.sid,headers=self.headers())
            self.assertEqual(response.status_code,200,response.text)
            self.assertTrue(entered.wait(2))
            try:
                wait(lambda:session.execution_owner.retained)
                self.assertTrue(session.agent.session_log.locked)
                self.assertFalse(session.connection_shutdown_done.is_set())
            finally:
                release.set()
                wait(lambda:session.connection_shutdown_done.is_set())
        self.assertTrue(session.execution_owner.retained)

    def test_old_receipt_is_readable_while_busy_and_old_instance_cannot_mount(self):
        self.setup_connection()
        self.assertEqual(self.manage('mount').status_code,200)
        session=self.client.app.state.sessions[self.sid]
        wait(lambda:self.registry.list(session)['connections'][0]['state']=='ready')
        session.busy=True
        try:
            response=self.manage('mount')
            self.assertEqual(response.status_code,200,response.text)
            self.assertTrue(response.json()['duplicate'])
        finally:session.busy=False
        self.assertEqual(self.manage('remove').status_code,200)
        old=self.client.post(f'/session/{self.sid}/connections/echo',json={'action':'mount','configuration_revision':1},headers={**self.headers(),'Idempotency-Key':'old-mount','X-Xiaoyu-Instance':'old-process'})
        self.assertEqual(old.status_code,409,old.text)
        self.assertFalse(self.registry.list(session)['connections'][0]['mounted'])

    def test_full_service_lifespan_cancels_pending_oauth_and_seals_store(self):
        from labs.desktop.tests.test_connections import ConnectionsTests
        from xiaoyu_agent_sdk.oauth import OAuthTokens
        self.setup_connection()
        self.session=self.client.app.state.sessions[self.sid]
        ConnectionsTests.provider(self)
        job=self.registry.begin_authorization(self.session,'oauth')
        aid=job['authorization_id']
        wait(lambda:self.registry.authorization('oauth',aid)['status']=='pending')
        token_store=self.registry.authorizations[aid]['store']
        self.client.__exit__(None,None,None)
        self.assertTrue(self.registry.closed)
        self.assertEqual(self.registry.authorization('oauth',aid)['status'],'cancelled')
        wait(lambda:not self.registry.authorizations[aid]['thread'].is_alive())
        with self.assertRaises(ValueError):token_store.save(OAuthTokens('late-token'))
