"""Native backend executes reviewed merges and separately confirmed cleanup."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from labs.desktop.tests import test_fleet_contract as fleet_tests


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("node") and shutil.which("git"),
                     "macOS, Node and Git required")
class FleetMergeContract(unittest.TestCase):
    def setUp(self):
        fleet_tests.FleetContractTests.setUp(self)
        # The shared batch fixture creates an unrelated local agent declaration.
        # Keep the merge checkout clean without weakening the real dirty gate.
        exclude = self.workspace / ".git/info/exclude"
        with exclude.open("a", encoding="utf-8") as stream:
            stream.write("\n.xiaoyu/agents/\n")

    cleanup_children = fleet_tests.FleetContractTests.cleanup_children
    ready = fleet_tests.FleetContractTests.ready
    request = fleet_tests.FleetContractTests.request
    stop = fleet_tests.FleetContractTests.stop
    node = fleet_tests.FleetContractTests.node
    launch = fleet_tests.FleetContractTests.launch

    def test_review_merge_and_separate_cleanup_preserve_branch_and_result(self):
        def tool(name, args):
            return "tool_call: " + json.dumps({"name": name, "arguments": args}, ensure_ascii=False) + "\n---\n"
        script = (tool("write_file", {"path": "delivery.txt", "content": "已评审交付\n"})
                  + tool("bash", {"command": "git add delivery.txt && git commit -qm delivery"})
                  + tool("chenshu_mission", {"status": "completed", "note": "文件已提交"})
                  + "text: 文件已提交并完成交接。\n---\n"
                  + tool("chenshu_review", {"target": "M1", "verdict": "clean", "summary": "已复核交付"})
                  + "text: 评审完成。\n")
        child, info = self.launch("error: GUI commands must not call the parent model\n", script)
        result = self.node(info, r'''
const sid=(await send("create",{kind:"create",path:"/session",body:{model:"lab-scripted"}})).session_id;
const base=`/session/${sid}/fleet-commands`;
async function execute(scope,body,tool="") {
 const preview=await client.post(base+"/preview",body);
 assert.notEqual(preview.can_execute,false,JSON.stringify(preview));
 assert.ok(preview.preview_token);
 const action={kind:"fleet_command",sid,path:base,body:{...body,preview_token:preview.preview_token}};
 const response=await send(scope,action);
 if(tool) assert.equal((await approval(sid,"allow")).tool,tool);
 await waitFor(sid,s=>!s.busy);
 return {response,action,key:outbox.items[scope].key};
}
await execute("init",{action:"init",mode:"new"},"chenshu_init");
let fleet=(await client.get(`/session/${sid}/fleets`)).fleets[0];
const bid=fleet.board_id;
await execute("plan",{action:"plan",board_id:bid,missions:[{title:"生成交付",kind:"build",scope:["delivery.txt"],deps:[]}]});
await execute("writer",{action:"spawn",board_id:bid,name:"writer",kind:"worker",mission_id:"M1"});
async function settle(name) {
 for(let i=0;i<500;i++) {
  const status=await client.get(`/session/${sid}/status`);
  for(const p of status.pending_approvals.filter(p=>p.identity?.member_name===name))
   await send(`approve:${p.request_id}`,{kind:"approval",sid,path:`/session/${sid}/permissions`,body:{request_id:p.request_id,decision:"allow"}});
  const f=(await client.get(`/session/${sid}/fleets`)).fleets[0];
  if(f.members.some(m=>m.name===name && m.execution_state==="settled"))return f;
  await new Promise(r=>setTimeout(r,20));
 }
 throw new Error("member did not settle");
}
fleet=await settle("writer");
const changesPath=`/session/${sid}/fleets/${bid}/missions/M1/changes`;
let changes=await client.get(changesPath);
assert.equal(changes.can_merge,false);
assert.ok(changes.blockers.length);
assert.ok(changes.merge.diff.text.includes("已评审交付"));
await execute("reviewer",{action:"spawn",board_id:bid,name:"reviewer",kind:"reviewer",review_target:"M1"});
fleet=await settle("reviewer");
changes=await client.get(changesPath);
assert.equal(changes.can_merge,true,JSON.stringify(changes));
assert.equal(changes.merge.review.current,true);
const originalWorktree=fleet.missions[0].worktree;
const merged=await execute("merge",{action:"merge",board_id:bid,mission_id:"M1"},"chenshu_merge");
fleet=(await client.get(`/session/${sid}/fleets`)).fleets[0];
assert.equal(fleet.missions[0].status,"merged");
assert.equal(fleet.missions[0].worktree,originalWorktree);
const cleaned=await execute("cleanup",{action:"teardown",board_id:bid});
const replay=await client.post(base,merged.action.body,{key:merged.key});
assert.equal(replay.duplicate,true);assert.equal(replay.run_id,merged.response.run_id);
console.log(JSON.stringify({sid,base,merged,cleaned,worktree:originalWorktree,branch:changes.merge.target_branch}));
''')
        self.assertEqual((self.workspace / "delivery.txt").read_text(encoding="utf-8", errors="replace"), "已评审交付\n")
        self.assertFalse(Path(result["worktree"]).exists())
        self.assertEqual(subprocess.run(["git", "rev-parse", "--verify", result["branch"]],
                                       cwd=self.workspace, capture_output=True).returncode, 0)
        self.stop(child, info)
        child, info = self.launch("error: restart must not execute\n")
        self.node(info, r'''
const p=input.previous;
await assert.rejects(client.post(p.base,p.cleaned.action.body,{key:p.cleaned.key}),error=>error.status===409 && error.recorded);
console.log(JSON.stringify({ok:true}));
''', previous=result)
        self.stop(child, info)
