"""Desktop forms execute domain commands without asking a model to interpret them."""
import json
from pathlib import Path
import shutil
import sys
import unittest

from labs.desktop.tests import test_fleet_contract as fleet_tests


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("node") and shutil.which("git"),
                     "macOS, Node and Git required")
class FleetCommandsContract(unittest.TestCase):
    setUp = fleet_tests.FleetContractTests.setUp
    cleanup_children = fleet_tests.FleetContractTests.cleanup_children
    ready = fleet_tests.FleetContractTests.ready
    request = fleet_tests.FleetContractTests.request
    stop = fleet_tests.FleetContractTests.stop
    node = fleet_tests.FleetContractTests.node
    launch = fleet_tests.FleetContractTests.launch

    def test_preview_init_plan_spawn_resume_and_restart_do_not_replay(self):
        write = 'tool_call: ' + json.dumps({"name": "write_file", "arguments": {
            "path": "delivery.txt", "content": "成员交付"}}, ensure_ascii=False) + '\n'
        child, info = self.launch('error: domain commands must not invoke parent model\n',
                                  write + '---\ntext: 已交付文件。\n---\ntext: 已按补充要求复核。\n')
        previous = self.node(info, r'''
assert.equal(health.fleet_command_protocol,1);
const sid=(await send("create",{kind:"create",path:"/session",body:{model:"lab-scripted"}})).session_id;
const base=`/session/${sid}/fleet-commands`;
async function fleetCommand(scope,body){
 const preview=await client.post(base+"/preview",body);
 assert.ok(preview.preview_token);
 const action={kind:"fleet_command",sid,path:base,body:{...body,preview_token:preview.preview_token}};
 const response=await send(scope,action);
 return {response,action,key:outbox.items[scope].key};
}
await client.get(base+"/capabilities");
const init=await fleetCommand("init",{action:"init",mode:"new"});
assert.equal((await approval(sid,"allow")).tool,"chenshu_init");
await waitFor(sid,s=>!s.busy);
let fleet=(await client.get(`/session/${sid}/fleets`)).fleets[0];
assert.ok(fleet.board_id);
await fleetCommand("plan",{action:"plan",board_id:fleet.board_id,missions:[
 {title:"生成交付文件",kind:"build",scope:["delivery.txt"],deps:[]}]});
await waitFor(sid,s=>!s.busy);
fleet=(await client.get(`/session/${sid}/fleets`)).fleets[0];
assert.equal(fleet.missions.length,1);
const spawned=await fleetCommand("spawn",{action:"spawn",board_id:fleet.board_id,name:"writer",kind:"worker",mission_id:fleet.missions[0].id});
await waitFor(sid,s=>!s.busy);
assert.ok((await client.get(`/session/${sid}/fleets`)).fleets[0].members.length,JSON.stringify(await client.get(`/session/${sid}/events`)));
let state=await waitFor(sid,s=>!s.busy && s.pending_approvals.some(p=>p.identity?.member_name==="writer"));
const pending=state.pending_approvals.find(p=>p.identity?.member_name==="writer");
await send("write-approval",{kind:"approval",sid,path:`/session/${sid}/permissions`,body:{request_id:pending.request_id,decision:"allow"}});
async function settled(previousRun=""){
 for(let i=0;i<400;i++){
  const f=(await client.get(`/session/${sid}/fleets`)).fleets[0],m=f.members[0];
  if(m?.execution_state==="settled" && (!previousRun || m.run_id!==previousRun))return f;
  await new Promise(r=>setTimeout(r,20));
 }
 throw new Error("member did not settle");
}
fleet=await settled();const old=fleet.members[0].run_id;
await fleetCommand("resume",{action:"resume",board_id:fleet.board_id,name:"writer",run_id:old,instructions:"复核交付"});
await waitFor(sid,s=>!s.busy);
fleet=await settled(old);
assert.notEqual(fleet.members[0].run_id,old);
const duplicate=await client.post(base,init.action.body,{key:init.key});
assert.equal(duplicate.duplicate,true);assert.equal(duplicate.run_id,init.response.run_id);
console.log(JSON.stringify({sid,base,spawned,turns:(await client.get(`/session/${sid}/status`)).turns,worktree:fleet.missions[0].worktree}));
''')
        worktree = Path(previous["worktree"])
        self.assertEqual((worktree / "delivery.txt").read_text(encoding="utf-8", errors="replace"), "成员交付")
        self.assertFalse((self.workspace / "delivery.txt").exists())
        self.stop(child, info)
        child, info = self.launch('error: restart must not call model\n')
        self.node(info, r'''
const p=input.previous;
await assert.rejects(client.post(p.base,p.spawned.action.body,{key:p.spawned.key}),error=>error.status===409 && error.recorded);
assert.equal((await client.get(`/session/${p.sid}/status`)).turns,p.turns);
const fleet=(await client.get(`/session/${p.sid}/fleets`)).fleets[0];
assert.equal(fleet.recovered,true);
await assert.rejects(client.post(p.base+"/preview",{action:"spawn",board_id:fleet.board_id,name:"another",kind:"worker",mission_id:fleet.missions[0].id}));
console.log(JSON.stringify({ok:true}));
''', previous=previous)
        self.assertTrue(worktree.is_dir())
        self.stop(child, info)
