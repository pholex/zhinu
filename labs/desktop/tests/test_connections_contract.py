"""Native backend process: explicit MCP connection, approval, restart and epoch."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import sys
import unittest

from labs.desktop.tests import test_weave_actions_contract as weave_contract
from tests.test_mcp import FAKE_SERVER


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('node') and shutil.which('git'),
                     'macOS backend, Node and Git are required')
class ConnectionsContractTests(unittest.TestCase):
    # Reuse the process driver, without inheriting the batch test cases.
    setUp = weave_contract.WeaveActionsContractTests.setUp
    cleanup_children = weave_contract.WeaveActionsContractTests.cleanup_children
    ready = weave_contract.WeaveActionsContractTests.ready
    request = weave_contract.WeaveActionsContractTests.request
    stop = weave_contract.WeaveActionsContractTests.stop
    launch = weave_contract.WeaveActionsContractTests.launch
    node = weave_contract.WeaveActionsContractTests.node

    def test_explicit_stdio_connection_approval_and_restart_never_auto_mount(self):
        starts = self.workspace / 'mcp-starts.txt'
        calls = self.workspace / 'mcp-calls.txt'
        fixture = self.workspace / 'fixture_mcp.py'
        prefix = ('from pathlib import Path\nimport os\n'
                  f'with Path({str(starts)!r}).open("a", encoding="utf-8") as marker:\n'
                  '    marker.write(str(os.getpid()) + "\\n")\n')
        script = FAKE_SERVER.replace('elif method == "tools/call":',
            'elif method == "tools/call":\n'
            f'        with Path({str(calls)!r}).open("a", encoding="utf-8") as marker:\n'
            '            marker.write("called\\n")')
        fixture.write_text(prefix + script, encoding='utf-8')
        model = ('tool_call: {"name":"search_tool","arguments":{"query":"echo"}}\n---\n'
                 'tool_call: {"name":"use_tool","arguments":{"tool_name":"mcp__desktop_echo__echo",'
                 '"tool_input":{"text":"native approved result"}}}\n---\n'
                 'text: 已使用连接完成回显。\n')
        child, info = self.launch(model)
        first = self.node(info, r'''
import {existsSync} from "node:fs";
const config=await client.post("/desktop/connections",{revision:0,connection_id:"echo",definition:{
  name:"本机回显",transport:"stdio",command:input.python,args:[input.fixture]}});
const revision=config.connections.find(c=>c.id==="echo").revision;
const created=await send("connection-create",{kind:"create",path:"/session",body:{model:"lab-scripted"}}),sid=created.session_id;
const initial=await client.get(`/session/${sid}/connections`);
assert.equal(initial.instance_id,health.instance_id);
assert.equal(initial.connections[0].mounted,false);
assert.equal(existsSync(input.starts),false,"configuration must not start a process");
const path=`/session/${sid}/connections/echo`,body={action:"mount",configuration_revision:revision},key="native-explicit-mount";
await client.post(path,body,{key,instance:health.instance_id});
const duplicate=await client.post(path,body,{key,instance:health.instance_id});
assert.equal(duplicate.duplicate,true);
let connection;
for(let i=0;i<500;i++){
  connection=(await client.get(`/session/${sid}/connections`)).connections[0];
  if(connection.state==="ready")break;
  await new Promise(resolve=>setTimeout(resolve,20));
}
assert.equal(connection.state,"ready");
assert.ok(connection.tools.some(t=>t.name==="echo"));
await send(`${sid}:prompt`,{kind:"prompt",sid,path:`/session/${sid}/prompt_async`,body:{text:"调用回显连接完成任务"}});
const pending=await waitFor(sid,state=>state.pending_approvals.length>0);
assert.match(JSON.stringify(pending.pending_approvals[0]),/echo/);
assert.equal(existsSync(input.calls),false,"MCP invocation must wait for normal approval");
await approval(sid,"allow");
const final=await waitFor(sid,state=>!state.busy);
assert.equal(final.detail,"finished");
const events=await client.get(`/session/${sid}/events?from=1&limit=200`);
assert.match(JSON.stringify(events),/echo: native approved result/);
const history=await client.get(`/session/${sid}/history?cursor=0&limit=100`);
assert.ok(history.messages.some(m=>m.role==="assistant"&&m.content.includes("已使用连接")));
console.log(JSON.stringify({sid,path,body,key,instance:health.instance_id,history:history.messages}));
''', python=sys.executable, fixture=str(fixture), starts=str(starts), calls=str(calls))
        self.assertEqual(len(starts.read_text(encoding='utf-8').splitlines()), 1)
        self.assertEqual(calls.read_text(encoding='utf-8').splitlines(), ['called'])
        self.stop(child)

        child, info = self.launch('error: must not run on connection restoration\n')
        self.node(info, r'''
import {readFileSync} from "node:fs";
const {sid,path,body,key,instance}=input.first;
assert.notEqual(health.instance_id,instance);
const catalog=await client.get("/desktop/connections");
assert.equal(catalog.connections[0].id,"echo");
const state=await client.get(`/session/${sid}/connections`);
assert.equal(state.instance_id,health.instance_id);
assert.equal(state.connections[0].mounted,false);
assert.equal(state.connections[0].state,"unmounted");
assert.deepEqual(state.connections[0].tools,[]);
const history=await client.get(`/session/${sid}/history?cursor=0&limit=100`);
assert.deepEqual(history.messages,input.first.history);
const before=await client.get(`/session/${sid}/status`);
// Use the new service token deliberately; stale-instance rejection is independent
// of token rotation and cannot be mistaken for an authentication failure.
await assert.rejects(client.post(path,body,{key,instance}),error=>error.status===409);
await new Promise(resolve=>setTimeout(resolve,150));
assert.equal((await client.get(`/session/${sid}/connections`)).connections[0].mounted,false);
assert.equal(readFileSync(input.starts,"utf8").trim().split("\n").length,1);
assert.equal((await client.get(`/session/${sid}/status`)).turns,before.turns);
// A new explicit action in this epoch can mount the saved definition.
await client.post(path,body,{key:"native-remount-new-epoch",instance:health.instance_id});
let row;
for(let i=0;i<500;i++){
  row=(await client.get(`/session/${sid}/connections`)).connections[0];
  if(row.state==="ready")break;
  await new Promise(resolve=>setTimeout(resolve,20));
}
assert.equal(row.state,"ready");
assert.equal(readFileSync(input.starts,"utf8").trim().split("\n").length,2);
await client.post(path,{action:"remove",configuration_revision:body.configuration_revision},{key:"native-remove",instance:health.instance_id});
assert.equal((await client.get(`/session/${sid}/connections`)).connections[0].mounted,false);
assert.equal((await client.get(`/session/${sid}/status`)).turns,before.turns);
console.log(JSON.stringify({ok:true}));
''', first=first, starts=str(starts))
        self.assertEqual(calls.read_text(encoding='utf-8').splitlines(), ['called'])
        self.stop(child)
