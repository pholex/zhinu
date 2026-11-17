import test from "node:test";
import assert from "node:assert/strict";
import {composerControls,conversationMessage,composerEnterAction} from "../web/conversation_ui.mjs";

test("composer changes from send to pending to stop, preserving deliberate supplemental input",()=>{
  assert.deepEqual(composerControls({hasText:true}),{sendHidden:false,sendLabel:"发送",sendSymbol:"arrow",stopHidden:true,stopLabel:"停止生成",stopSymbol:"stop"});
  assert.equal(composerControls({pending:true}).sendSymbol,"loading");
  assert.equal(composerControls({busy:true}).sendHidden,true);
  assert.equal(composerControls({busy:true}).stopHidden,false);
  assert.equal(composerControls({busy:true,hasText:true}).sendLabel,"发送补充");
  assert.equal(composerControls({busy:true,hasText:true}).sendHidden,false);
  assert.equal(composerControls({busy:true,hasText:true,directCollaboration:true}).sendHidden,true);
  assert.equal(composerControls({busy:true,stopPending:true}).stopSymbol,"loading");
  assert.equal(composerControls().stopHidden,true);
});
test("messages escape user input and expose copy only for finished nonempty answers",()=>{
  const user=conversationMessage({kind:"user",text:'<img src=x onerror=alert(1)>'},'user');
  assert.match(user,/&lt;img/);assert.doesNotMatch(user,/<img|data-copy-message/);
  const answer=conversationMessage({kind:"assistant",text:"**回答**"},'answer');
  assert.match(answer,/<strong>回答<\/strong>/);assert.match(answer,/data-copy-message="answer"/);
  assert.doesNotMatch(conversationMessage({kind:"assistant",text:"尚在生成"},'live',{copyable:false}),/data-copy-message/);
  assert.doesNotMatch(conversationMessage({kind:"assistant",text:""},'empty'),/data-copy-message/);
});

test("Enter sends by default, newline modifiers win, composition never sends",()=>{
  const enter={key:"Enter"};
  assert.equal(composerEnterAction(enter),"send");
  assert.equal(composerEnterAction({...enter,shiftKey:true}),"newline");
  assert.equal(composerEnterAction({...enter,altKey:true}),"newline");
  assert.equal(composerEnterAction({...enter,altKey:true,metaKey:true}),"newline");
  assert.equal(composerEnterAction({...enter,isComposing:true}),null);
  assert.equal(composerEnterAction({...enter,keyCode:229}),null);
  assert.equal(composerEnterAction(enter,"mod-enter"),"newline");
  assert.equal(composerEnterAction({...enter,metaKey:true},"mod-enter"),"send");
});

test("tool activity stays between preamble and answer, merges progress, and escapes output", async()=>{
  const {liveTimeline,historyTimeline,activityHTML}=await import('../web/conversation_ui.mjs');
  const blocks=liveTimeline([
    {kind:'run.started',seq:1,prompt:'查版本'},
    {kind:'text.delta',seq:2,text:'我查一下。'},
    {kind:'tool.pending',seq:3,name:'status',args:{path:'a'}},
    {kind:'tool.running',seq:4,name:'status'},
    {kind:'tool.progress',seq:5,name:'status',message:'读取中'},
    {kind:'tool.completed',seq:6,name:'status',output:'<script>bad</script>',ok:true,seconds:1},
    {kind:'text.delta',seq:7,text:'版本是 1。'},
  ]);
  assert.deepEqual(blocks.map(b=>b.kind),['user','assistant','tool','assistant']);
  assert.equal(blocks[2].status,'completed');
  assert.match(activityHTML(blocks[2]),/&lt;script&gt;/);
  assert.doesNotMatch(activityHTML(blocks[2]),/<script>|<details[^>]* open/);
  assert.equal(historyTimeline([{id:'t',role:'tool',name:'status',content:'{}',ok:true}])[0].kind,'tool');
});

test('ordinary replies have no process chrome; tools fold before the conclusion and keep real duration',async()=>{
  const {historyTimeline,timelineHTML}=await import('../web/conversation_ui.mjs');
  const render=(b,key)=>conversationMessage(b,key);
  assert.doesNotMatch(timelineHTML(historyTimeline([{role:'user',id:'u',content:'ping'},{role:'assistant',id:'a',content:'pong'}]),render),/turn-activity|用时|运行详情/);
  const rows=[{role:'user',id:'u',content:'检查'},{role:'assistant',id:'p',run_id:'r',content:'读取中'},{role:'tool',id:'t',run_id:'r',name:'read_file',content:'{}',ok:true},{role:'assistant',id:'a',run_id:'r',content:'完成'}];
  const html=timelineHTML(historyTimeline(rows),render,{runs:[{id:'r',started_at:10,finished_at:31}]});
  assert.match(html,/用时 21 秒/);
  assert.ok(html.indexOf('turn-activity')<html.indexOf('data-message-id="a"'));
  assert.doesNotMatch(html,/<details[^>]* open/);
  assert.match(historyTimeline([{role:'user',id:'image',content:'看看',has_attachments:true}])[0].text,/含附件/);
});


test('runtime metadata query has a readable title and keeps its result folded',async()=>{
  const {activityHTML}=await import('../web/conversation_ui.mjs');
  const html=activityHTML({id:'runtime',name:'get_runtime_status',status:'completed',text:'{"scope":"current_session"}'});
  assert.match(html,/查看运行状态/);
  assert.match(html,/current_session/);
  assert.doesNotMatch(html,/<details[^>]* open/);
});


test('normal submission stays in composer; uncertain, failed and recovered receipts remain visible',async()=>{
  const {showSubmissionReceipt}=await import('../web/conversation_ui.mjs');
  for(const kind of ['create','prompt','steer']){
    for(const state of ['sending','accepted'])assert.equal(showSubmissionReceipt({kind,state}),false);
    for(const state of ['ready','unknown','rejected','recorded'])assert.equal(showSubmissionReceipt({kind,state}),true);
  }
  assert.equal(showSubmissionReceipt({kind:'prompt',state:'sending'},{owned:false}),true);
  assert.equal(showSubmissionReceipt({kind:'create',state:'sending',workflow:'weave'}),true);
  assert.equal(showSubmissionReceipt({kind:'create',state:'accepted',body:{fork_from:{}}}),true);
  assert.equal(showSubmissionReceipt({kind:'approval',state:'sending'}),true);
});

test('a delayed submission never shows a top receipt, but a lost response immediately does',async()=>{
  const {Outbox}=await import('../web/client.mjs');
  const {showSubmissionReceipt}=await import('../web/conversation_ui.mjs');
  const data=new Map(),storage={getItem:k=>data.get(k),setItem:(k,v)=>data.set(k,v)};
  const outbox=new Outbox(storage,'flash');
  outbox.prepare('s:prompt',{kind:'prompt',sid:'s',path:'/session/s/prompt_async',body:{text:'hello'}});
  let finish;
  const request=outbox.send('s:prompt',{post:()=>new Promise(resolve=>{finish=resolve;})});
  await Promise.resolve();
  assert.equal(showSubmissionReceipt(outbox.items['s:prompt']),false);
  finish({session_id:'s'});await request;
  assert.equal(showSubmissionReceipt(outbox.items['s:prompt']),false);
  outbox.forget('s:prompt');
  outbox.prepare('s:prompt',{kind:'prompt',sid:'s',path:'/session/s/prompt_async',body:{text:'hello'}});
  await assert.rejects(outbox.send('s:prompt',{post:async()=>{throw new Error('lost response');}}));
  assert.equal(outbox.items['s:prompt'].state,'unknown');
  assert.equal(showSubmissionReceipt(outbox.items['s:prompt']),true);
});
