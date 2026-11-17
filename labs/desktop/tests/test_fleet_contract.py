"""Real desktop fleet observations survive restart without adopting runtime state."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from labs.desktop.tests import test_macos_backend as backend_tests
from labs.desktop.tests import test_weave_actions_contract as weave_tests

ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("node") and shutil.which("git"),
                     "macOS, Node and Git are required")
class FleetContractTests(unittest.TestCase):
    setUp = weave_tests.WeaveActionsContractTests.setUp
    cleanup_children = backend_tests.DesktopBackendTests.cleanup_children
    ready = backend_tests.DesktopBackendTests.ready
    request = backend_tests.DesktopBackendTests.request
    stop = backend_tests.DesktopBackendTests.stop
    node = weave_tests.WeaveActionsContractTests.node

    def launch(self, script, member_script=None):
        path = self.root / "script.txt"
        path.write_text(script, encoding="utf-8")
        env = {key: os.environ[key] for key in ("PATH", "TMPDIR") if key in os.environ}
        env.update(HOME=str(self.root), USERPROFILE=str(self.root), XDG_CONFIG_HOME=str(self.root / "config"),
                   APPDATA=str(self.root / "config"), PYTHONPATH=str(ROOT), PYTHONNOUSERSITE="1",
                   XIAOYU_SCRIPTED_SCRIPTS=str(path), XIAOYU_SUGGESTIONS="0", XIAOYU_ENABLE_CHENSHU="1",
                   XIAOYU_ENABLE_ASKPASS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
        for feature in ("MCP", "PLUGINS", "SKILLS", "HOOKS", "EXPLORE", "WEB_SEARCH", "X_SEARCH",
                        "DEEP_RESEARCH", "BROWSER", "AGENTS"):
            env[f"XIAOYU_ENABLE_{feature}"] = "0"
        errors = tempfile.TemporaryFile()
        command = [sys.executable, "-P", "-u", "-m", "labs.desktop.macos.backend"]
        if member_script is not None:
            member_path = self.root / "member-script.txt"
            member_path.write_text(member_script, encoding="utf-8")
            # Give real member Agents a separate deterministic model queue.
            bootstrap = (
                "import runpy\nfrom xiaoyu.chenshu import ChenshuRuntime\n"
                "from xiaoyu.providers import Registry\nfrom xiaoyu.scripted import ScriptedClient\n"
                "original = ChenshuRuntime.__init__\n"
                "def initialize(self, *args, **kwargs):\n"
                "    original(self, *args, **kwargs)\n"
                f"    self.registry = Registry.for_client(ScriptedClient.from_file({str(member_path)!r}))\n"
                "ChenshuRuntime.__init__ = initialize\n"
                "runpy.run_module('labs.desktop.macos.backend', run_name='__main__')\n"
            )
            command = [sys.executable, "-P", "-u", "-c", bootstrap]
        child = subprocess.Popen([*command, "--workspace", str(self.workspace)], cwd=ROOT, env=env,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        self.children.append((child, errors))
        return child, self.ready(child)

    def test_reads_and_restart_preserve_observations_without_adopting_or_writing_workspace(self):
        def tool(name, args):
            return "tool_call: " + json.dumps({"name": name, "arguments": args}, ensure_ascii=False) + "\n"
        script = (tool("chenshu_init", {}) + "---\n"
                  + tool("chenshu_plan", {"missions": [
                      {"title": "只读范围调研", "kind": "survey"},
                      {"title": "后续实现", "kind": "build", "scope": ["src/"], "deps": ["M1"]}]}) + "---\n"
                  + tool("chenshu_mission", {"mission_id": "M1", "status": "completed", "note": "已记录范围；没有启动实现成员。"})
                  + "---\ntext: 已完成规划并保留后续实现为待开始。\n")
        child, info = self.launch(script)
        before = self.node(info, r'''
assert.equal(health.fleet_protocol,1);
const created=await send("create",{kind:"create",path:"/session",body:{model:"lab-scripted"}}),sid=created.session_id;
assert.equal((await client.get(`/session/${sid}/fleets`)).fleets.length,0);
await send("prompt",{kind:"prompt",sid,path:`/session/${sid}/prompt_async`,body:{text:"规划两项任务，不开始实现"}});
assert.equal((await approval(sid,"allow")).tool,"chenshu_init");
await waitFor(sid,s=>!s.busy);
const page=await client.get(`/session/${sid}/fleets`),fleet=page.fleets[0];
assert.equal(page.fleets.length,1);assert.equal(page.persisted,true);
assert.deepEqual(fleet.missions.map(m=>m.status),["completed","pending"]);
assert.deepEqual(fleet.missions[1].deps,["M1"]);assert.deepEqual(fleet.missions[1].scope,["src/"]);
assert.equal(fleet.members.length,0);assert.equal(fleet.live,true);assert.ok(fleet.source_run_id);
const other=await client.post("/session",{model:"lab-scripted"},{key:"empty-observer"});
assert.equal((await client.get(`/session/${other.session_id}/fleets`)).fleets.length,0);
console.log(JSON.stringify({sid,fleet,other:other.session_id}));
''')
        protocol = self.workspace / ".xiaoyu/chenshu"
        disk = {p.relative_to(protocol): p.read_bytes() for p in protocol.rglob("*") if p.is_file()}
        self.stop(child, info)
        child, info = self.launch("error: a GET must never call a model\n")
        self.node(info, r'''
const {sid,fleet:previous,other}=input.previous;
for(let i=0;i<3;i++){
 const page=await client.get(`/session/${sid}/fleets`),fleet=page.fleets[0];
 assert.equal(fleet.board_id,previous.board_id);assert.equal(fleet.source.state_id,previous.source.state_id);
 assert.equal(fleet.revision,previous.revision);assert.equal(fleet.source_run_id,previous.source_run_id);
 assert.deepEqual(fleet.missions,previous.missions);assert.equal(fleet.live,false);assert.equal(fleet.recovered,true);
 assert.equal(fleet.runtime_state,"unknown_after_restart");
}
await assert.rejects(client.get(`/session/${other}/fleets`),error=>error.status===404);
assert.ok(!(await client.get("/session")).sessions.some(s=>s.session_id===other));
assert.equal((await client.get(`/session/${sid}/status`)).turns,1);
console.log(JSON.stringify({ok:true}));
''', previous=before)
        self.assertEqual({p.relative_to(protocol): p.read_bytes() for p in protocol.rglob("*") if p.is_file()}, disk)
        self.stop(child, info)

    def test_member_message_receipts_stop_and_restart_keep_worktree(self):
        def tool(name, args):
            return "tool_call: " + json.dumps({"name": name, "arguments": args}, ensure_ascii=False) + "\n"
        parent = (tool("chenshu_init", {}) + "---\n"
                  + tool("chenshu_plan", {"missions": [{"title": "写入两份交付", "kind": "build", "scope": ["first.txt", "second.txt"]}]})
                  + "---\n" + tool("chenshu_spawn", {"name": "writer", "kind": "worker", "mission_id": "M1"})
                  + "---\ntext: 成员已启动。\n")
        member = (tool("write_file", {"path": "first.txt", "content": "已批准的交付"}) + "---\n"
                  + tool("write_file", {"path": "second.txt", "content": "不应写入"})
                  + "---\ntext: 成员结束。\n")
        child, info = self.launch(parent, member)
        before = self.node(info, r'''
assert.equal(health.fleet_control_protocol,1);
const created=await send("create",{kind:"create",path:"/session",body:{model:"lab-scripted"}}),sid=created.session_id;
await send("prompt",{kind:"prompt",sid,path:`/session/${sid}/prompt_async`,body:{text:"启动独立实现成员"}});
assert.equal((await approval(sid,"allow")).tool,"chenshu_init");
await waitFor(sid,s=>!s.busy);
const initial=(await client.get(`/session/${sid}/fleets`)).fleets[0];
assert.ok(initial?.members?.length,JSON.stringify(await client.get(`/session/${sid}/events`)));
let state=await waitFor(sid,s=>!s.busy && s.pending_approvals.some(p=>p.identity?.member_name==="writer"));
const first=state.pending_approvals.find(p=>p.identity?.member_name==="writer");
assert.equal(first.args.path,"first.txt");assert.deepEqual(first.allowed_decisions,["allow","deny"]);
const fleet=(await client.get(`/session/${sid}/fleets`)).fleets[0],worker=fleet.members[0];
assert.equal(first.identity.board_id,fleet.board_id);assert.equal(first.identity.member_run_id,worker.run_id);
assert.equal(first.identity.workspace,fleet.missions[0].worktree);assert.equal(worker.controls.can_message,true);
const path=`/session/${sid}/fleets/${fleet.board_id}/members/writer`;
const lost={post:async(...args)=>{await client.post(...args);throw new TypeError("reply dropped");}};
const action={kind:"fleet_message",sid,path:path+"/messages",body:{run_id:worker.run_id,text:"保留第一份交付，核对第二步。"}};
await assert.rejects(send("member-message",action,lost));
const duplicate=await outbox.send("member-message",client);await local.flush();
assert.equal(duplicate.duplicate,true);assert.equal(duplicate.operation.status,"queued");
const messageId=duplicate.operation.operation_id;
assert.equal(outbox.items["member-message"].result.operation_id,messageId);
await send("first-approval",{kind:"approval",sid,path:`/session/${sid}/permissions`,body:{request_id:first.request_id,decision:"allow"}});
state=await waitFor(sid,s=>s.pending_approvals.some(p=>p.identity?.member_name==="writer" && p.args.path==="second.txt"));
assert.equal((await client.get(`/session/${sid}/fleet-operations/${messageId}`)).operation.status,"accepted");
const stopped=await send("member-stop",{kind:"fleet_stop",sid,path:path+"/stop",body:{run_id:worker.run_id}});
for(let i=0;i<300;i++){
 const op=(await client.get(`/session/${sid}/fleet-operations/${stopped.operation.operation_id}`)).operation;
 if(op.status==="stopped")break;
 if(i===299)throw new Error("member did not settle");
 await new Promise(r=>setTimeout(r,20));
}
state=await waitFor(sid,s=>!s.pending_approvals.length);
const final=(await client.get(`/session/${sid}/fleets`)).fleets[0];
assert.equal(final.members[0].controls.can_stop,false);
assert.equal(final.members[0].execution_outcome,"partial");
console.log(JSON.stringify({sid,path,run:worker.run_id,worktree:first.identity.workspace,messageId,
 stopId:stopped.operation.operation_id,messageKey:outbox.items["member-message"].key,messageBody:action.body}));
''')
        worktree = Path(before["worktree"])
        self.assertEqual((worktree / "first.txt").read_text(encoding="utf-8", errors="replace"), "已批准的交付")
        self.assertFalse((worktree / "second.txt").exists())
        self.assertFalse((self.workspace / "first.txt").exists())
        self.stop(child, info)
        self.assertTrue(worktree.is_dir())
        child, info = self.launch("error: restart must not invoke model\n")
        self.node(info, r'''
const p=input.previous;
const repeated=await client.post(p.path+"/messages",p.messageBody,{key:p.messageKey});
assert.equal(repeated.duplicate,true);assert.equal(repeated.operation.operation_id,p.messageId);
assert.equal(repeated.operation.status,"accepted");
assert.equal((await client.get(`/session/${p.sid}/fleet-operations/${p.stopId}`)).operation.status,"stopped");
const fleet=(await client.get(`/session/${p.sid}/fleets`)).fleets[0];
assert.equal(fleet.recovered,true);assert.equal(fleet.members[0].controls.can_message,false);
assert.equal((await client.get(`/session/${p.sid}/status`)).turns,1);
console.log(JSON.stringify({ok:true}));
''', previous=before)
        self.assertEqual((worktree / "first.txt").read_text(encoding="utf-8", errors="replace"), "已批准的交付")
        self.assertFalse((worktree / "second.txt").exists())
        self.stop(child, info)

    def test_demo_has_real_member_finding_and_keeps_earlier_workflows(self):
        child = backend_tests.DesktopBackendTests.start(self, "--fixture")
        info = self.ready(child)
        self.node(info, r'''
const sessions=(await client.get("/session")).sessions;
assert.equal(sessions.length,4);
const done=sessions.find(s=>s.title==="整理本次工作台实验的交付记录。"),sid=done.session_id;
const page=await client.get(`/session/${sid}/fleets`),fleet=page.fleets[0];
assert.equal(page.fleets.length,1);assert.equal(fleet.members.length,1);
assert.equal(fleet.members[0].name,"scope-reader");assert.equal(fleet.members[0].status,"done");
assert.equal(fleet.missions[0].status,"completed");assert.equal(fleet.missions[1].status,"pending");
assert.equal(fleet.findings[0].kind,"improve");assert.ok(fleet.members[0].handoff);
assert.equal((await client.get(`/session/${sid}/weaves`)).batches[0].items.length,2);
assert.equal((await client.get(`/session/${sid}/contests`)).contests.length,0);
console.log(JSON.stringify({ok:true}));
''')
        self.stop(child, info)
