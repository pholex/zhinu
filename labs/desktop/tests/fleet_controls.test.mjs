import test from "node:test";
import assert from "node:assert/strict";
import {createFleetController,fleetControlState,fleetDraftKey,fleetMemberKey,parseFleetDrafts,renderFleetMemberControls,renderFleetOperations,renderFleetApprovalIdentity} from "../web/fleets.mjs";

const board = () => ({board_id:"board",revision:1,live:true,recovered:false,active:true,source:{workspace:"/work"},missions:[],members:[{name:"worker",run_id:"run-one",status:"running",execution_state:"running",execution_outcome:"running",thread_alive:true,controls:{can_message:true,can_stop:true,reason:""},operations:[]}],reviews:[],findings:[],activity:[],gaps:[],limits:{}});
const enabled = () => ({protocol:true,writable:true,persisted:true,pending:[]});

test("controls require live, exact, healthy, persisted member capability and never infer it from parent busy", () => {
  const f = board(), member = f.members[0];
  assert.equal(fleetControlState(f,member,enabled()).canMessage,true);
  for (const patch of [{stale:true},{writable:false},{protocol:false}]) {
    const state = fleetControlState(f,member,{...enabled(),...patch}); assert.equal(state.canMessage,false); assert.equal(state.canStop,false);
  }
  for (const patch of [{live:false},{recovered:true}]) assert.equal(fleetControlState({...f,...patch},member,enabled()).canStop,false);
  assert.equal(fleetControlState(f,{...member,execution_state:"unknown"},enabled()).canMessage,false);
  assert.equal(fleetControlState(f,{...member,run_id:""},enabled()).canStop,false);
  assert.equal(fleetControlState(f,{...member,controls:{can_message:false,can_stop:false}},enabled()).canMessage,false);
});
test("precise safe stop remains available with a broken service journal while messages and unknown instances stay blocked",()=>{
  const f=board(),m=f.members[0],options={...enabled(),persisted:false};
  assert.equal(fleetControlState(f,m,options).canMessage,false);
  assert.equal(fleetControlState(f,m,options).canStop,true);
  for(const patch of [{execution_state:"unknown"},{run_id:""},{controls:{can_stop:false}},{execution_state:"settled"}])assert.equal(fleetControlState(f,{...m,...patch},options).canStop,false);
  assert.equal(fleetControlState({...f,recovered:true},m,options).canStop,false);
  assert.equal(fleetControlState(f,m,{...options,stale:true}).canStop,false);
  assert.match(renderFleetMemberControls(f,m,options),/回执不保证重启后可核对/);
});
test("message delivery statuses and stop request vs actual end are distinct", () => {
  const member = board().members[0];
  member.operations = ["reserved","queued","accepted","not_consumed","stop_requested","stopped","unknown","failed"].map((status,index) => ({operation_id:String(index),run_id:member.run_id,kind:status.includes("stop") ? "stop" : "message",status,text:"正文"}));
  const html = renderFleetOperations(member);
  for (const label of ["尚未确认排队","已排队，尚未进入成员上下文","已进入成员上下文","消息未被消费","已请求停止，等待成员结束","目标成员本代已结束","执行状态未知","请求失败","不代表已回答或已完成要求"]) assert.ok(html.includes(label),label);
  assert.equal(renderFleetOperations({...member,run_id:"new-run"}),"");
});
test("controls keep old drafts read only and escape message, identity and operation errors", () => {
  const f = board(), member = f.members[0], attack = '<img src=x onerror="bad()">';
  const html = renderFleetMemberControls(f,member,{...enabled(),drafts:{"run-one":"当前",old:attack},error:attack});
  assert.match(html,/保留的旧代草稿/); assert.match(html,/不会自动交给当前成员/); assert.match(html,/<textarea rows="3" readonly>/);
  assert.match(html,/不执行清理/); assert.doesNotMatch(html,/<img/); assert.match(html,/&lt;img/);
  assert.doesNotMatch(renderFleetMemberControls(f,member),/<form|data-fleet-action="stop"/);
});
test("member approval attribution is explicit, escaped and grants only the current call", () => {
  const html = renderFleetApprovalIdentity({board_id:"board",member_name:"worker",member_run_id:"run",mission_id:"M1",workspace:"/work/<script>"});
  for (const label of ["统筹协作成员工具调用","M1","成员工作区","成员运行","协作实例","这一次工具调用"]) assert.ok(html.includes(label));
  assert.doesNotMatch(html,/<script>/); assert.match(html,/&lt;script&gt;/); assert.equal(renderFleetApprovalIdentity(null),"");
});

function harness() {
  let f = board(), failure = false, receiptFailure = false;
  const saved = new Map(), sent = [], reads = [], operations = new Map();
  const ctx = {sid:"s",active:true,writable:true,pending:[],session:{busy:true},conn:{healthy:true,health:{fleet_protocol:1,fleet_control_protocol:1},controller:new AbortController(),localState:{getDraft:key=>saved.get(key),setDraft:(key,text)=>saved.set(key,text),deleteDraft:key=>saved.delete(key)},client:{get:async path => {
    reads.push(path);
    if (path.includes("fleet-operations")) { if(receiptFailure)throw new Error("receipt offline"); return {operation:operations.get(path.split("/").at(-1))}; }
    if (failure) throw new Error("offline");
    return {session_id:"s",fleets:[f],next_cursor:null,total:1,persisted:true,available:true};
  }}}};
  const ui = {context:()=>ctx,render:()=>{},submit:(scope,action)=>sent.push({scope,...structuredClone(action)})};
  const controller = createFleetController(ui);
  const key = () => fleetMemberKey(f,f.members[0]);
  const input = value => controller.input({target:{dataset:{fleetDraft:key()},value}});
  const send = () => controller.onSubmit({target:{dataset:{fleetMessage:key()}},preventDefault:()=>{}});
  const stop = () => controller.click({target:{closest:()=>({dataset:{fleetAction:"stop",fleetMember:key()}})}});
  const receipt = (action,id="op",status="queued") => ({...action,state:"accepted",result:{operation_id:id,operation_status:status}});
  return {ctx,saved,sent,reads,operations,controller,input,send,stop,key,receipt,ui,get fleet(){return f;},setFleet:value=>{f=value;},fail:()=>{failure=true;},failReceipt:()=>{receiptFailure=true;}};
}

test("new messages and stops use scoped idempotent actions with exact board, member, generation", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.input("补充说明"); h.send(); h.stop();
  assert.equal(h.sent.length,2); assert.equal(h.sent[0].kind,"fleet_message");
  assert.equal(h.sent[0].path,"/session/s/fleets/board/members/worker/messages");
  assert.deepEqual(h.sent[0].body,{run_id:"run-one",text:"补充说明"}); assert.equal(h.sent[0].draftText,"补充说明");
  assert.equal(h.sent[1].kind,"fleet_stop"); assert.deepEqual(h.sent[1].body,{run_id:"run-one"});
  h.ctx.pending = h.sent; h.send(); h.stop(); assert.equal(h.sent.length,2);
});
test("unknown requests cannot be replaced by a new submission and stale targets never send", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.input("说明"); h.send();
  h.ctx.pending = [{...h.sent[0],state:"unknown"}]; h.send(); assert.equal(h.sent.length,1);
  const oldKey = h.key(), next = board(); next.members[0].run_id = "run-two"; h.setFleet(next); await h.controller.load(h.ctx.conn,"s");
  h.controller.onSubmit({target:{dataset:{fleetMessage:oldKey}},preventDefault:()=>{}}); assert.equal(h.sent.length,1);
  assert.match(h.controller.render("s"),/保留的旧代草稿/); assert.match(h.controller.render("s"),/说明/);
  h.ctx.pending = []; h.send(); assert.equal(h.sent.length,1);
  h.fail(); await h.controller.load(h.ctx.conn,"s"); h.input("离线保存"); h.send(); h.stop(); assert.equal(h.sent.length,1);
});
test("accepted cleanup removes only identical current-generation text, preserving new input and other drafts", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.input("原文"); h.send();
  h.input("新输入"); h.controller.accepted(h.ctx.conn,h.receipt(h.sent[0]));
  const key = fleetDraftKey("s","board","worker"); assert.equal(parseFleetDrafts(h.saved.get(key))["run-one"],"新输入");
  h.input("原文"); h.controller.accepted(h.ctx.conn,h.receipt(h.sent[0])); assert.equal(h.saved.has(key),false);
  assert.match(h.saved.get("fleet-receipts:s:board:worker"),/"operation_id":"op"/);
});
test("failed, unknown and not-consumed HTTP receipts preserve original draft and never become context acceptance", async () => {
  for (const status of ["failed","unknown","not_consumed","reserved"]) {
    const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.input("保留"); h.send();
    h.controller.accepted(h.ctx.conn,h.receipt(h.sent[0],"op",status));
    assert.equal(parseFleetDrafts(h.saved.get(fleetDraftKey("s","board","worker")))["run-one"],"保留");
  }
});
test("recovered accepted outbox receipt rebuilds bounded operation references without resending", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.input("原文"); h.send();
  const accepted = h.receipt(h.sent[0]); h.controller.reset(); h.controller.accepted(h.ctx.conn,accepted);
  assert.equal(h.saved.has(fleetDraftKey("s","board","worker")),false); assert.equal(h.sent.length,1);
  for (let i=0;i<70;i++) h.controller.accepted(h.ctx.conn,h.receipt(h.sent[0],`op-${i}`));
  const index = JSON.parse(h.saved.get("fleet-receipts:s:board:worker")); assert.equal(index.refs.length,64); assert.equal(index.truncated,true);
});
test("old-generation operation lookup is authoritative and failures disclose uncertainty", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.input("旧代正文"); h.send(); h.controller.accepted(h.ctx.conn,h.receipt(h.sent[0]));
  const next = board(); next.members[0].run_id = "run-two"; h.setFleet(next);
  h.operations.set("op",{operation_id:"op",board_id:"board",member_name:"worker",run_id:"run-one",kind:"message",status:"queued",text:"旧代正文"});
  await h.controller.load(h.ctx.conn,"s"); assert.match(h.controller.render("s"),/旧代提交记录/); assert.match(h.controller.render("s"),/尚未进入成员上下文/);
  h.failReceipt(); await h.controller.load(h.ctx.conn,"s"); assert.match(h.controller.render("s"),/回执状态读取失败/); assert.match(h.controller.render("s"),/上次记录，可能已过期/);
  assert.equal(h.sent.length,1); assert.ok(h.reads.includes("/session/s/fleet-operations/op"));
});
test("unreadable saved member drafts are never silently replaced", async () => {
  const h = harness(), key = fleetDraftKey("s","board","worker"); h.saved.set(key,"{broken");
  await h.controller.load(h.ctx.conn,"s"); h.input("新文"); h.send();
  assert.equal(h.saved.get(key),"{broken"); assert.equal(h.sent.length,0); assert.match(h.controller.render("s"),/readonly/);
});
test("switching sessions preserves original member draft and does not submit old DOM actions", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.input("留在原会话"); h.ctx.sid = "other"; h.send(); h.stop();
  assert.equal(h.sent.length,0); assert.equal(parseFleetDrafts(h.saved.get(fleetDraftKey("s","board","worker")))["run-one"],"留在原会话");
});
