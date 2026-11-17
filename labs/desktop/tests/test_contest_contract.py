"""Real desktop comparisons keep recommendation, human choice and files separate."""
import json
from pathlib import Path
import shutil
import sys
import unittest

from labs.desktop.tests import test_macos_backend as backend_tests
from labs.desktop.tests import test_weave_actions_contract as weave_tests


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("node") and shutil.which("git"),
                     "macOS backend, Node and Git are required")
class ContestContractTests(unittest.TestCase):
    setUp = weave_tests.WeaveActionsContractTests.setUp
    cleanup_children = backend_tests.DesktopBackendTests.cleanup_children
    ready = backend_tests.DesktopBackendTests.ready
    request = backend_tests.DesktopBackendTests.request
    stop = backend_tests.DesktopBackendTests.stop
    launch = weave_tests.WeaveActionsContractTests.launch
    node = weave_tests.WeaveActionsContractTests.node
    write = staticmethod(weave_tests.WeaveActionsContractTests.write)

    def test_human_override_unknown_reply_restart_and_candidate_artifact_identity(self):
        long = weave_tests.LONG
        script = (self.write("result.md", "# First delivery\n") + f"---\ntext: First candidate. {long}\n---\n"
                  + self.write("result.md", "# Second delivery\n") + f"---\ntext: Second candidate. {long}\n"
                  + '---\ntext: "胜者：#2\\n理由：第二项更便于核对。"\n')
        child, info = self.launch(script)
        first = self.node(info, r'''
const created=await send("create",{kind:"create",path:"/session",body:{model:"lab-scripted"}}),sid=created.session_id;
const path=`/session/${sid}/contests`;
assert.equal(health.contest_protocol,1);
assert.equal((await client.get(path+"/capabilities")).can_start,true);
const body={spec:"writer",task:"分别交付一份文件方案",models:["candidate-a","candidate-b"],judge_model:"judge",criteria:"交付可核验"};
const preview=await client.post(path+"/preview",body);
await send("contest",{kind:"contest",sid,path,body:{...body,preview_token:preview.preview_token}});
await approval(sid,"allow");await approval(sid,"allow");
await waitFor(sid,s=>!s.busy);
const contest=(await client.get(path)).contests[0];
assert.deepEqual(contest.candidates.map(c=>c.status),["completed","completed"]);
const [a,b]=contest.candidates;
assert.equal(contest.judge.recommendation_id,b.id);
assert.equal(contest.selection,null);
const artifact=a.artifacts[0];assert.equal("artifact_previews" in a,false);
const query=`?contest_id=${contest.contest_id}&candidate_id=${a.id}`;
const artifactPath=`/session/${sid}/artifacts/${artifact.id}`;
assert.equal((await client.get(artifactPath+query)).delivered.text,"# First delivery\n");
await assert.rejects(client.get(artifactPath+`?contest_id=${contest.contest_id}&candidate_id=${b.id}`),e=>e.status===404);
await assert.rejects(client.get(artifactPath+`?contest_id=${contest.contest_id}`),e=>e.status===400);
await assert.rejects(client.get(artifactPath+`?contest_id=${contest.contest_id}&candidate_id=${a.id}&run_id=${contest.run_id}`),e=>e.status===400);
const located=await fetch(new URL(info.url).origin+artifactPath+`/location?contest_id=${contest.contest_id}&candidate_id=${a.id}`,{headers:{Authorization:`Bearer ${info.token}`}});
assert.equal(located.status,200);
const location=await located.json();
assert.equal(location.workspace,a.workspace);
assert.equal(location.path,a.workspace+"/result.md");
let dropped=false;
const transport=new ReadClient(new URL(info.url).origin,info.token,async(url,options)=>{
  const response=await fetch(url,options);
  if(!dropped&&options.method==="POST"&&url.endsWith("/selection")){dropped=true;throw new TypeError("lost accepted reply");}
  return response;
});
await assert.rejects(send("selection",{kind:"selection",sid,contestId:contest.contest_id,path:path+`/${contest.contest_id}/selection`,
 body:{candidate_id:a.id,reason:"采用更小范围的第一项",expected_revision:0}},transport));
assert.equal(outbox.items.selection.state,"unknown");
const chosen=(await client.get(path)).contests[0];
assert.equal(chosen.selection.candidate_id,a.id);
assert.equal(chosen.judge.recommendation_id,b.id);
console.log(JSON.stringify({sid,contest_id:contest.contest_id,first:a.id,second:b.id,
 artifact_id:artifact.id,workspace:a.workspace,selection:chosen.selection}));
''')
        artifact = Path(first["workspace"]) / "result.md"
        self.assertTrue(artifact.resolve().is_relative_to(self.root))
        self.assertFalse((self.workspace / "result.md").exists(), "adoption must not merge candidate files")
        artifact.write_text("# Later manual change\n", encoding="utf-8")
        self.stop(child, info)
        child, info = self.launch("error: selection must not execute a model\n")
        second = self.node(info, r'''
const {sid,contest_id,first,second,artifact_id}=input.previous;
const path=`/session/${sid}/contests`, selectionPath=path+`/${contest_id}/selection`;
assert.equal(outbox.items.selection.state,"unknown");
const duplicate=await outbox.send("selection",client);await local.flush();
assert.equal(duplicate.duplicate,true);
assert.equal(duplicate.selection.id,input.previous.selection.id);
const changed=await client.get(`/session/${sid}/artifacts/${artifact_id}?contest_id=${contest_id}&candidate_id=${first}`);
assert.equal(changed.version,"current_changed");
assert.equal(changed.delivered.text,"# First delivery\n");
assert.equal(changed.current.text,"# Later manual change\n");
assert.ok(!changed.diff.text.includes("Later manual"));
const updated=await client.post(selectionPath,{candidate_id:second,reason:"复核后改选第二项",expected_revision:1},{key:"second-choice"});
assert.equal(updated.selection_revision,2);
const old=await client.post(selectionPath,outbox.items.selection.body,{key:outbox.items.selection.key});
assert.equal(old.duplicate,true);assert.equal(old.selection.candidate_id,first);
const current=(await client.get(path)).contests[0];
assert.equal(current.selection.candidate_id,second);
assert.equal(current.selection_revision,2);assert.equal(current.selections.length,2);
assert.equal(current.judge.recommendation_id,second);
assert.equal((await client.get(`/session/${sid}/status`)).turns,1);
await assert.rejects(client.post(selectionPath,{candidate_id:first,reason:"过期决定",expected_revision:0},{key:"stale"}),e=>e.status===409);
console.log(JSON.stringify({selection:current.selection,turns:1}));
''', previous=first)
        self.assertEqual(second["selection"]["candidate_id"], first["second"])
        self.assertFalse((self.workspace / "result.md").exists())
        self.stop(child, info)

    def test_fixture_can_compare_two_candidates_without_altering_seed_batches(self):
        child = backend_tests.DesktopBackendTests.start(self, "--fixture")
        info = self.ready(child)
        result = self.node(info, r'''
const sessions=(await client.get("/session")).sessions;
const s=sessions.find(s=>s.title.includes("整理本次工作台"));assert.ok(s);
const sid=s.session_id,path=`/session/${sid}/contests`;
const original=(await client.get(`/session/${sid}/weaves`)).batches;
const caps=await client.get(path+"/capabilities");assert.equal(caps.can_start,true);
const body={spec:"reviewer",task:"比较两种验收方案",models:["first","second"],judge_model:"judge"};
const preview=await client.post(path+"/preview",body);
await send("contest",{kind:"contest",sid,path,body:{...body,preview_token:preview.preview_token}});
await waitFor(sid,s=>!s.busy);
const contest=(await client.get(path)).contests[0];
assert.equal(contest.final,true);assert.equal(contest.candidates.length,2);
assert.equal(contest.judge.recommendation_id,contest.candidates[1].id);
assert.deepEqual((await client.get(`/session/${sid}/weaves`)).batches,original);
console.log(JSON.stringify({status:contest.judge.status}));
''')
        self.assertEqual(result["status"], "completed")
        self.stop(child, info)
