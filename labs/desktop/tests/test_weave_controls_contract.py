"""Per-item interaction uses actual child agents and durable desktop receipts."""
from pathlib import Path
import shutil
import sys
import unittest

from labs.desktop.tests import test_weave_actions_contract as weave_tests


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("node") and shutil.which("git"),
                     "macOS, Node and Git required")
class WeaveControlsContract(unittest.TestCase):
    setUp = weave_tests.WeaveActionsContractTests.setUp
    cleanup_children = weave_tests.WeaveActionsContractTests.cleanup_children
    ready = weave_tests.WeaveActionsContractTests.ready
    request = weave_tests.WeaveActionsContractTests.request
    stop = weave_tests.WeaveActionsContractTests.stop
    node = weave_tests.WeaveActionsContractTests.node
    launch = weave_tests.WeaveActionsContractTests.launch
    write = staticmethod(weave_tests.WeaveActionsContractTests.write)

    def test_targeted_input_then_stop_preserves_sibling_and_restart_receipts(self):
        child, info = self.launch(self.write("first.txt", "保留本项已写成果") + "---\n"
                                  + self.write("not-written.txt", "停止前等待审批") + "---\n"
                                  + self.write("sibling.txt", "兄弟任务继续完成") + "---\n"
                                  + f"text: {weave_tests.LONG}\n")
        result = self.node(info, r'''
const sid=(await send("create",{kind:"create",path:"/session",body:{model:"lab-scripted"}})).session_id;
await command(sid,{spec:"writer",prompt_template:"完成{{item}}",items:["第一项","第二项"]});
let state=await waitFor(sid,s=>s.pending_approvals.length>0);
let pending=state.pending_approvals[0];
assert.equal(pending.identity.kind,"qixiang");
const identity=pending.identity;
assert.ok(identity.execution_id);assert.ok(identity.batch_id);assert.ok(identity.item_id);
const itemPath=`/session/${sid}/weaves/${encodeURIComponent(identity.batch_id)}/items/${encodeURIComponent(identity.item_id)}`;
const message={kind:"weave_message",sid,path:itemPath+"/messages",body:{execution_id:identity.execution_id,text:"保留当前结果，继续前复核。"}};
let dropped=false;
const lossy=new ReadClient(new URL(info.url).origin,info.token,async(url,options)=>{
 const response=await fetch(url,options);
 if(!dropped && options.method==="POST" && url.endsWith("/messages")){
  dropped=true;await response.arrayBuffer();throw new TypeError("response lost");
 }
 return response;
});
await assert.rejects(send("item-message",message,lossy));
const key=outbox.items["item-message"].key;
const queued=(await outbox.send("item-message",client)).operation;await local.flush();
assert.equal(outbox.items["item-message"].key,key);
assert.equal(queued.status,"queued");
await send("allow-first",{kind:"approval",sid,path:`/session/${sid}/permissions`,body:{request_id:pending.request_id,decision:"allow"}});
state=await waitFor(sid,s=>s.pending_approvals.some(p=>p.request_id!==pending.request_id));
pending=state.pending_approvals.find(p=>p.request_id!==pending.request_id);
assert.equal(pending.identity.execution_id,identity.execution_id);
const operationPath=`/session/${sid}/weave-operations/${queued.operation_id}`;
let operation=(await client.get(operationPath)).operation;
assert.equal(operation.status,"accepted");
const stopped=(await send("item-stop",{kind:"weave_stop",sid,path:itemPath+"/stop",body:{execution_id:identity.execution_id}})).operation;
assert.ok(["requested","settled"].includes(stopped.status),JSON.stringify(stopped));
state=await waitFor(sid,s=>s.pending_approvals.some(p=>p.identity?.item_id!==identity.item_id));
const sibling=state.pending_approvals.find(p=>p.identity?.item_id!==identity.item_id);
assert.equal(sibling.identity.kind,"qixiang");
assert.notEqual(sibling.identity.execution_id,identity.execution_id);
await send("allow-sibling",{kind:"approval",sid,path:`/session/${sid}/permissions`,body:{request_id:sibling.request_id,decision:"allow"}});
await waitFor(sid,s=>!s.busy);
const batch=(await client.get(`/session/${sid}/weaves`)).batches.find(b=>b.batch_id===identity.batch_id);
assert.equal(batch.items[1].status,"completed");
assert.notEqual(batch.items[0].status,"completed");
operation=(await client.get(`/session/${sid}/weave-operations/${stopped.operation_id}`)).operation;
assert.equal(operation.status,"settled");
console.log(JSON.stringify({sid,identity,message,key,operationPath,workspace:identity.workspace,siblingWorkspace:sibling.identity.workspace}));
''')
        self.assertTrue((Path(result["workspace"]) / "first.txt").is_file())
        self.assertFalse((Path(result["workspace"]) / "not-written.txt").exists())
        self.assertTrue((Path(result["siblingWorkspace"]) / "sibling.txt").is_file())
        self.stop(child)
        child, info = self.launch("error: restart must not call a model\n")
        self.node(info, r'''
const p=input.previous;
const op=(await client.get(p.operationPath)).operation;assert.equal(op.status,"accepted");
const replay=(await client.post(p.message.path,p.message.body,{key:p.key})).operation;
assert.equal(replay.operation_id,op.operation_id);
assert.equal(replay.status,"accepted");
await assert.rejects(client.post(p.message.path,p.message.body,{key:"new-attempt-after-restart"}));
console.log(JSON.stringify({ok:true}));
''', previous=result)
        self.stop(child)
