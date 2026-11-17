import test from "node:test";
import assert from "node:assert/strict";
import {createWeaveControlController,weaveControlKey,weaveMessageDraftKey,parseWeaveMessageDrafts,weaveControlState,renderWeaveItemControls,renderWeaveOperation,renderWeaveApprovalIdentity} from "../web/weave_controls.mjs";
import {renderFleetApprovalIdentity} from "../web/fleets.mjs";
import {renderWeaves} from "../web/workflows.mjs";

const batch = () => ({batch_id:"batch",items:[{id:"batch:0",index:0,label:"任务一",status:"running",execution_id:"exec1",execution_phase:"primary",execution_state:"running",live:true,recovered:false,run_id:"archive-only",controls:{can_message:true,can_stop:true,reason:""},operations:[]}]});
const opts = () => ({writable:true,protocol:true,stale:false,persisted:true,pending:[]});
test("controls bind exact live execution; archive handle alone or queued item never authorizes writes",()=>{
  const b=batch(), i=b.items[0];assert.equal(weaveControlState(b,i,opts()).canMessage,true);
  for(const patch of [{execution_id:""},{live:false},{recovered:true},{execution_state:"settled"},{execution_state:"unknown"}]){const result=weaveControlState(b,{...i,...patch},opts());assert.equal(result.canMessage,false);assert.equal(result.canStop,false);}
  const queued=renderWeaveItemControls(b,{...i,execution_id:"",status:"queued"},opts());assert.match(queued,/尚未开始/);assert.match(queued,/暂不支持消息或单项停止/);
  for(const patch of [{writable:false},{stale:true},{protocol:false}])assert.equal(weaveControlState(b,i,{...opts(),...patch}).canStop,false);
});
test("a bad service log disables messages but preserves explicitly allowed safe stop",()=>{
  const b=batch(), result=weaveControlState(b,b.items[0],{...opts(),persisted:false});assert.equal(result.canMessage,false);assert.equal(result.canStop,true);
  assert.match(renderWeaveItemControls(b,b.items[0],{...opts(),persisted:false}),/不承诺重启后可核对/);
  assert.equal(weaveControlState(b,{...b.items[0],controls:{can_message:false,can_stop:false}},opts()).canStop,false);
});
test("queued, context acceptance, unconsumed, stop request and instance exit are distinct and escaped",()=>{
  const statuses={queued:"已排队",accepted:"已进入此执行实例上下文",not_consumed:"消息未被消费",requested:"已请求停止",settled:"当前执行实例已退出",unknown:"操作结果未知"};
  for(const [status,label] of Object.entries(statuses)){const html=renderWeaveOperation({operation_id:"op",execution_id:"exec",kind:["requested","settled"].includes(status)?"stop":"message",status,text:"<img>"});assert.match(html,new RegExp(label));assert.doesNotMatch(html,/<img>/);}
  assert.match(renderWeaveOperation({kind:"stop",status:"settled",persisted:false,warning:"warning"}),/不保证它是因停止请求结束/);
  assert.match(renderWeaveOperation({kind:"message",status:"accepted"}),/不代表模型已经回答或遵循要求/);
});
test("task details show item interaction and qixiang approval never claims fleet member identity",()=>{
  const b=batch(), html=renderWeaves({items:[b],cursor:null},{itemControls:(batch,item)=>renderWeaveItemControls(batch,item,opts())});assert.match(html,/结果与交互/);assert.match(html,/发送给此项/);
  const identity={kind:"qixiang",batch_id:"batch",item_id:"batch:0",execution_id:"exec1",workspace:"/work/<script>",label:"任务"};
  const approval=renderWeaveApprovalIdentity(identity);for(const text of ["批量执行单项工具调用","batch:0","exec1","这一次工具调用"])assert.ok(approval.includes(text));assert.doesNotMatch(approval,/<script>/);assert.equal(renderFleetApprovalIdentity(identity),"");assert.equal(renderWeaveApprovalIdentity({board_id:"board"}),"");
});
function harness(){
  let b=batch(), failure=false;const saved=new Map(),sent=[],operations=new Map(),reads=[];
  const state={items:[b],persisted:true,error:""},ctx={sid:"s",active:true,writable:true,pending:[],conn:{healthy:true,health:{weave_control_protocol:1},controller:new AbortController(),localState:{getDraft:k=>saved.get(k),setDraft:(k,v)=>saved.set(k,v),deleteDraft:k=>saved.delete(k)},client:{get:async path=>{reads.push(path);if(failure)throw new Error("offline");return {operation:operations.get(path.split("/").at(-1))};}}}};
  const controller=createWeaveControlController({context:()=>ctx,state:sid=>sid==="s" ? state : undefined,render:()=>{},submit:(scope,a)=>sent.push({scope,...structuredClone(a)})});
  const key=()=>weaveControlKey(b,b.items[0]);
  return {ctx,state,saved,sent,operations,reads,controller,key,get batch(){return b;},setBatch:next=>{b=next;state.items=[b];},input:text=>controller.input({target:{dataset:{weaveMessageDraft:key()},value:text}}),send:()=>controller.onSubmit({target:{dataset:{weaveMessage:key()}},preventDefault:()=>{}}),stop:()=>controller.click({target:{closest:()=>({dataset:{weaveControl:"stop",weaveTarget:key()}})}}),accept:(a,status="queued",extra={})=>controller.accepted(ctx.conn,{...a,state:"accepted",result:{operation_id:"op",operation_status:status,operation_persisted:true,...extra}}),fail:()=>{failure=true;}};
}
test("messages and stops use original execution identity and independent idempotent scope",()=>{
  const h=harness();h.input("补充");h.send();h.stop();assert.equal(h.sent.length,2);assert.equal(h.sent[0].kind,"weave_message");assert.deepEqual(h.sent[0].body,{execution_id:"exec1",text:"补充"});assert.equal(h.sent[0].path,"/session/s/weaves/batch/items/batch%3A0/messages");assert.deepEqual(h.sent[1].body,{execution_id:"exec1"});assert.doesNotMatch(JSON.stringify(h.sent),/archive-only/);
  h.ctx.pending=h.sent.map(a=>({...a,state:"unknown"}));h.send();h.stop();assert.equal(h.sent.length,2);
});
test("follow-up generation changes preserve old drafts and cannot receive stale DOM submission",()=>{
  const h=harness();h.input("原执行草稿");const oldkey=h.key();const next=batch();next.items[0].execution_id="exec2";next.items[0].execution_phase="follow_up";h.setBatch(next);
  h.controller.onSubmit({target:{dataset:{weaveMessage:oldkey}},preventDefault:()=>{}});h.send();assert.equal(h.sent.length,0);
  const html=h.controller.render("s",next,next.items[0]);assert.match(html,/自动追问执行/);assert.match(html,/原执行草稿/);assert.match(html,/不会自动转投/);
  h.input("新执行草稿");h.send();assert.deepEqual(h.sent[0].body,{execution_id:"exec2",text:"新执行草稿"});
});
test("unknown, failed, reserved and unconsumed receipts preserve original drafts; accepted cleanup keeps newer text",()=>{
  for(const status of ["unknown","failed","reserved","not_consumed"]){const h=harness();h.input("原文");h.send();h.accept(h.sent[0],status);assert.equal(parseWeaveMessageDrafts(h.saved.get(weaveMessageDraftKey("s","batch","batch:0"))).exec1,"原文");}
  const h=harness();h.input("原文");h.send();h.input("新草稿");h.accept(h.sent[0]);assert.equal(parseWeaveMessageDrafts(h.saved.get(h.sent[0].draftKey)).exec1,"新草稿");h.input("原文");h.accept(h.sent[0]);assert.equal(h.saved.has(h.sent[0].draftKey),false);
});
test("restored accepted outbox rebuilds receipt references without sending and surfaces nonpersistent stop",async()=>{
  const h=harness();h.stop();const a=h.sent[0];h.controller.reset();h.accept(a,"requested",{operation_persisted:false,operation_warning:"日志损坏"});assert.equal(h.sent.length,1);
  const refs=JSON.parse(h.saved.get("weave-receipts:s:batch:batch:0"));assert.equal(refs.refs[0].persisted,false);assert.equal(refs.refs[0].execution_id,"exec1");assert.match(h.controller.render("s",h.batch,h.batch.items[0]),/日志损坏/);
  h.fail();await h.controller.loadReceipts(h.ctx.conn,"s");assert.match(h.controller.render("s",h.batch,h.batch.items[0]),/回执读取失败/);
});
test("old execution receipt lookup validates identity and errors disclose stale observations",async()=>{
  const h=harness();h.input("原文");h.send();h.accept(h.sent[0]);const next=batch();next.items[0].execution_id="exec2";h.setBatch(next);
  h.operations.set("op",{operation_id:"op",batch_id:"batch",item_id:"batch:0",execution_id:"exec1",kind:"message",status:"queued",text:"原文",persisted:true});await h.controller.loadReceipts(h.ctx.conn,"s");assert.match(h.controller.render("s",next,next.items[0]),/尚未进入此执行实例上下文/);assert.ok(h.reads.includes("/session/s/weave-operations/op"));
  h.fail();await h.controller.loadReceipts(h.ctx.conn,"s");assert.match(h.controller.render("s",next,next.items[0]),/上次记录，可能已过期/);assert.equal(h.sent.length,1);
});
test("corrupt drafts remain untouched while safe stop and mismatched session behavior stay independent",()=>{
  const h=harness(),key=weaveMessageDraftKey("s","batch","batch:0");h.saved.set(key,"{broken");h.input("new");h.send();assert.equal(h.saved.get(key),"{broken");assert.equal(h.sent.length,0);h.stop();assert.equal(h.sent.length,1);
  h.ctx.sid="other";h.send();h.stop();assert.equal(h.sent.length,1);
});
test("bad service log is independent of healthy connection; stale state cannot stop a previous instance",()=>{
  const h=harness();h.state.persisted=false;h.input("留住");h.send();h.stop();assert.equal(h.sent.length,1);assert.equal(h.sent[0].kind,"weave_stop");h.state.error="offline";h.stop();assert.equal(h.sent.length,1);
  h.state.error="";h.ctx.conn.healthy=false;h.stop();assert.equal(h.sent.length,1);
});
