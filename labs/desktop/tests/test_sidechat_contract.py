"""Actual side-conversation controller with durable native service and scripted model."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
from labs.desktop.tests import test_weave_actions_contract as helpers

ROOT = Path(__file__).resolve().parents[3]
SIDE = (ROOT/'labs/desktop/web/sidechat.mjs').as_uri()
DOM = '''
globalThis.document={activeElement:null,createElement:()=>{const listeners={},input={focus(){},setSelectionRange(){},selectionStart:0,selectionEnd:0};return{className:'',innerHTML:'',isConnected:false,listeners,contains:()=>false,querySelector:()=>input,addEventListener:(n,f)=>listeners[n]=f};}};
const conn={client,localState:local,outbox,health,controller:new AbortController()};
const sideOptions={id:'native-side',context:()=>({conn,workspace:health.root}),openMain:()=>{}};
'''


@unittest.skipUnless(sys.platform=='darwin' and shutil.which('node') and shutil.which('git'), 'native backend and Node')
class SideChatContractTests(unittest.TestCase):
    setUp = helpers.WeaveActionsContractTests.setUp
    cleanup_children = helpers.WeaveActionsContractTests.cleanup_children
    ready = helpers.WeaveActionsContractTests.ready
    request = helpers.WeaveActionsContractTests.request
    stop = helpers.WeaveActionsContractTests.stop
    node = helpers.WeaveActionsContractTests.node

    def launch(self, script):
        original = helpers.subprocess.Popen
        def launch(*args, **kwargs):
            kwargs['env'] = {**kwargs['env'], 'XIAOYU_MODEL':'lab-scripted'}
            return original(*args, **kwargs)
        with mock.patch.object(helpers.subprocess, 'Popen', side_effect=launch):
            return helpers.WeaveActionsContractTests.launch(self, script)

    def test_unknown_create_retries_after_empty_session_expires_without_duplicate_approval_or_run(self):
        child, info = self.launch('text: must not execute before explicit recovery\n')
        first = self.node(info, f'import {{createSideConversation,sideConversationEntries}} from {json.dumps(SIDE)};\n'+DOM+r'''
const side=createSideConversation(sideOptions);await side.ready;
side.node.listeners.input({target:{tagName:'TEXTAREA',value:'为侧边讨论写一份说明'}});await local.flush();
const original=client.post.bind(client);let lose=true;
client.post=async(path,body,options)=>{const result=await original(path,body,options);if(path==='/session'&&lose){lose=false;throw new Error('fixture lost create receipt');}return result;};
await side.send();await local.flush();
assert.equal(side.session(),'');assert.equal(sideConversationEntries(local)[0].pending,true);
const pending=outbox.items['side:native-side:create'];assert.equal(pending.state,'unknown');
const sessions=(await client.get('/session')).sessions;assert.equal(sessions.length,1);assert.equal(sessions[0].turns,0);
console.log(JSON.stringify({sid:sessions[0].session_id,key:pending.key}));side.dispose();
''')
        self.stop(child)
        script = helpers.WeaveActionsContractTests.write('side-result.txt', 'side discussion result')+'---\ntext: 侧边讨论成果已经完成。\n'
        child, info = self.launch(script)
        self.node(info, f'import {{createSideConversation,sideConversationEntries}} from {json.dumps(SIDE)};\n'+DOM+r'''
assert.equal(sideConversationEntries(local)[0].id,'native-side');
let side=createSideConversation(sideOptions);await side.ready;
assert.equal((await client.get('/session')).sessions.length,0);
await assert.rejects(client.get(`/session/${input.first.sid}/status`),error=>error.status===404);
assert.equal(outbox.items['side:native-side:create'].key,input.first.key);
const original=client.post.bind(client);let lose=true;
client.post=async(path,body,options)=>{const result=await original(path,body,options);if(path.endsWith('/prompt_async')&&lose){lose=false;throw new Error('fixture lost prompt receipt');}return result;};
await side.send();const recoveredSid=side.session();assert.ok(recoveredSid);assert.notEqual(recoveredSid,input.first.sid);
await waitFor(side.session(),state=>state.pending_approvals.length>0);await side.refresh();
assert.match(side.node.innerHTML,/需要审批或回答/);assert.equal(outbox.items['side:native-side:prompt'].state,'unknown');
await side.send();assert.equal(outbox.items['side:native-side:prompt'],undefined);
await approval(side.session(),'allow');await waitFor(side.session(),state=>!state.busy);await side.refresh();
assert.match(side.node.innerHTML,/侧边讨论成果已经完成/);
const runs=await client.get(`/session/${side.session()}/runs?cursor=0&limit=100`);assert.equal(runs.runs.length,1);
assert.equal((await client.get('/session')).sessions.length,1);
side.dispose();side=createSideConversation(sideOptions);await side.ready;
assert.equal(side.session(),recoveredSid);assert.match(side.node.innerHTML,/侧边讨论成果已经完成/);
assert.equal((await client.get(`/session/${side.session()}/runs?cursor=0&limit=100`)).runs.length,1);
console.log(JSON.stringify({sid:side.session(),runs:1}));side.dispose();
''', first=first)
        self.assertEqual((self.workspace/'side-result.txt').read_text(), 'side discussion result')
        self.stop(child)
