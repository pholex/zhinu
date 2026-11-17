"""Real desktop batch commands: approvals, selective recovery and durable deduplication."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from labs.desktop.tests import test_macos_backend as backend_tests

ROOT = Path(__file__).resolve().parents[3]
CLIENT = (ROOT / "labs/desktop/web/client.mjs").as_uri()
LONG = "已核对本项文件、执行记录与交付范围。" * 20


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("node") and shutil.which("git"),
                     "macOS backend, Node and Git are required")
class WeaveActionsContractTests(unittest.TestCase):
    cleanup_children = backend_tests.DesktopBackendTests.cleanup_children
    ready = backend_tests.DesktopBackendTests.ready
    request = backend_tests.DesktopBackendTests.request
    stop = backend_tests.DesktopBackendTests.stop

    def setUp(self):
        backend_tests.DesktopBackendTests.setUp(self)
        self.root = self.root.resolve()
        self.workspace = self.root / "batch-workspace"
        self.workspace.mkdir()
        (self.workspace / "README.md").write_text("isolated scripted batch\n", encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root),
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
        for args in (["init", "-q"], ["add", "README.md"],
                     ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                      "commit", "-qm", "fixture"]):
            subprocess.run([shutil.which("git"), *args], cwd=self.workspace, env=env,
                           capture_output=True, check=True)
        agents = self.workspace / ".xiaoyu/agents"
        agents.mkdir(parents=True)
        (agents / "writer.toml").write_text(
            'description = "隔离文件交付测试"\nsystem_prompt = "工作区 {workspace}，完成指定任务。"\n'
            'tools = ["read_file", "write_file"]\nmax_iterations = 20\n', encoding="utf-8")

    def launch(self, script):
        path = self.root / "script.txt"
        path.write_text(script, encoding="utf-8")
        env = {key: os.environ[key] for key in ("PATH", "TMPDIR") if key in os.environ}
        env.update(HOME=str(self.root), USERPROFILE=str(self.root), XDG_CONFIG_HOME=str(self.root / "config"),
                   APPDATA=str(self.root / "config"), PYTHONPATH=str(ROOT), PYTHONNOUSERSITE="1",
                   XIAOYU_SCRIPTED_SCRIPTS=str(path), XIAOYU_SUGGESTIONS="0", XIAOYU_ENABLE_AGENTS="1",
                   XIAOYU_QIXIANG_CONCURRENCY="1", XIAOYU_ENABLE_ASKPASS="0",
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        for feature in ("MCP", "PLUGINS", "SKILLS", "HOOKS", "EXPLORE", "WEB_SEARCH", "X_SEARCH",
                        "DEEP_RESEARCH", "BROWSER", "CHENSHU"):
            env[f"XIAOYU_ENABLE_{feature}"] = "0"
        errors = tempfile.TemporaryFile()
        child = subprocess.Popen([sys.executable, "-P", "-u", "-m", "labs.desktop.macos.backend",
                                  "--workspace", str(self.workspace)], cwd=ROOT, env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        self.children.append((child, errors))
        return child, self.ready(child)

    @staticmethod
    def write(path, content):
        return "tool_call: " + json.dumps({"name": "write_file", "arguments": {
            "path": path, "content": content}}, ensure_ascii=False) + "\n"

    def node(self, info, source, **data):
        prelude = f'import {{ReadClient,LocalState,Outbox,stateOf}} from {json.dumps(CLIENT)};\n' + r'''
import assert from "node:assert/strict";
let raw="";for await(const chunk of process.stdin)raw+=chunk;
const input=JSON.parse(raw),info=input.info;
const client=new ReadClient(new URL(info.url).origin,info.token);
const health=await client.get("/health"),local=await LocalState.load(client);
const outbox=new Outbox(local.storage,"native");
async function waitFor(sid,predicate){
  for(let i=0;i<750;i++){
    const state=await client.get(`/session/${sid}/status`);
    if(predicate(state))return state;
    await new Promise(resolve=>setTimeout(resolve,20));
  }
  throw new Error("batch did not reach expected state");
}
async function send(scope,action,transport=client){
  outbox.prepare(scope,{...action,instance:health.instance_id});
  const result=await outbox.send(scope,transport);await local.flush();return result;
}
async function approval(sid,decision){
  const state=await waitFor(sid,s=>s.pending_approvals.length>0),request=state.pending_approvals[0];
  assert.equal(stateOf(state).label,"等待审批");
  await send(`${sid}:approval:${request.request_id}`,{kind:"approval",sid,path:`/session/${sid}/permissions`,
    body:{request_id:request.request_id,decision}});
  return request;
}
async function command(sid,body,transport=client){
  const preview=await client.post(`/session/${sid}/weaves/preview`,body);
  assert.ok(preview.preview_token);
  const action={kind:"weave",sid,path:`/session/${sid}/weaves`,body:{...body,preview_token:preview.preview_token}};
  return send(`${sid}:weave`,action,transport);
}
'''
        observed = subprocess.run([shutil.which("node"), "--input-type=module", "-e", prelude + source],
                                  input=json.dumps({"info": info, **data}), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=45, check=False)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        return json.loads(observed.stdout)

    def test_selective_resume_after_restart_keeps_results_and_never_replays_unknown_submission(self):
        child, info = self.launch(self.write("first.txt", "first delivery")
                                  + f"---\ntext: {LONG}\n---\n"
                                  + self.write("partial.txt", "incomplete delivery")
                                  + "---\nerror: scripted item failure\n")
        first = self.node(info, r'''
const created=await send("create",{kind:"create",path:"/session",body:{model:"lab-scripted"}}),sid=created.session_id;
await command(sid,{spec:"writer",prompt_template:"完成{{item}}",items:["甲","乙"]});
await approval(sid,"allow");
await approval(sid,"allow");
await waitFor(sid,state=>!state.busy);
const page=await client.get(`/session/${sid}/weaves?cursor=0&limit=20`);
assert.equal(page.batches.length,1);
const batch=page.batches[0];
assert.deepEqual(batch.items.map(item=>item.status),["completed","failed"]);
assert.equal(batch.items[0].artifacts[0].path,"first.txt");
assert.equal("artifact_previews" in batch.items[0],false);
const artifact=batch.items[0].artifacts[0],artifactPath=`/session/${sid}/artifacts/${artifact.id}?batch_id=${batch.batch_id}&item_id=${batch.items[0].id}`;
const preview=await client.get(artifactPath);
assert.equal(preview.version,"delivered");assert.equal(preview.delivered.text,"first delivery");
assert.equal(preview.diff.available,true);
assert.ok(batch.items[1].run_id);
outbox.forget("create");outbox.forget(`${sid}:weave`);await local.flush();
console.log(JSON.stringify({sid,batch_id:batch.batch_id,item_id:batch.items[1].id,first:batch,artifactPath}));
''')
        item = first["first"]["items"][0]
        self.assertEqual((Path(item["workspace"]) / "first.txt").read_text(encoding="utf-8"), "first delivery")
        (Path(item["workspace"]) / "first.txt").write_text("manual edit after delivery", encoding="utf-8")
        self.node(info, r'''
const preview=await client.get(input.first.artifactPath);
assert.equal(preview.version,"current_changed");
assert.equal(preview.delivered.text,"first delivery");
assert.equal(preview.current.text,"manual edit after delivery");
assert.equal(preview.diff.available,true);
assert.ok(!preview.diff.text.includes("manual edit after delivery"),"diff must describe the delivered change, not later edits");
console.log(JSON.stringify({ok:true}));
''', first=first)
        self.stop(child)

        child, info = self.launch(self.write("resumed.txt", "resumed delivery") + f"---\ntext: {LONG}\n")
        second = self.node(info, r'''
const {sid,batch_id,item_id}=input.first;
const original=await client.get(`/session/${sid}/weaves?cursor=0&limit=20`);
const recovered=structuredClone(input.first.first);
for(const item of recovered.items) { if(item.execution_id) { item.recovered=true; assert.equal(item.live,false); } }
assert.deepEqual(original.batches[0],recovered);
let dropped=false;
const lossy=new ReadClient(new URL(info.url).origin,info.token,async(url,options)=>{
  const response=await fetch(url,options);
  if(!dropped&&options.method==="POST"&&new URL(url).pathname===`/session/${sid}/weaves`){
    dropped=true;await response.arrayBuffer();throw new TypeError("simulated response loss");
  }
  return response;
});
await assert.rejects(command(sid,{source_batch_id:batch_id,item_ids:[item_id],instruction:"接着完成剩余交付。"},lossy));
const scope=`${sid}:weave`,pending=outbox.items[scope],key=pending.key,body=structuredClone(pending.body);
assert.equal(pending.state,"unknown");await local.flush();
await outbox.send(scope,client);await local.flush();
assert.equal(outbox.items[scope].key,key);
assert.deepEqual(outbox.items[scope].body,body);
await approval(sid,"allow");await waitFor(sid,state=>!state.busy);
const page=await client.get(`/session/${sid}/weaves?cursor=0&limit=20`);
assert.equal(page.batches.length,2);
const latest=page.batches.find(batch=>batch.batch_id!==batch_id);
assert.equal(latest.items.length,1);
assert.equal(latest.items[0].status,"completed");
assert.equal(latest.items[0].resume_from,input.first.first.items[1].run_id);
assert.equal(latest.items[0].artifacts[0].path,"resumed.txt");
const artifact=latest.items[0].artifacts[0];
const artifactView=await client.get(`/session/${sid}/artifacts/${artifact.id}?batch_id=${latest.batch_id}&item_id=${latest.items[0].id}`);
assert.equal(artifactView.delivered.available,true);
assert.equal(artifactView.delivered.text,"resumed delivery");
assert.deepEqual(page.batches.find(batch=>batch.batch_id===batch_id),recovered);
console.log(JSON.stringify({sid,scope,key,body,batches:page.batches}));
''', first=first)
        self.stop(child)

        child, info = self.launch("error: must never execute after a recorded retry\n")
        self.node(info, r'''
const {sid,scope,key,body}=input.second;
const before=await client.get(`/session/${sid}/weaves?cursor=0&limit=20`);
const recovered=structuredClone(input.second.batches);
for(const batch of recovered) for(const item of batch.items) if(item.execution_id) { item.recovered=true; assert.equal(item.live,false); }
assert.deepEqual(before.batches,recovered);
const receipt=outbox.items[scope];assert.equal(receipt.key,key);assert.deepEqual(receipt.body,body);
receipt.state="unknown";outbox.save();await local.flush();
await assert.rejects(outbox.send(scope,client),error=>error.status===409&&error.recorded===true);
await local.flush();assert.equal(outbox.items[scope].state,"recorded");
const after=await client.get(`/session/${sid}/weaves?cursor=0&limit=20`);
assert.deepEqual(after.batches,before.batches);
assert.equal((await client.get(`/session/${sid}/status`)).busy,false);
const preview=await client.get(input.first.artifactPath);
assert.equal(preview.version,"current_changed");assert.equal(preview.delivered.text,"first delivery");
assert.equal(preview.current.text,"manual edit after delivery");
console.log(JSON.stringify({ok:true}));
''', second=second, first=first)
        self.stop(child)

    def test_denied_write_and_explicit_stop_preserve_unfinished_item_status(self):
        child, info = self.launch(self.write("denied.txt", "must not exist")
                                  + f"---\ntext: 写入被拒绝，没有生成文件。{LONG}\n---\n"
                                  + self.write("stopped.txt", "must not exist")
                                  + f"---\ntext: {LONG}\n---\ntext: queued must not run\n")
        result = self.node(info, r'''
const created=await send("create",{kind:"create",path:"/session",body:{model:"lab-scripted"}}),sid=created.session_id;
await command(sid,{spec:"writer",prompt_template:"处理{{item}}",items:["拒绝写入","停止写入","仍在排队"]});
await approval(sid,"deny");
const pending=await waitFor(sid,state=>state.pending_approvals.length>0);
await send(`${sid}:abort`,{kind:"abort",sid,path:`/session/${sid}/abort`,body:{turn:pending.in_flight.turn}});
await waitFor(sid,state=>!state.busy);
const page=await client.get(`/session/${sid}/weaves?cursor=0&limit=20`),batch=page.batches[0];
assert.equal(batch.final,true);assert.equal(batch.cancelled,true);
assert.equal(batch.items[0].status,"completed");
assert.match(batch.items[0].answer,/拒绝/);assert.deepEqual(batch.items[0].artifacts,[]);
assert.deepEqual(batch.items.slice(1).map(item=>item.status),["aborted","aborted"]);
assert.ok(batch.items.every(item=>item.artifacts.length===0));
console.log(JSON.stringify({items:batch.items}));
''')
        for item in result["items"]:
            if item.get("workspace"):
                self.assertFalse((Path(item["workspace"]) / "denied.txt").exists())
                self.assertFalse((Path(item["workspace"]) / "stopped.txt").exists())
        self.stop(child)

    def test_fixture_exposes_live_specs_for_new_and_selected_resume(self):
        child = backend_tests.DesktopBackendTests.start(self, "--fixture")
        info = self.ready(child)
        self.node(info, r'''
const sessions=(await client.get("/session")).sessions;
assert.equal(sessions.length,4);
let sid,source;
for(const session of sessions){
  const batches=(await client.get(`/session/${session.session_id}/weaves?cursor=0&limit=20`)).batches;
  if(batches.length){sid=session.session_id;source=batches[0];break;}
}
assert.ok(sid);
const capabilities=await client.get(`/session/${sid}/weaves/capabilities`);
assert.equal(capabilities.can_start,true);
assert.equal(capabilities.specs[0].name,"reviewer");
assert.ok(capabilities.batches[0].items.every(item=>item.can_resume));
await command(sid,{spec:"reviewer",prompt_template:"核查{{item}}",items:["范围","文件"]});
await waitFor(sid,state=>!state.busy);
outbox.forget(`${sid}:weave`);await local.flush();
await command(sid,{source_batch_id:source.batch_id,item_ids:[source.items[0].id],instruction:"再核查这项交付范围。"});
await waitFor(sid,state=>!state.busy);
const batches=(await client.get(`/session/${sid}/weaves?cursor=0&limit=20`)).batches;
assert.equal(batches.length,3);
assert.ok(batches.every(batch=>batch.final&&batch.items.every(item=>item.status==="completed")));
assert.equal(batches.filter(batch=>batch.items.length===1)[0].items[0].resume_from,source.items[0].run_id);
console.log(JSON.stringify({ok:true}));
''')
        self.stop(child)


if __name__ == "__main__":
    unittest.main()
