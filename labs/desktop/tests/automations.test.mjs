import test from "node:test";
import assert from "node:assert/strict";
import {createAutomationController,automationDefinition,automationEligibility,validateAutomationResponse} from "../web/automations.mjs";
const tick=()=>new Promise(r=>setTimeout(r,0));
const definition=()=>({name:"日报",workspace:"/work",model:"scripted",prompt:"检查项目",schedule:{kind:"interval",seconds:60}});
function harness(){
  const state={revision:0,tasks:{},runs:{}},saved=new Map(),sent=[],events=[],opened=[];let failRead=false,lose=false,flushFailure=false;
  const ctx={writable:true,projects:[{workspace:"/work"}],conn:{controller:new AbortController(),localState:{getDraft:k=>saved.get(k),setDraft:(k,v)=>saved.set(k,v),deleteDraft:k=>saved.delete(k),flush:async()=>{events.push("flush");if(flushFailure)throw new Error("disk");}},client:{get:async()=>{if(failRead)throw new Error("offline");return structuredClone({state,warning:"",runner_active:true});},post:async(path,b)=>{events.push("post");sent.push({path,body:structuredClone(b)});let duplicate=false;
    if(path.endsWith("configure")){duplicate=b.create && state.tasks[b.task_id] && JSON.stringify(state.tasks[b.task_id].definition)===JSON.stringify(b.definition);if(!duplicate){if(b.revision!==state.revision)throw Object.assign(new Error("conflict"),{status:409});state.tasks[b.task_id]={definition:structuredClone(b.definition),enabled:false,next_due:1000,warning:""};}}
    else if(path.endsWith("enabled")){const t=state.tasks[path.split("/")[3]];duplicate=t.enabled===b.enabled;if(!duplicate){if(b.revision!==state.revision)throw Object.assign(new Error("conflict"),{status:409});t.enabled=b.enabled;}}
    else {const r=state.runs[path.split("/")[3]];duplicate=r.state==="acknowledged" && r.message===b.note;if(!duplicate){if(b.revision!==state.revision)throw Object.assign(new Error("conflict"),{status:409});r.state="acknowledged";r.message=b.note;}}
    if(!duplicate)state.revision++;if(lose){lose=false;throw new Error("lost receipt");}return {state};}}}};
  const c=createAutomationController({context:()=>ctx,render:()=>{},openSession:s=>opened.push(s)});
  const input=(field,value)=>c.input({target:{dataset:{autoField:field},value}});
  const click=(action,extra={})=>c.click({target:{closest:()=>({dataset:{autoAction:action,...extra}})}});
  const submit=(kind="configure",extra={})=>c.onSubmit({target:{dataset:{autoForm:kind,...extra}},preventDefault:()=>{}});
  return {c,ctx,state,saved,sent,events,opened,input,click,submit,lose:()=>{lose=true;},failRead:()=>{failRead=true;},failFlush:()=>{flushFailure=true;},async form(){await c.load();c.open();for(const [k,v]of Object.entries({name:"日报",model:"scripted",prompt:"检查项目",seconds:"60"}))input(k,v);},task(id="t"){state.tasks[id]={definition:definition(),enabled:false,next_due:1000,warning:""};},run(id="r",status="unknown"){state.runs[id]={task_id:"t",scheduled_at:900,created_at:901,state:status,session_id:"s",run_id:"turn",message:"待核对"};}};
}
test("create persists stable identity before request and remains disabled until separate explicit enable",async()=>{
  const h=harness();await h.form();h.submit();await tick();assert.equal(h.events[0],"flush");assert.equal(h.sent.length,1);const id=h.sent[0].body.task_id;assert.equal(h.sent[0].body.create,true);assert.equal(h.state.tasks[id].enabled,false);assert.match(h.c.render(),/仅在羽案应用打开期间/);h.click("enable",{autoId:id});await tick();assert.equal(h.state.tasks[id].enabled,true);assert.equal(h.sent[1].path,`/desktop/automations/${id}/enabled`);
});
test("lost create receipt restores the exact ID and revision without automatic retry or enabled-state reset",async()=>{
  const h=harness();await h.form();h.lose();h.submit();await tick();const original=h.sent[0];h.state.tasks[original.body.task_id].enabled=true;h.c.reset();await h.c.load();assert.equal(h.sent.length,1);assert.match(h.c.render(),/未自动重发/);h.click("retry");await tick();assert.deepEqual(h.sent[1],original);assert.equal(h.state.tasks[original.body.task_id].enabled,true);
});
test("local persistence failure sends nothing and keeps original configuration and pending request",async()=>{
  const h=harness();await h.form();h.failFlush();h.submit();await tick();assert.equal(h.sent.length,0);assert.match(h.saved.get("automation-definition"),/检查项目/);assert.match(h.saved.get("automation-pending"),/configure/);assert.match(h.c.render(),/disk/);
});
test("CAS conflicts preserve local draft and do not auto-rebase the request",async()=>{
  const h=harness();h.task();await h.c.load();h.c.open("t");h.input("prompt","我的修改");h.state.revision++;h.state.tasks.t.definition.prompt="别人修改";h.submit();await tick();assert.match(h.c.render(),/别人修改/);assert.match(h.c.render(),/我的修改/);const count=h.sent.length;h.click("retry");await tick();assert.equal(h.sent.length,count);h.click("forget");h.submit();await tick();assert.equal(h.sent[1].body.revision,1);assert.equal(h.state.tasks.t.definition.prompt,"我的修改");
});
test("unknown run requires a note, does not imply success, and acknowledgement does not enable",async()=>{
  const h=harness();h.task();h.run();await h.c.load();h.click("enable",{autoId:"t"});assert.equal(h.sent.length,0);h.submit("acknowledge",{autoRun:"r"});assert.equal(h.sent.length,0);h.c.input({target:{dataset:{autoNote:"r"},value:"已核对会话与文件"}});h.submit("acknowledge",{autoRun:"r"});await tick();assert.equal(h.state.runs.r.state,"acknowledged");assert.equal(h.state.tasks.t.enabled,false);assert.match(h.c.render(),/不代表成功/);h.click("session",{autoSession:"s"});assert.deepEqual(h.opened,["s"]);h.click("enable",{autoId:"t"});await tick();assert.equal(h.state.tasks.t.enabled,true);
});
test("running configuration is not editable and disabling does not stop its session",async()=>{
  const h=harness();h.task();h.state.tasks.t.enabled=true;h.run("r","running");await h.c.load();h.c.open("t");assert.doesNotMatch(h.c.render(),/data-auto-form="configure"/);h.click("disable",{autoId:"t"});await tick();assert.equal(h.state.tasks.t.enabled,false);assert.equal(h.state.runs.r.state,"running");assert.equal(h.sent.length,1);assert.match(h.c.render(),/不停止已创建的会话/);
});
test("failed reads retain stale records and prohibit writes until a fresh read",async()=>{
  const h=harness();h.task();await h.c.load();h.failRead();await h.c.load();assert.match(h.c.render(),/日报/);assert.match(h.c.render(),/可能过期/);h.click("enable",{autoId:"t"});assert.equal(h.sent.length,0);
});
test("unrelated or same-task edit never silently discards an existing different draft",async()=>{
  const h=harness();h.task();h.task("other");await h.c.load();h.c.open("t");h.input("prompt","未保存编辑");h.c.open("t");assert.match(h.c.render(),/未保存编辑/);h.c.open("other");assert.match(h.c.render(),/先保存，或明确放弃/);assert.match(h.saved.get("automation-definition"),/未保存编辑/);
});
test("restored corrupt drafts and requests stay untouched and cannot dispatch",async()=>{
  const h=harness();h.saved.set("automation-definition","{bad");h.saved.set("automation-pending","{bad");await h.c.load();h.c.open();h.submit();h.click("retry");assert.equal(h.sent.length,0);assert.equal(h.saved.get("automation-definition"),"{bad");assert.match(h.c.render(),/原数据保留/);
});
test("schedule and external content validation distinguishes true state without injecting markup",async()=>{
  assert.throws(()=>automationDefinition({name:"n",workspace:"/w",model:"m",prompt:"p",kind:"interval",seconds:"59"}),/60/);assert.throws(()=>automationDefinition({name:"n",workspace:"/w",model:"m",prompt:"p",kind:"once",at:"2000-01-01T00:00"}),/未来/);
  assert.throws(()=>validateAutomationResponse({state:{revision:0,tasks:{x:{definition:{},enabled:false,next_due:0,warning:""}},runs:{}},warning:"",runner_active:true}));
  const h=harness();h.task();h.state.tasks.t.definition.name="<script>";h.state.tasks.t.definition.prompt="<img src=x>";await h.c.load();assert.doesNotMatch(h.c.render(),/<script>|<img src=x>/);assert.match(h.c.render(),/&lt;script&gt;/);assert.deepEqual(automationEligibility("t",{runs:{r:{task_id:"t",state:"unknown"}}}),{canEdit:false,canEnable:false});
});
test("explicitly reconciling a lost create receipt allows a later edit under the same saved task",async()=>{
  const h=harness();await h.form();h.lose();h.submit();await tick();const id=h.sent[0].body.task_id;h.input("prompt","核对后修订");h.click("forget");h.submit();await tick();assert.equal(h.sent[1].body.task_id,id);assert.equal(h.sent[1].body.create,false);assert.equal(h.state.tasks[id].definition.prompt,"核对后修订");assert.equal(Object.keys(h.state.tasks).length,1);
});
test("opening the page before native LocalState is ready still restores original drafts and pending request later",async()=>{
  const h=harness();await h.form();h.lose();h.submit();await tick();const original=h.sent[0],savedDraft=h.saved.get("automation-definition"),store=h.ctx.conn.localState;
  h.c.reset();delete h.ctx.conn.localState;h.c.render();await h.c.load();assert.equal(h.sent.length,1);
  h.ctx.conn.localState=store;await h.c.load();h.c.open();assert.match(h.c.render(),/发现待核对的管理请求/);assert.match(h.c.render(),/检查项目/);assert.equal(h.saved.get("automation-definition"),savedDraft);assert.equal(h.sent.length,1);
  h.click("retry");await tick();assert.deepEqual(h.sent[1],original);assert.equal(Object.keys(h.state.tasks).length,1);
});
test('calendar draft preserves IANA time and weekdays without using local browser offset',()=>{
  const d=automationDefinition({name:'daily',workspace:'/w',model:'m',prompt:'work',kind:'calendar',timezone:'Asia/Tokyo',time:'09:30',weekdays:'0,2,4'});
  assert.deepEqual(d.schedule,{kind:'calendar',timezone:'Asia/Tokyo',time:'09:30',weekdays:[0,2,4]});
  assert.throws(()=>automationDefinition({...d,kind:'calendar',timezone:'Bad/Zone',time:'09:30',weekdays:'0'}));
});
test('session automation creates only a disabled draft and preserves competing draft',async()=>{
  const h=harness();await h.c.load();assert.equal(h.c.fromSession({session_id:'source',workspace:'/work',text:'new task',model:'scripted'}),true);assert.equal(h.sent.length,0);assert.equal(Object.keys(h.state.tasks).length,0);assert.match(h.saved.get('automation-definition'),/new task/);
  assert.equal(h.c.fromSession({session_id:'another',workspace:'/other',text:'replace'}),false);assert.match(h.saved.get('automation-definition'),/new task/);assert.doesNotMatch(h.saved.get('automation-definition'),/replace/);
});
