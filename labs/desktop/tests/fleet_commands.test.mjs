import test from "node:test";
import assert from "node:assert/strict";
import {createFleetCommandController,fleetCommandDraftKey,fleetCommandRequest,renderFleetCommandForm,renderFleetMergeFacts,renderFleetCleanupFacts,fleetLifecycleExecutable} from "../web/fleet_commands.mjs";

const caps = () => ({available:true,board_id:"board",revision:1,can_init:false,can_adopt:false,can_plan:true,can_spawn:true,can_merge:true,can_teardown:true,models:["model-a"],default_model:"model-a",efforts:["low","high"],missions:[{id:"M1",title:"第一项",kind:"build",status:"pending"}],members:[{name:"worker",kind:"worker",run_id:"run1",model:"original-model",mission_id:"M1",can_resume:true,resume_reason:""}]});
const draft = () => ({missions:[{id:"row",title:"新任务",kind:"survey",scope:"src\ndocs",deps:["M1"]}],name:"builder",kind:"worker",target:"M1",model:"",effort:"high",instructions:"  补充要求  ",member:JSON.stringify(["worker","run1"])});

test("requests distinguish new/adopt and keep resume identity, model, role pinned", () => {
  const c = caps(), d = draft();
  assert.deepEqual(fleetCommandRequest("new",d,c),{action:"init",mode:"new"});
  assert.deepEqual(fleetCommandRequest("adopt",d,c),{action:"init",mode:"adopt"});
  assert.deepEqual(fleetCommandRequest("plan",d,c),{action:"plan",board_id:"board",missions:[{title:"新任务",kind:"survey",scope:["src","docs"],deps:["M1"]}]});
  assert.deepEqual(fleetCommandRequest("spawn",d,c),{action:"spawn",board_id:"board",name:"builder",kind:"worker",mission_id:"M1",effort:"high",instructions:"补充要求"});
  assert.deepEqual(fleetCommandRequest("spawn",{...d,kind:"reviewer"},c).review_target,"M1");
  const resume = fleetCommandRequest("resume",{...d,model:"other",kind:"reviewer",target:"other"},c);
  assert.deepEqual(resume,{action:"resume",board_id:"board",name:"worker",run_id:"run1",effort:"high",instructions:"补充要求"});
  assert.throws(() => fleetCommandRequest("resume",{...d,member:JSON.stringify(["worker","old"])},c),/可续跑/);
  assert.throws(() => fleetCommandRequest("plan",d,{...c,board_id:""}),/历史记录须先明确采用/);
  assert.throws(() => fleetCommandRequest("plan",{...d,missions:[{...d.missions[0],deps:["future"]}]},c),/依赖任务已变化/);
});
test("renderer explains adoption, disabled capabilities and actual assigned preview facts safely", () => {
  const base = {open:true,mode:"adopt",draft:draft(),draftKey:"key",caps:{...caps(),can_adopt:true},preview:{label:"采用",tool:"chenshu_init",args:{evil:"<script>"},base_branch:"main",repo_head:"abcdef",existing:{state_id:"old",missions_count:3,members_count:2},missions:[{id:"M9",title:"<img>",kind:"build",scope:["src"],deps:["M1"]}],member:{name:"worker",model:"resolved",effort:"high",workspace:"/work"},warnings:["<script>"]}};
  const html = renderFleetCommandForm(base);
  for(const text of ["旧线程和旧成员运行不会自动恢复","M9","3 项任务","工具审批","resolved","main"]) assert.ok(html.includes(text),text);
  assert.doesNotMatch(html,/<script>|<img>/); assert.match(html,/&lt;script&gt;/);
  assert.match(renderFleetCommandForm({...base,caps:{...caps(),can_adopt:false}}),/data-fc-action="confirm" disabled/);
  assert.match(renderFleetCommandForm({...base,mode:"resume",preview:null,caps:{...caps(),members:[{...caps().members[0],can_resume:false,resume_reason:"存档缺失"}]}}),/存档缺失/);
});
function harness() {
  let currentCaps = caps(), fail = false, previewDeferred = null, previewResult = null, changesResult = null;
  const saved = new Map(), sent = [], posted = [];
  const ctx = {sid:"s",active:true,writable:true,pending:[],session:{busy:false},conn:{health:{fleet_command_protocol:1},controller:new AbortController(),localState:{getDraft:k=>saved.get(k),setDraft:(k,v)=>saved.set(k,v),deleteDraft:k=>saved.delete(k)},client:{get:async path => {if(fail)throw new Error("offline");return structuredClone(path.endsWith("/changes") ? changesResult : currentCaps);},post:async (path,body) => { posted.push({path,body});if(previewDeferred)return previewDeferred;if(previewResult)return structuredClone(previewResult);return {preview_token:"token",tool:"chenshu_plan",args:body,missions:[{id:"M2",title:"任务",scope:[],deps:[]}]};}}}};
  const controller = createFleetCommandController({context:()=>ctx,render:()=>{},submit:(scope,a)=>sent.push({scope,...structuredClone(a)})});
  let mode = "new";
  const click = (action,data={}) => controller.click({target:{closest:()=>({dataset:{fcAction:action,...data}})}});
  const select = value => {mode=value;click("mode",{fcMode:value});};
  const key = () => fleetCommandDraftKey(ctx.sid,mode,currentCaps.board_id);
  const input = (name,value,data={}) => controller.input({target:{name,value,dataset:data,closest:()=>({dataset:{fcKey:key()}})}});
  const plan = () => { select("plan"); const html=controller.render(ctx.sid); const row=html.match(/data-fc-row="([^"]+)"/)[1];input("title","任务",{fcRow:row});return row; };
  return {ctx,saved,sent,posted,controller,click,select,input,plan,key,get caps(){return currentCaps;},setCaps:c=>{currentCaps=c;},fail:()=>{fail=true;},recover:()=>{fail=false;},defer:p=>{previewDeferred=p;},setPreview:p=>{previewResult=p;},setChanges:p=>{changesResult=p;},check:(mode,checked=true)=>controller.input({target:{dataset:{fcConfirm:mode},checked}})};
}
test("preview first; scoped submit retains exact token, body and independent draft snapshot", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.plan(); h.click("confirm"); assert.equal(h.sent.length,0);
  await h.controller.preview(); h.click("confirm");
  assert.equal(h.sent.length,1); const a=h.sent[0]; assert.equal(a.kind,"fleet_command");assert.equal(a.scope,"s:fleet_command:");assert.equal(a.path,"/session/s/fleet-commands");assert.equal(a.body.preview_token,"token");assert.equal(a.draftKey,h.key());assert.equal(h.saved.get(a.draftKey),a.draftText);
  h.ctx.pending=[{...a,state:"unknown"}];h.click("confirm");await h.controller.preview();assert.equal(h.sent.length,1);assert.equal(h.posted.length,1);
});
test("editing, capability revision, failure or busy state prevents stale confirmation and preserves draft", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); const row=h.plan(); await h.controller.preview(); h.input("title","改后",{fcRow:row});h.click("confirm");assert.equal(h.sent.length,0);
  await h.controller.preview();h.setCaps({...h.caps,revision:2});await h.controller.load(h.ctx.conn,"s");h.click("confirm");assert.equal(h.sent.length,0);assert.match(h.controller.render("s"),/协作状态已变化/);
  await h.controller.preview();h.ctx.session.busy=true;h.click("confirm");assert.equal(h.sent.length,0);h.ctx.session.busy=false;
  h.fail();await h.controller.load(h.ctx.conn,"s");h.click("confirm");assert.equal(h.sent.length,0);assert.match(h.controller.render("s"),/读取可用操作失败/);assert.match(h.controller.render("s"),/改后/);assert.match(h.saved.get(h.key()),/改后/);
});
test("operation and board drafts never overwrite each other or resume a newly named generation", async () => {
  const h = harness();await h.controller.load(h.ctx.conn,"s");h.plan();const planKey=h.key();h.select("spawn");h.input("name","builder");const spawnKey=h.key();h.select("plan");assert.match(h.controller.render("s"),/value="任务"/);assert.match(h.saved.get(spawnKey),/builder/);
  h.select("resume");h.input("member",JSON.stringify(["worker","run1"]));await h.controller.preview();h.setCaps({...h.caps,members:[{...h.caps.members[0],run_id:"run2"}]});await h.controller.load(h.ctx.conn,"s");h.click("confirm");await h.controller.preview();assert.equal(h.sent.length,0);
  h.setCaps({...h.caps,board_id:"new-board"});await h.controller.load(h.ctx.conn,"s");h.select("plan");assert.doesNotMatch(h.controller.render("s"),/value="任务"/);assert.match(h.saved.get(planKey),/任务/);
});
test("late preview after edits or connection switch never submits", async () => {
  const h=harness();await h.controller.load(h.ctx.conn,"s");const row=h.plan();let resolve;h.defer(new Promise(r=>resolve=r));const pending=h.controller.preview();h.input("title","新文",{fcRow:row});resolve({preview_token:"old"});await pending;h.click("confirm");assert.equal(h.sent.length,0);
  let resolve2;h.defer(new Promise(r=>resolve2=r));const pending2=h.controller.preview();h.ctx.conn={...h.ctx.conn};resolve2({preview_token:"old2"});await pending2;h.click("confirm");assert.equal(h.sent.length,0);
});
test("accepted and recovered receipts preserve configuration for approval denial or execution failure; never resend", async () => {
  const h=harness();await h.controller.load(h.ctx.conn,"s");const row=h.plan();await h.controller.preview();h.click("confirm");const action={...h.sent[0],state:"accepted"};
  h.input("title","新输入",{fcRow:row});h.controller.accepted(h.ctx.conn,action);assert.match(h.saved.get(action.draftKey),/新输入/);
  h.controller.rejected(action,{status:409,message:"stale"});assert.match(h.controller.render("s"),/预览已过期/);assert.match(h.saved.get(action.draftKey),/新输入/);
  h.saved.set(action.draftKey,action.draftText);h.controller.reset();h.controller.accepted(h.ctx.conn,action);assert.equal(h.saved.get(action.draftKey),action.draftText);assert.equal(h.sent.length,1);
  h.select("plan");h.controller.rejected(action,{status:409,recorded:true});assert.match(h.controller.render("s"),/不会自动重放/);
});
test("malformed persistent drafts remain intact and disabled", async () => {
  const h=harness();const key=fleetCommandDraftKey("s","plan","board");h.saved.set(key,"{broken");await h.controller.load(h.ctx.conn,"s");h.select("plan");await h.controller.preview();h.click("confirm");assert.equal(h.sent.length,0);assert.equal(h.saved.get(key),"{broken");assert.match(h.controller.render("s"),/原始草稿未覆盖/);
});

const mergeFacts = () => ({mission:{id:"M1",title:"交付",kind:"build"},base_branch:"main",base_head:"base-sha",target_branch:"mission/M1",target_head:"tip-sha",merge_base:"ancestor-sha",review:{round:2,verdict:"clean",reviewed_commit:"tip-sha",current:true},deps:[{id:"M0",status:"merged",ready:true}],changed_files:["src/a.js"],diff:{text:"+new text",available:true,truncated:false,note:""},conflict_check:"not_checked"});
const cleanupFacts = () => ({worktrees:[{mission_id:"M1",path:"/worktree/M1",branch:"mission/M1",merged:false,dirty:false,live:false,can_remove:true,reason:""}],retained_branches:["mission/M1"],archives_retained:true});
const mergePreview = () => ({action:"merge",board_id:"board",can_execute:true,blockers:[],preview_token:"merge-token",merge:mergeFacts(),args:{target:"M1"},tool:"chenshu_merge"});
const cleanupPreview = () => ({action:"teardown",board_id:"board",can_execute:true,blockers:[],preview_token:"cleanup-token",teardown:cleanupFacts(),args:{},tool:"chenshu_teardown"});

test("merge and cleanup request bodies never contain force or client paths", () => {
  assert.deepEqual(fleetCommandRequest("merge",draft(),caps()),{action:"merge",board_id:"board",mission_id:"M1"});
  assert.deepEqual(fleetCommandRequest("teardown",draft(),caps()),{action:"teardown",board_id:"board"});
});
test("merge facts show bounded diff, review validity and blockers without promising conflict-free execution", () => {
  const m = mergeFacts();m.diff.truncated=true;m.diff.text="<script>";m.review.current=false;
  const html = renderFleetMergeFacts(m,[{code:"stale_review",message:"评审提交不匹配"}]);
  for (const text of ["base-sha","tip-sha","ancestor-sha","评审提交不匹配","差异已截断","未预检合并冲突","评审尚未覆盖当前任务提交"]) assert.ok(html.includes(text),text);
  assert.doesNotMatch(html,/<script>/);assert.match(html,/&lt;script&gt;/);
  const survey = renderFleetMergeFacts({...m,mission:{...m.mission,kind:"survey"}});assert.match(survey,/不发生|不修改代码/);assert.doesNotMatch(survey,/已提交差异/);
});
test("cleanup explains all-worktree scope, unmerged branch retention, and refuses dirty/live/unknown facts", () => {
  const clean = cleanupPreview();assert.equal(fleetLifecycleExecutable("teardown",clean),true);
  const html = renderFleetCleanupFacts(clean.teardown);
  for(const text of ["结束协作","存在未合并的任务分支","mission/M1","审计轨迹与成员档案保留","不会替你停止仍活跃的成员"]) assert.ok(html.includes(text),text);
  for(const patch of [{dirty:true},{dirty:null},{live:true},{live:null},{can_remove:false}]) assert.equal(fleetLifecycleExecutable("teardown",{...clean,teardown:{...clean.teardown,worktrees:[{...clean.teardown.worktrees[0],...patch}]}}),false);
  assert.equal(fleetLifecycleExecutable("teardown",{...clean,teardown:{...clean.teardown,archives_retained:false}}),false);
  assert.equal(fleetLifecycleExecutable("merge",{...mergePreview(),blockers:[{code:"x",message:"blocked"}]}),false);
});
test("blocked preview remains inspectable, but checkbox or forged token cannot submit it", async () => {
  const h=harness();await h.controller.load(h.ctx.conn,"s");h.select("merge");h.input("target","M1");
  h.setPreview({...mergePreview(),can_execute:false,preview_token:"",blockers:[{code:"review",message:"需要重新评审"}]});await h.controller.preview();
  assert.match(h.controller.render("s"),/需要重新评审/);assert.match(h.controller.render("s"),/\+new text/);
  h.check("merge");h.click("confirm");assert.equal(h.sent.length,0);
  h.setPreview({...mergePreview(),can_execute:false});await h.controller.preview();h.check("merge");h.click("confirm");assert.equal(h.sent.length,0);
});
test("merge and teardown each require a new explicit checkbox; confirmation is never persisted or restored", async () => {
  const h=harness();await h.controller.load(h.ctx.conn,"s");h.select("merge");h.input("target","M1");h.setPreview(mergePreview());await h.controller.preview();
  h.click("confirm");assert.equal(h.sent.length,0);h.check("merge");h.click("confirm");assert.equal(h.sent.length,1);assert.equal(h.sent[0].body.preview_token,"merge-token");assert.doesNotMatch(h.saved.get(h.key()),/confirmed|merge-token/);
  h.select("teardown");h.setPreview(cleanupPreview());await h.controller.preview();h.click("confirm");assert.equal(h.sent.length,1);h.check("merge");h.click("confirm");assert.equal(h.sent.length,1);h.check("teardown");h.click("confirm");assert.equal(h.sent.length,2);assert.deepEqual(h.sent[1].body,{action:"teardown",board_id:"board",preview_token:"cleanup-token"});
  await h.controller.preview();h.click("confirm");assert.equal(h.sent.length,2);h.controller.reset();await h.controller.load(h.ctx.conn,"s");h.select("teardown");await h.controller.preview();h.click("confirm");assert.equal(h.sent.length,2);
});
test("readonly changes require exact source, remain visibly stale on failure, and cannot authorize execution", async () => {
  const h=harness();await h.controller.load(h.ctx.conn,"s");h.select("merge");h.input("target","M1");h.setChanges({board_id:"board",mission_id:"M1",can_merge:true,blockers:[],merge:mergeFacts()});await h.controller.changes();assert.match(h.controller.render("s"),/base-sha/);h.check("merge");h.click("confirm");assert.equal(h.sent.length,0);
  h.fail();await h.controller.changes();assert.match(h.controller.render("s"),/保留上次检查，可能过期/);h.recover();h.setChanges({board_id:"wrong",mission_id:"M1",blockers:[],merge:mergeFacts()});await h.controller.changes();assert.match(h.controller.render("s"),/身份或格式不匹配/);
  h.select("teardown");assert.doesNotMatch(h.controller.render("s"),/base-sha/);
});
test("invalid lifecycle preview identity and changed capabilities cannot reuse explicit approval", async () => {
  const h=harness();await h.controller.load(h.ctx.conn,"s");h.select("merge");h.input("target","M1");h.setPreview({...mergePreview(),board_id:"other"});await h.controller.preview();h.check("merge");h.click("confirm");assert.equal(h.sent.length,0);assert.match(h.controller.render("s"),/没有返回完整的操作检查结果/);
  h.setPreview(mergePreview());await h.controller.preview();h.check("merge");h.setCaps({...h.caps,revision:9});await h.controller.load(h.ctx.conn,"s");h.click("confirm");assert.equal(h.sent.length,0);
});
