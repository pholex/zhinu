from __future__ import annotations

import asyncio
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlencode, urlsplit

from labs.desktop import connections
from labs.desktop.connections import ConnectionStore, ConnectionRegistry, ConnectionConflict, ConnectionError
from tests.test_mcp import write_fake_server
from xiaoyu.config import Config
from xiaoyu.tools import Toolbox


def wait(predicate, seconds=5):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('timed out')


class ConnectionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = ConnectionStore(self.root/'connections.json')
        self.registry = ConnectionRegistry(self.store, 'http://127.0.0.1:8420')
        self.addCleanup(self.registry.close)
        self.session = self.new_session('one')

    def new_session(self, sid):
        cfg = Config(base_url='http://127.0.0.1:1', model='scripted', workspace=self.root, enable_mcp=False)
        return SimpleNamespace(id=sid, agent=SimpleNamespace(toolbox=Toolbox(cfg)), busy=False,
                               closing=False, fleets=SimpleNamespace(fleets={}))

    def stdio(self):
        server = write_fake_server(self.root)
        return {'name':'echo', 'transport':'stdio', 'command':sys.executable, 'args':[str(server)]}

    def manage(self, action, key=None, **extra):
        return self.registry.manage(self.session, 'echo', {'action':action,'configuration_revision':1,**extra},key or action)

    def ready(self):
        return wait(lambda: (s if s['state']=='ready' else None) if (s:=self.registry.list(self.session)['connections'][0]) else None)

    def test_missing_command_rejected_before_mount_or_receipt(self):
        self.registry.configure(0, 'echo', {**self.stdio(), 'command':'missing-desktop-runtime-fixture'})
        with self.assertRaisesRegex(ConnectionConflict, '启动命令'):
            self.manage('mount')
        self.assertNotIn(self.session.id, self.registry.sessions)
        self.assertIsNone(self.registry.receipt(self.session.id, 'echo',
            {'action':'mount','configuration_revision':1}, 'mount'))

    def test_stdio_values_named_environment_and_working_directory_reach_process(self):
        from tests.test_mcp import FAKE_SERVER
        marker = self.root / 'received.json'
        script = self.root / 'environment-server.py'
        script.write_text('import os, json\nfrom pathlib import Path\n' +
            f'Path({str(marker)!r}).write_text(json.dumps({{"cwd":os.getcwd(),"fixed":os.getenv("MCP_FIXED"),"passed":os.getenv("MCP_PASSED"),"other":os.getenv("MCP_OTHER")}}))\n' + FAKE_SERVER, encoding='utf-8')
        directory = self.root / 'working'; directory.mkdir()
        config = {'name':'local','transport':'stdio','command':sys.executable,'args':[str(script)],
                  'env':{'MCP_FIXED':'fixed value'},'inherit_env':['MCP_PASSED'],'cwd':str(directory)}
        self.registry.configure(0,'echo',config)
        with mock.patch.dict('os.environ', {'MCP_PASSED':'inherited value','MCP_OTHER':'not forwarded'}):
            self.manage('mount'); self.ready()
        received=json.loads(marker.read_text(encoding='utf-8',errors='replace'))
        self.assertEqual(received,{'cwd':str(directory.resolve()),'fixed':'fixed value','passed':'inherited value','other':None})
        self.assertNotIn('inherited value',self.store.path.read_text(encoding='utf-8',errors='replace'))

    def test_http_headers_and_bearer_references_reach_real_transport(self):
        self.provider()
        url=self.store.get('oauth')['definition']['url']
        self.registry.configure(1,'echo',{'name':'http','transport':'http','url':url,
            'headers':{'X-Client':'desktop'},'env_headers':{'X-Tenant':'MCP_TEST_TENANT'},'bearer_token_env_var':'MCP_TEST_BEARER'})
        with mock.patch.dict('os.environ',{'MCP_TEST_TENANT':'tenant-value','MCP_TEST_BEARER':'local-'+'bearer'}):
            self.registry.manage(self.session,'echo',{'action':'mount','configuration_revision':2},'http-mount')
            wait(lambda: next((r for r in self.registry.list(self.session)['connections'] if r['id']=='echo' and r['state']=='ready'),None))
        self.assertTrue(self.mcp_headers)
        for headers in self.mcp_headers:
            lowered={k.lower():v for k,v in headers.items()}
            self.assertEqual(lowered['authorization'],'Bearer local-'+'bearer')
            self.assertEqual(lowered['x-client'],'desktop')
            self.assertEqual(lowered['x-tenant'],'tenant-value')
        serialized=self.store.path.read_text(encoding='utf-8',errors='replace')+json.dumps(self.registry.list(self.session))
        self.assertNotIn('tenant-value',serialized)
        self.assertNotIn('local-'+'bearer',serialized)

    def test_config_rejects_ambiguous_auth_duplicate_headers_and_invalid_references(self):
        base={'name':'http','transport':'http','url':'https://example.invalid/mcp'}
        bad=[{'headers':{'X-Test':'a','x-test':'b'}},{'headers':{'X-Test':'a\r\nInjected: yes'}},
             {'env_headers':{'X-Test':'not a variable'}},{'headers':{'Content-Length':'0'}},
             {'headers':{'Authorization':'value'},'bearer_token_env_var':'TOKEN'},
             {'headers':{'X-Test':'a'},'env_headers':{'x-test':'TEST'}}]
        for fields in bad:
            with self.subTest(fields=fields),self.assertRaises(ConnectionError):
                connections.definition({**base,**fields})
        with self.assertRaises(ConnectionError):
            connections.definition({**self.stdio(),'env':{'VAR':'v'},'inherit_env':['VAR']})
        with mock.patch.dict('os.environ',{},clear=True),self.assertRaisesRegex(ConnectionConflict,'MCP_NOT_SET'):
            connections.runtime_options({'bearer_token_env_var':'MCP_NOT_SET'})
        with self.assertRaisesRegex(ConnectionConflict,'工作目录'):
            connections.runtime_options({'cwd':str(self.root/'absent')})

    def test_new_store_preserves_legacy_configuration_for_rollback(self):
        self.registry.configure(0,'echo',self.stdio())
        before=self.store.path.read_bytes()
        new=ConnectionStore(self.root/'connections-v2.json',self.store.path)
        self.assertEqual(new.read(),self.store.read())
        registry=ConnectionRegistry(new,'http://127.0.0.1:8420');self.addCleanup(registry.close)
        registry.configure(1,'remote',{'name':'remote','transport':'http','url':'https://example.invalid/mcp','headers':{'X-Client':'desktop'}})
        self.assertEqual(self.store.path.read_bytes(),before)
        self.assertEqual(len(new.read()['connections']),2)
        self.assertEqual(new.path.stat().st_mode & 0o777,0o600)

    def test_keyless_saved_configuration_is_not_a_live_connection(self):
        config=self.registry.configure(0,'research',{'name':'资料连接','transport':'http','url':'https://mcp.example.invalid/tools'})
        actual={'config':config,'session':self.registry.list(self.session)}
        expected=json.loads(Path(__file__).with_name('connections.expected.json').read_text(encoding='utf-8',errors='replace'))
        self.assertEqual(actual,expected)
        self.assertEqual(self.registry.sessions,{})

    def test_store_cas_nonsecret_validation_and_no_auto_mount(self):
        state = self.registry.configure(0, 'echo', self.stdio())
        self.assertEqual(state['revision'],1)
        self.assertEqual(self.registry.configure(0,'echo',self.stdio()),state)
        self.assertFalse(self.registry.list(self.session)['connections'][0]['mounted'])
        restored=ConnectionRegistry(self.store,'http://127.0.0.1:8420')
        self.addCleanup(restored.close)
        self.assertEqual(restored.sessions,{})
        with self.assertRaises(ConnectionConflict):
            self.registry.configure(0,'second',self.stdio())
        for bad in ({'headers':{'Authorization':'secret'}},{'env':{'BAD-NAME':'value'}},{'oauth':{'client_secret':'secret'}}):
            with self.assertRaises(ConnectionError):
                self.registry.configure(1,'bad',{**self.stdio(),**bad})
        with self.assertRaises(ConnectionError):
            connections.definition({'name':'x','transport':'http','url':'https://host/mcp?token=secret'})
        self.assertEqual(self.store.path.stat().st_mode & 0o777,0o600)

    def test_real_stdio_lease_discovery_stop_restart_remove_and_retry(self):
        self.registry.configure(0,'echo',self.stdio())
        response=self.manage('mount')
        state=self.ready()
        self.assertTrue(state['tools'])
        self.assertTrue(self.manage('mount')['duplicate'])
        with self.assertRaises(ConnectionConflict):
            self.manage('stop',key='mount')
        toolbox=self.session.agent.toolbox
        found=json.loads(toolbox._search_mcp_tools('echo'))
        self.assertIn('mcp__desktop_echo__echo',json.dumps(found))
        result=toolbox._use_tool('mcp__desktop_echo__echo',{'text':'connected'})
        self.assertIn('echo: connected',result)
        pool=self.registry.sessions['one']['pool']
        with self.assertRaises(Exception):
            pool.stop('desktop_echo',_owner='wrong-session')
        self.manage('stop')
        self.assertEqual(self.registry.list(self.session)['connections'][0]['state'],'stopped')
        self.assertIn('ERROR',toolbox._use_tool('mcp__desktop_echo__echo',{'text':'no'}))
        self.manage('start')
        self.ready()
        self.manage('remove')
        self.assertFalse(self.registry.list(self.session)['connections'][0]['mounted'])
        self.assertEqual(len(self.store.read()['connections']),1)

    def test_changed_declaration_exact_review_and_quarantine(self):
        self.registry.configure(0,'echo',self.stdio())
        self.manage('mount')
        first=self.ready()
        write_fake_server(self.root,echo_description='Changed effect')
        self.manage('reconnect')
        changed=wait(lambda: s if (s:=self.registry.list(self.session)['connections'][0])['declaration_fingerprint']!=first['declaration_fingerprint'] else None)
        self.assertTrue(any(t['quarantined'] for t in changed['tools']))
        with self.assertRaises(ConnectionConflict):
            self.manage('approve_changes',key='stale',expected_fingerprint=first['declaration_fingerprint'])
        self.manage('approve_changes',key='current',expected_fingerprint=changed['declaration_fingerprint'])
        self.assertFalse(any(t['quarantined'] for t in self.registry.list(self.session)['connections'][0]['tools']))
        self.assertTrue(self.manage('approve_changes',key='current',expected_fingerprint=changed['declaration_fingerprint'])['duplicate'])

    def test_busy_unknown_live_children_and_existing_manager_fail_closed(self):
        self.registry.configure(0,'echo',self.stdio())
        self.session.busy=True
        with self.assertRaises(ConnectionConflict): self.manage('mount')
        self.session.busy=False
        self.session.fleets.fleets={'b':{'members':[{'execution_state':'unknown'}]}}
        with self.assertRaises(ConnectionConflict): self.manage('mount')
        self.session.fleets.fleets={}
        self.session.weaves=SimpleNamespace(executions={'e':{'execution_state':'unknown'}})
        with self.assertRaises(ConnectionConflict): self.manage('mount')
        self.session.weaves.executions={}
        thread=SimpleNamespace(is_alive=lambda:True)
        self.session.agent.chenshu=SimpleNamespace(lock=threading.Lock(),_threads={'retired':thread})
        with self.assertRaises(ConnectionConflict): self.manage('mount')
        del self.session.agent.chenshu
        self.session.agent.toolbox._mcp=object()
        with self.assertRaises(ConnectionConflict): self.manage('mount')
        self.assertEqual(self.registry.sessions,{})

    def test_failed_operation_receipt_is_unknown_without_repeat(self):
        self.registry.configure(0,'echo',self.stdio())
        self.manage('mount'); self.ready()
        pool=self.registry.sessions['one']['pool']
        with mock.patch.object(pool,'reconnect',side_effect=RuntimeError('private diagnostics')) as operation:
            with self.assertRaises(ConnectionConflict): self.manage('reconnect')
            replay=self.manage('reconnect')
            self.assertTrue(replay['duplicate'])
            self.assertEqual(replay['status'],'unknown')
            self.assertEqual(operation.call_count,1)

    def provider(self):
        owner=self
        self.exchange=[]
        self.mcp_authorizations=[]
        self.mcp_headers=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self):
                self.send_response(405);self.end_headers()
            def do_DELETE(self):
                self.send_response(200);self.end_headers()
            def do_POST(self):
                if self.path == '/mcp':
                    owner.mcp_authorizations.append(self.headers.get('Authorization'))
                    owner.mcp_headers.append(dict(self.headers))
                    msg=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    if 'id' not in msg:
                        self.send_response(202);self.end_headers();return
                    method=msg['method']
                    if method=='initialize':
                        result={'protocolVersion':msg['params']['protocolVersion'],'capabilities':{'tools':{}},'serverInfo':{'name':'local','version':'1.0.0'}}
                    elif method=='tools/list':
                        result={'tools':[{'name':'echo','description':'local echo','inputSchema':{'type':'object','properties':{}}}]}
                    else:
                        result={'content':[{'type':'text','text':'authorized remote result'}]}
                    raw=json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':result}).encode()
                    self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw);return
                value=parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())
                owner.exchange.append(value)
                payload={'access_token':'local-'+'access','token_type':'Bearer','scope':'tools','expires_in':3600}
                raw=json.dumps(payload).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base='http://127.0.0.1:'+str(server.server_port)
        config={'name':'oauth','transport':'http','url':base+'/mcp','oauth':{
            'client_id':'public-client','authorization_endpoint':base+'/authorize','token_endpoint':base+'/token','scope':'tools'}}
        self.registry.configure(self.registry.store.read()['revision'],'oauth',config)

    def authorize(self, *, wrong_state=False):
        job=self.registry.begin_authorization(self.session,'oauth')
        aid=job['authorization_id']
        wait(lambda:self.registry.authorization('oauth',aid)['status']=='pending')
        again=self.registry.begin_authorization(self.session,'oauth')
        self.assertEqual(again['authorization_id'],aid)
        url=self.registry.open_url('oauth',aid)['url']
        fields=parse_qs(urlsplit(url).query)
        self.registry.callback(aid,urlencode({'state':'wrong' if wrong_state else fields['state'][0], 'code':'local-code'}))
        wait(lambda:self.registry.authorization('oauth',aid)['status'] not in {'pending','authorizing'})
        return aid,fields

    def test_real_oauth_pkce_memory_only_and_session_isolation(self):
        self.provider()
        aid,fields=self.authorize()
        self.assertEqual(self.registry.authorization('oauth',aid)['status'],'authorized')
        verifier=self.exchange[0]['code_verifier'][0]
        challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        self.assertEqual(fields['code_challenge'],[challenge])
        self.assertEqual(self.registry.list(self.session)['connections'][0]['oauth_status'],'authorized')
        self.assertEqual(self.registry.list(self.new_session('other'))['connections'][0]['oauth_status'],'unauthorized')
        self.assertNotIn('local-access',self.store.path.read_text())
        self.assertNotIn('local-access',json.dumps(self.registry.list(self.session)))
        with self.assertRaises(ConnectionConflict):self.registry.open_url('oauth',aid)
        with self.assertRaises(ConnectionConflict):self.registry.callback(aid,'state=x&code=x')
        result=self.registry.revoke(self.session,'oauth')
        self.assertTrue(result['revoked']);self.assertFalse(result['remote_revoked'])
        self.assertEqual(self.registry.list(self.session)['connections'][0]['oauth_status'],'unauthorized')

    def test_oauth_credentials_flow_to_real_http_mcp_without_cross_session_share(self):
        self.provider()
        self.authorize()
        self.registry.manage(self.session,'oauth',{'action':'mount','configuration_revision':1},'mount-http')
        wait(lambda:self.registry.list(self.session)['connections'][0]['state']=='ready')
        result=self.session.agent.toolbox._use_tool('mcp__desktop_oauth__echo',{})
        self.assertIn('authorized remote result',result)
        self.assertTrue(self.mcp_authorizations)
        self.assertTrue(all(value=='Bearer local-access' for value in self.mcp_authorizations))
        with self.assertRaises(ConnectionConflict):
            self.registry.manage(self.new_session('other'),'oauth',{'action':'mount','configuration_revision':1},'other')
        with self.assertRaises(ConnectionConflict):self.registry.begin_authorization(self.session,'oauth')
        self.registry.manage(self.session,'oauth',{'action':'remove','configuration_revision':1},'remove-http')
        self.registry.revoke(self.session,'oauth')
        self.assertEqual(self.registry.list(self.session)['connections'][0]['oauth_status'],'unauthorized')

    def test_oauth_invalid_state_no_token_exchange_cancel_expire_close(self):
        self.provider()
        aid,_=self.authorize(wrong_state=True)
        self.assertEqual(self.registry.authorization('oauth',aid)['status'],'failed')
        self.assertEqual(self.exchange,[])
        second=self.registry.begin_authorization(self.session,'oauth')['authorization_id']
        self.registry.cancel('oauth',second)
        wait(lambda:not self.registry.authorizations[second]['thread'].is_alive())
        self.assertEqual(self.registry.authorization('oauth',second)['status'],'cancelled')
        self.registry.authorization_timeout=.02
        third=self.registry.begin_authorization(self.session,'oauth')['authorization_id']
        wait(lambda:self.registry.authorization('oauth',third)['status']=='expired')
        self.registry.authorization_timeout=10
        fourth=self.registry.begin_authorization(self.session,'oauth')['authorization_id']
        self.registry.close_session(self.session.id)
        wait(lambda:not self.registry.authorizations[fourth]['thread'].is_alive())
        self.assertEqual(self.registry.authorization('oauth',fourth)['status'],'cancelled')

    def test_shutdown_does_not_wait_for_registry_lock_or_resurrect_tokens(self):
        from xiaoyu_agent_sdk.oauth import OAuthTokens
        self.provider()
        self.authorize()
        credential=next(iter(self.registry.credentials.values()))
        entered=threading.Event();release=threading.Event()
        def hold():
            with self.registry.lock:
                entered.set();release.wait(3)
        thread=threading.Thread(target=hold);thread.start();self.assertTrue(entered.wait(1))
        try:
            started=time.monotonic()
            self.registry.begin_shutdown()
            self.assertLess(time.monotonic()-started,.2)
            self.assertIsNone(credential['store'].load())
            with self.assertRaises(ConnectionError):
                credential['store'].save(OAuthTokens('late-refresh'))
        finally:
            release.set();thread.join(3)

    def test_config_damaged_or_symlink_is_not_reset(self):
        self.store.path.write_text('{bad')
        with self.assertRaises(ValueError):self.store.read()
        self.store.path.unlink()
        target=self.root/'target';target.write_text('{}')
        self.store.path.symlink_to(target)
        with self.assertRaises(OSError):self.store.read()
        self.assertEqual(target.read_text(),'{}')


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_request_keeps_gate_until_real_mutation_settles(self):
        from labs.desktop.connections_api import _managed
        gate=threading.Event(); entered=threading.Event()
        session=SimpleNamespace(agent=SimpleNamespace(),busy=False)
        def mutate():entered.set();gate.wait(3)
        with mock.patch('xiaoyu.serve_ownership.require_write'):
            task=asyncio.create_task(_managed(session,mutate))
            await asyncio.to_thread(entered.wait,1)
            self.assertTrue(session.management_busy)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
            self.assertTrue(session.management_busy)
            with self.assertRaises(ConnectionConflict):await _managed(session,lambda:None)
            gate.set()
            await session.management_drain
            self.assertFalse(session.management_busy)
