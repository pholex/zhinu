import test from "node:test";
import assert from "node:assert/strict";
import {renderFleets,missionState,memberState,createFleetController} from "../web/fleets.mjs";

const fleet = () => ({board_id:"board",revision:4,active:true,base_branch:"feature",observed_at:1730000000,live:true,recovered:false,source_run_id:"parent-run",source:{runtime_id:"board",state_id:"state",origin:"new",workspace:"/work/project"},
  missions:[{id:"M1",title:"实现接口",kind:"build",scope:["src/api"],deps:[],status:"completed",owner:"worker",branch:"feat/api",worktree:"/work/api",notes:["需要复核"]},{id:"M2",title:"验证接口",kind:"survey",scope:[],deps:["M1"],status:"pending",owner:""}],
  members:[{name:"worker",kind:"worker",mission_id:"M1",status:"done",execution_state:"settled",execution_outcome:"partial",thread_alive:false,model:"scripted",effort:"high",run_id:"child-run",handoff:"交接说明",handoff_truncated:true,stop_reason:"turn_cap"},{name:"reviewer",kind:"reviewer",review_target:"M1",status:"running",execution_state:"running",execution_outcome:"running",thread_alive:true,model:"scripted",handoff:""}],
  reviews:[{id:"r1",mission_id:"M1",from:"reviewer",round:1,verdict:"clean",summary:"已检查",reviewed_commit:"abc",recorded_at:"2026-10-16T01:02:03Z",commit_check:"not_rechecked"}],
  findings:[{id:"f1",from:"worker",mission_id:"M1",kind:"bug",title:"另一个缺陷",body:"范围之外，待处理",recorded_at:1730000000}],
  activity:[{id:"a1",at:1730000000,actor:"chenshu",action:"merge.blocked",details:{mission:"M1",reason:"deps-unmerged"}},{id:"a2",at:1730000000,actor:"chenshu",action:"inbox.send",details:{to:"worker"}}],limits:{missions:0,members:0,reviews:0,findings:0,activity:0},gaps:[]});
const state = (f = fleet()) => ({fleets:[f],cursor:null,total:1,persisted:true,available:true});

test("mission protocol completion, merge, and member execution are distinct", () => {
  const f = fleet(), html = renderFleets(state(f));
  assert.match(html,/已完成，尚未收账/); assert.match(html,/已结束，未完整完成/);
  assert.match(html,/花名册记录/); assert.match(html,/运行已结束/);
  assert.equal(missionState({status:"merged",kind:"build"}),"已合并");
  assert.equal(missionState({status:"merged",kind:"survey"}),"已收账（无代码合并）");
  assert.equal(memberState({status:"failed",execution_state:"settled",execution_outcome:"failed"},f),"执行失败");
  assert.equal(memberState({status:"retired",execution_state:"retired"},f),"已退役");
});
test("scope, dependency edges, ownership, configured models and handoff provenance are rendered", () => {
  const html = renderFleets(state());
  for (const value of ["/work/project","src/api","M1 · 实现接口 · 已完成","worker","scripted","child-run","parent-run","交接说明","交接正文已截断"]) assert.ok(html.includes(value),value);
  assert.match(html,/调研任务，不分配写入范围/);
});
test("idle parent never implies the crew stopped and does not change member state", () => {
  const html = renderFleets(state(),{parentBusy:false});
  assert.match(html,/父会话当前轮次空闲/); assert.match(html,/不等于停止统筹协作成员/);
  assert.match(html,/执行中/); assert.doesNotMatch(html,/全队已停止/);
  assert.doesNotMatch(html,/<form|data-fleet-action="(?:spawn|send|merge|teardown)"/);
});
test("recovered snapshots preserve facts without claiming restored workers", () => {
  const f = fleet(); f.live = false; f.recovered = true; f.runtime_state = "unknown_after_restart";
  Object.assign(f.members[1],{execution_state:"unknown",execution_outcome:"unknown",thread_alive:null,recorded_execution_state:"running"});
  const html = renderFleets(state(f));
  assert.match(html,/历史快照/); assert.match(html,/未收养或恢复成员/); assert.match(html,/上次记录：运行状态未知/);
  assert.match(html,/已结束，未完整完成/);
  assert.equal(memberState({...f.members[1],execution_state:"running"},f),"运行状态未知");
});
test("read failure retains old data explicitly; empty failure never becomes no activity", () => {
  let html = renderFleets({...state(),error:"offline"});
  assert.match(html,/旧快照，待核对/); assert.match(html,/上次记录：执行中/); assert.match(html,/成员当前状态尚未核对/);
  html = renderFleets({fleets:[],error:"offline"});
  assert.match(html,/不能判断是否有成员执行/); assert.doesNotMatch(html,/尚无已记录的统筹协作活动/);
});
test("reviews are recorded verdicts, never current merge approval; message delivery is not read receipt", () => {
  const html = renderFleets(state());
  assert.match(html,/评审记录：通过/); assert.match(html,/当前提交未复核/);
  assert.match(html,/不代表现在可以合并/); assert.match(html,/合并被阻止/); assert.match(html,/deps-unmerged/);
  assert.match(html,/不表示成员已读取或模型已接纳/); assert.match(html,/另一个缺陷/);
});
test("snapshot truncation, gaps and persistence limitations are visible", () => {
  const f = fleet(); f.limits.findings = 8; f.limits.activity = 19; f.gaps = ["部分评审文件无法读取"];
  f.missions[0].deps_truncated = true; f.missions[0].scope_truncated = true; f.findings[0].body_truncated = true; f.reviews[0].summary_truncated = true;
  const html = renderFleets({...state(f),persisted:false,warning:"持久化失败"});
  assert.match(html,/负责范围、依赖已截断/); assert.match(html,/发现的正文已截断/); assert.match(html,/评审正文已截断/);
  assert.match(html,/发现省略 8 项/); assert.match(html,/活动省略 19 项/); assert.match(html,/部分评审文件无法读取/);
  assert.match(html,/尚未确认持久化/); assert.match(html,/持久化失败/);
});
test("all structured and markdown content renders inert; unknown status is never promoted to success", () => {
  const attack = '<img src=x onerror="bad()">', f = fleet();
  f.source.workspace = attack; f.members[0].handoff = attack; f.findings[0].body = attack; f.reviews[0].summary = attack;
  f.activity[0].details = {"<script>":attack}; f.missions[0].scope = [attack]; f.gaps = [attack];
  const html = renderFleets(state(f)); assert.doesNotMatch(html,/<img|<script>/); assert.match(html,/&lt;img/);
  assert.equal(missionState({status:"new_state"}),"new_state");
  assert.equal(memberState({status:"unexpected"},f),"结果尚未确认");
});
test("loading and unavailable/empty states are concise and contain no control placeholders", () => {
  assert.match(renderFleets({fleets:[],loading:true}),/正在读取协作记录/);
  assert.match(renderFleets({fleets:[],available:true}),/尚无已记录的统筹协作活动/);
  assert.match(renderFleets({fleets:[],available:false,reason:"未启用"}),/未启用/);
  assert.doesNotMatch(renderFleets({fleets:[],available:false}),/<button/);
  assert.match(renderFleets({...state(),available:false}),/实现接口/);
});

function harness() {
  let page = {session_id:"s",...state(),next_cursor:null}, failure = null, paints = 0;
  const calls = [], ctx = {sid:"s",active:true,session:{busy:false},conn:{health:{fleet_protocol:1},controller:new AbortController(),client:{get:async path => {calls.push(path);if(failure)throw new Error(failure);return page;}}}};
  const controller = createFleetController({context:()=>ctx,render:()=>{paints++;}});
  return {ctx,controller,calls,paintCount:()=>paints,setPage:value=>{page=value;},fail:value=>{failure=value;}};
}
test("controller only reads; refresh failures preserve prior facts and successful refresh replaces them", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); assert.match(h.controller.render("s"),/实现接口/);
  assert.equal(h.controller.hasUnsettled("s"),true); h.fail("network"); await h.controller.load(h.ctx.conn,"s");
  assert.match(h.controller.render("s"),/旧快照，待核对/); assert.match(h.controller.render("s"),/实现接口/);
  h.fail(null); h.setPage({session_id:"s",fleets:[],next_cursor:null,total:0,available:true,persisted:true}); await h.controller.load(h.ctx.conn,"s");
  assert.doesNotMatch(h.controller.render("s"),/实现接口/); assert.equal(h.controller.hasUnsettled("s"),false);
  assert.ok(h.calls.every(path => path.startsWith("/session/s/fleets?")));
});
test("wrong-session payload cannot replace current data", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.setPage({session_id:"other",fleets:[],next_cursor:null}); await h.controller.load(h.ctx.conn,"s");
  assert.match(h.controller.render("s"),/实现接口/); assert.match(h.controller.render("s"),/不属于当前会话/);
});
test("connection reset discards a late response and switching sessions does not repaint another session", async () => {
  const h = harness(); let finish; h.ctx.conn.client.get = () => new Promise(resolve => {finish = resolve;});
  const loading = h.controller.load(h.ctx.conn,"s"); h.ctx.conn = {...h.ctx.conn}; h.controller.reset();
  finish({session_id:"s",...state(),next_cursor:null}); await loading; assert.equal(h.controller.render("s"),"");
  const next = h.controller.load(h.ctx.conn,"s"), paints = h.paintCount(); h.ctx.sid = "other";
  finish({session_id:"s",...state(),next_cursor:null}); await next; assert.equal(h.paintCount(),paints);
});
test("pagination merges stable board identities and preserves loaded count on refresh", async () => {
  const h = harness(); h.setPage({session_id:"s",fleets:[fleet()],next_cursor:1,total:2,persisted:true}); await h.controller.load(h.ctx.conn,"s");
  h.setPage({session_id:"s",fleets:[{...fleet(),board_id:"older",revision:1}],next_cursor:null,total:2,persisted:true}); await h.controller.load(h.ctx.conn,"s",true);
  assert.equal((h.controller.render("s").match(/class="fleet-board"/g) || []).length,2);
  assert.ok(h.calls[1].includes("cursor=1"));
  await h.controller.load(h.ctx.conn,"s"); assert.ok(h.calls[2].includes("cursor=0"));
});


test("global connection loss marks the last observation stale before a fleet fetch can run", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.ctx.conn.healthy = false;
  const html = h.controller.render("s"); assert.match(html,/服务连接异常/); assert.match(html,/旧快照，待核对/);
  assert.match(html,/父会话上次记录为轮次空闲/); assert.doesNotMatch(html,/父会话当前轮次空闲/);
});


test("core activity names are translated without promising model fallback or message acceptance", () => {
  const f = fleet();
  f.activity = ["spawn","member.done","member.failed","member.cut_short","member.reaped","archive.failed","inbox.send","mission.scope","mission.update","worktree.keep"].map((action,index) => ({id:String(index),action,actor:"worker",at:1730000000,details:{}}));
  const html = renderFleets(state(f));
  for (const label of ["启动成员","成员完整结束","成员执行失败","成员未完整结束","回收成员后台任务","成员存档失败","发送成员消息","调整使命范围","更新使命","保留工作树"]) assert.ok(html.includes(label),label);
  assert.match(html,/<dt>配置模型<\/dt>/); assert.doesNotMatch(html,/实际模型/);
  assert.match(html,/消息发送记录不表示成员已读取或模型已接纳/);
});


test("activity detail truncation is explicit and only shown when the core marks it", () => {
  const f = fleet(); assert.doesNotMatch(renderFleets(state(f)),/活动详情已截断/);
  f.activity[0].details_truncated = true; f.members[0].model_truncated = true;
  assert.match(renderFleets(state(f)),/模型名称已截断/);
  assert.match(renderFleets(state(f)),/活动详情已截断/);
});

test("retired roster with a live thread stays visible as unsettled, unlike settled thread cleanup", async () => {
  const f = fleet();
  f.members = [{name:"retired-worker",status:"retired",execution_state:"retired",execution_outcome:"unknown",thread_alive:true}];
  assert.equal(memberState(f.members[0],f),"花名册已退役，线程仍活跃");
  assert.match(renderFleets({...state(f),error:"offline"}),/上次记录：花名册已退役，线程仍活跃/);
  assert.equal(memberState(f.members[0],{...f,live:false,recovered:true}),"已退役");
  const h = harness(); h.setPage({session_id:"s",fleets:[f],next_cursor:null,total:1,persisted:true});
  await h.controller.load(h.ctx.conn,"s"); assert.equal(h.controller.hasUnsettled("s"),true);
  f.live = false; f.recovered = true; f.members[0].recorded_thread_alive = true; f.members[0].thread_alive = null;
  await h.controller.load(h.ctx.conn,"s"); assert.equal(h.controller.hasUnsettled("s"),true);
  assert.match(h.controller.render("s"),/历史花名册已退役，运行状态未知/);
  assert.doesNotMatch(h.controller.render("s"),/线程仍活跃/);
  f.live = true; f.recovered = false; f.members[0].thread_alive = true;
  f.members[0] = {...f.members[0],status:"done",execution_state:"settled",execution_outcome:"completed"};
  await h.controller.load(h.ctx.conn,"s"); assert.equal(h.controller.hasUnsettled("s"),false);
  assert.equal(memberState(f.members[0],f),"执行已完成");
  assert.doesNotMatch(h.controller.render("s"),/线程仍活跃/);
});

test("compact task pages omit empty boards but keep failures, warnings and real members",()=>{
  const empty={fleets:[],available:true,persisted:true};
  assert.equal(renderFleets(empty,{hideEmpty:true}),"");
  assert.match(renderFleets({...empty,error:"读取失败"},{hideEmpty:true}),/读取失败/);
  assert.match(renderFleets({...empty,warning:"记录待核对"},{hideEmpty:true}),/记录待核对/);
  assert.match(renderFleets(state(),{hideEmpty:true}),/worker/);
});
