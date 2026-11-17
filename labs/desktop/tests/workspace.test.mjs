import test from "node:test";
import assert from "node:assert/strict";
import {createWorkspaceController,validateWorkspace,organizeWorkspaceSessions,projectContextText,mergeProjectContextDraft,renderProjectArtifacts,projectContextDraftKey} from "../web/workspace.mjs";
const pid="a".repeat(24), other="b".repeat(24);
const payload=()=>({state:{revision:0,projects:{[pid]:{context:"已保存说明"}},sessions:{}},catalogue:{projects:[{project_id:pid,workspace:"/work"},{project_id:other,workspace:"/empty"}],sessions:{s:pid,s2:pid}}});
const tick=()=>new Promise(r=>setTimeout(r,0));
function harness(){
  let remote=payload(),failRead=false,failPost=false,loseReply=false,deferred=null,artifactData={project_id:pid,artifacts:[],truncated:false,warning:""},composer="";
  const saved=new Map(),sent=[],opened=[],cards=[],events=[];
  const ctx={active:true,sid:"s",projectId:pid,writable:true,conn:{controller:new AbortController(),localState:{getDraft:k=>saved.get(k),setDraft:(k,v)=>saved.set(k,v),deleteDraft:k=>saved.delete(k),flush:async()=>events.push("flush")},client:{get:async path=>{if(failRead)throw new Error("offline");return structuredClone(path.includes("artifacts") ? artifactData : remote);},post:async(path,body)=>{sent.push(structuredClone(body));events.push("post");if(deferred)await deferred;if(failPost)throw new Error("offline");const current=remote.state[body.group][body.key];if(JSON.stringify(current)!==JSON.stringify(body.value)){if(body.revision!==remote.state.revision)throw Object.assign(new Error("conflict"),{status:409});remote.state[body.group][body.key]=structuredClone(body.value);remote.state.revision++;}if(loseReply){loseReply=false;throw new Error("lost reply");}return structuredClone(remote);}}}};
  const controller=createWorkspaceController({context:()=>ctx,render:()=>{},getComposer:()=>composer,putComposer:text=>{composer=text;},openSession:sid=>opened.push(sid),artifactCard:(a,ref)=>{cards.push({a,ref});return `<code>${a.id}</code>`;}});
  return {ctx,saved,sent,opened,cards,events,controller,get composer(){return composer;},setComposer:text=>{composer=text;},get remote(){return remote;},setRemote:value=>{remote=value;},input:(text,id=pid)=>controller.input({target:{dataset:{projectContextDraft:id},value:text}}),save:(id=pid)=>controller.onSubmit({target:{dataset:{projectContext:id}},preventDefault:()=>{}}),click:(action,extra={})=>controller.click({target:{closest:()=>({dataset:{workspaceAction:action,projectId:pid,...extra}})}}),failRead:()=>{failRead=true;},failPost:()=>{failPost=true;},lose:()=>{loseReply=true;},defer:p=>{deferred=p;},setArtifacts:value=>{artifactData=value;}};
}
test("metadata sorting is isolated from session execution and empty registered projects exist",async()=>{
  const sessions=[{session_id:"a",updated_at:4},{session_id:"b",updated_at:1},{session_id:"c",updated_at:8}],meta={b:{pinned:true},c:{archived:true}};
  assert.deepEqual(organizeWorkspaceSessions(sessions,meta).map(s=>s.session_id),["b","a"]);assert.deepEqual(organizeWorkspaceSessions(sessions,meta,{view:"archived"}).map(s=>s.session_id),["c"]);assert.equal(sessions.length,3);
  const h=harness();await h.controller.load();assert.equal(h.controller.projects().length,2);assert.match(h.controller.renderProject(other),/empty/);assert.equal(h.sent.length,0);
});
test("saving project metadata flushes original CAS request and never injects or sends a model prompt",async()=>{
  const h=harness();await h.controller.load();h.input("新说明");h.save();await tick();assert.deepEqual(h.sent,[{revision:0,group:"projects",key:pid,value:{context:"新说明"}}]);assert.equal(h.events[0],"flush");assert.equal(h.composer,"");assert.equal(h.remote.state.projects[pid].context,"新说明");
  h.click("place");assert.match(h.composer,/新说明/);assert.match(h.composer,/由我明确加入/);assert.equal(h.sent.length,1);
});
test("context requires explicit append or replacement and a same-project target session",async()=>{
  const h=harness();await h.controller.load();h.setComposer("原任务");h.click("place");assert.equal(h.composer,"原任务");assert.match(h.controller.renderProject(pid),/当前任务已有不同草稿/);h.click("append");assert.match(h.composer,/^原任务/);assert.match(h.composer,/已保存说明/);
  h.ctx.sid="missing";const old=h.composer;h.click("place");assert.equal(h.composer,old);assert.match(h.controller.renderProject(pid),/选择同项目会话/);
  assert.equal(mergeProjectContextDraft("a","b"),null);assert.equal(mergeProjectContextDraft("a","b","replace"),"b");assert.match(projectContextText({workspace:"/work"},"text"),/不是额外的工具授权/);
});
test("CAS conflict keeps local text and fresh remote value; explicit replace is a new reviewed request",async()=>{
  const h=harness();await h.controller.load();h.input("本地编辑");h.remote.state.projects[pid].context="远端更新";h.remote.state.revision=1;h.save();await tick();assert.match(h.controller.renderProject(pid),/远端更新/);assert.match(h.controller.renderProject(pid),/本地编辑/);assert.equal(h.remote.state.projects[pid].context,"远端更新");
  assert.match(h.saved.get(projectContextDraftKey(pid)),/本地编辑/);h.click("replace-context");await tick();assert.equal(h.sent[1].revision,1);assert.equal(h.remote.state.projects[pid].context,"本地编辑");
});
test("lost response retries exact revision and value only on explicit action, including restart",async()=>{
  const h=harness();await h.controller.load();h.input("已送达");h.lose();h.save();await tick();const original=h.sent[0];assert.equal(h.remote.state.projects[pid].context,"已送达");assert.match(h.controller.renderProject(pid),/原请求与原文保留/);h.controller.reset();await h.controller.load();assert.equal(h.sent.length,1);h.click("retry");await tick();assert.deepEqual(h.sent[1],original);assert.equal(h.remote.state.revision,1);
});
test("edits made during saving remain as unsaved draft against the newly accepted base",async()=>{
  const h=harness();await h.controller.load();h.input("提交文");let release;h.defer(new Promise(r=>release=r));h.save();await tick();h.input("新编辑");release();await tick();assert.equal(h.remote.state.projects[pid].context,"提交文");assert.match(h.saved.get(projectContextDraftKey(pid)),/新编辑/);assert.doesNotMatch(h.controller.renderProject(pid),/服务中的项目说明已有变化/);
});
test("pin and archive change only exact boolean metadata and restore independently",async()=>{
  const h=harness();await h.controller.load();h.click("pin",{workspaceSession:"s"});await tick();h.click("archive",{workspaceSession:"s"});await tick();assert.deepEqual(h.remote.state.sessions.s,{pinned:true,archived:true});assert.match(h.controller.renderSessionActions("s"),/恢复到工作台/);h.click("archive",{workspaceSession:"s"});await tick();assert.deepEqual(h.remote.state.sessions.s,{pinned:true,archived:false});assert.equal(h.composer,"");
});
test("project artifacts preserve exact owner reference and explain truncation and stale failures",async()=>{
  const h=harness();await h.controller.load();h.setArtifacts({project_id:pid,artifacts:[{session_id:"s2",source:{batch_id:"b",item_id:"b:0"},artifact:{id:"a1",path:"<img>"},created_at:"now"}],truncated:true,warning:"有限历史"});await h.controller.loadArtifacts(pid);const html=h.controller.renderProject(pid);assert.match(html,/成果列表已截断/);assert.deepEqual(h.cards[0].ref,{session_id:"s2",batch_id:"b",item_id:"b:0"});h.click("source",{workspaceSession:"s2"});assert.deepEqual(h.opened,["s2"]);
  h.failRead();await h.controller.loadArtifacts(pid);assert.match(h.controller.renderProject(pid),/保留上次结果，可能过期/);
  const safe=renderProjectArtifacts({project_id:pid,data:{artifacts:[],truncated:false,warning:"<script>"}});assert.doesNotMatch(safe,/<script>/);assert.match(safe,/&lt;script&gt;/);
});
test("wrong project artifacts and malformed metadata never become empty successful state",async()=>{
  const h=harness();await h.controller.load();h.setArtifacts({project_id:other,artifacts:[]});await h.controller.loadArtifacts(pid);assert.match(h.controller.renderProject(pid),/成果来源不属于此项目/);assert.throws(()=>validateWorkspace({state:{},catalogue:{}}));
});
test("corrupt saved update and context are preserved rather than replaced",async()=>{
  const h=harness();h.saved.set(`workspace-update:projects:${pid}`,"{bad");h.saved.set(projectContextDraftKey(pid),"{bad");await h.controller.load();h.input("overwrite");h.save();await tick();assert.equal(h.sent.length,0);assert.equal(h.saved.get(projectContextDraftKey(pid)),"{bad");assert.match(h.controller.renderProject(pid),/原数据未覆盖/);
});
test("oversized restored context remains editable and can be shortened without losing its text",async()=>{
  const h=harness(),text="长".repeat(20001);h.saved.set(projectContextDraftKey(pid),JSON.stringify({text,base:"已保存说明"}));await h.controller.load();h.save();await tick();assert.equal(h.sent.length,0);assert.match(h.controller.renderProject(pid),/请缩短后保存/);assert.doesNotMatch(h.controller.renderProject(pid),/ readonly/);assert.equal(JSON.parse(h.saved.get(projectContextDraftKey(pid))).text,text);
  h.input("缩短后说明");h.save();await tick();assert.equal(h.remote.state.projects[pid].context,"缩短后说明");
});
test("a different pending metadata request cannot consume a project's conflict decision",async()=>{
  const h=harness();await h.controller.load();h.input("本地编辑");h.remote.state.projects[pid].context="远端编辑";h.remote.state.revision++;await h.controller.load();h.failPost();h.click("pin",{workspaceSession:"s"});await tick();const before=h.saved.get(projectContextDraftKey(pid));h.click("replace-context");await tick();assert.equal(h.saved.get(projectContextDraftKey(pid)),before);assert.match(h.controller.renderProject(pid),/服务中的项目说明已有变化/);assert.equal(h.sent.length,1);
});

test('compact sidebar controls keep metadata actions and disable writes while pending',async()=>{
  const h=harness();await h.controller.load();
  assert.equal(h.controller.renderSessionActions('missing',{compact:true}),'');
  let html=h.controller.renderSessionActions('s',{compact:true});
  assert.match(html,/aria-label="置顶"/);assert.match(html,/aria-label="归档"/);
  assert.doesNotMatch(html,/class="note"|<div class="actions">/);
  let release;h.defer(new Promise(resolve=>release=resolve));h.click('pin',{workspaceSession:'s'});await tick();
  assert.match(h.controller.renderSessionActions('s2',{compact:true}),/disabled/);
  release();await tick();assert.match(h.controller.renderSessionActions('s',{compact:true}),/aria-label="取消置顶"/);
  h.click('archive',{workspaceSession:'s'});await tick();assert.match(h.controller.renderSessionActions('s',{compact:true}),/aria-label="恢复到列表"/);
  h.ctx.writable=false;assert.match(h.controller.renderSessionActions('s',{compact:true}),/disabled/);
});

test('archive page searches only archived conversations and restores metadata without changing content',async()=>{
  const h=harness();await h.controller.load();
  const rows=[{session_id:'s',title:'已保存 <任务>',workspace:'/work'},{session_id:'s2',title:'活动对话',workspace:'/work'}];
  assert.match(h.controller.renderArchived(rows),/暂无归档对话/);
  h.click('pin',{workspaceSession:'s'});await tick();h.click('archive',{workspaceSession:'s'});await tick();
  const html=h.controller.renderArchived(rows);
  assert.match(html,/已保存 &lt;任务&gt;/);assert.doesNotMatch(html,/活动对话/);
  assert.match(html,/data-workspace-action="source" data-workspace-session="s"/);
  assert.match(h.controller.renderArchived(rows,{query:'not found'}),/没有找到匹配的对话/);
  assert.match(h.controller.renderArchived(rows,{query:'/WORK'}),/archive-row/);
  h.click('source',{workspaceSession:'s'});assert.deepEqual(h.opened,['s']);
  h.click('archive',{workspaceSession:'s'});await tick();
  assert.deepEqual(h.remote.state.sessions.s,{pinned:true,archived:false});
  assert.match(h.controller.renderArchived(rows),/暂无归档对话/);assert.equal(h.composer,'');
});
test('archive read failure never claims the archive is empty',async()=>{
  const h=harness();h.failRead();await h.controller.load();
  const html=h.controller.renderArchived([]);
  assert.match(html,/暂时无法读取归档对话/);assert.doesNotMatch(html,/暂无归档对话/);
});

function deletionHarness(){
  let remote=payload(),answer=false,confirmWait=null;
  const rows=[{session_id:'s',title:'First'},{session_id:'s2',title:'Second'}],sent=[],confirmations=[],deleted=[];
  remote.state.sessions={s:{pinned:false,archived:true},s2:{pinned:false,archived:true}};
  const ctx={writable:true,conn:{controller:new AbortController(),client:{
    get:async()=>structuredClone(remote),
    post:async(path,body)=>{
      sent.push({path,body:structuredClone(body)});
      if(body.revision!==remote.state.revision)throw Object.assign(new Error('changed'),{status:409});
      for(const id of body.session_ids){delete remote.state.sessions[id];delete remote.catalogue.sessions[id];}
      remote.state.revision++;
      return {...structuredClone(remote),deleted:body.session_ids,incomplete:false};
    }
  }}};
  const controller=createWorkspaceController({context:()=>ctx,render:()=>{},confirmDelete:async c=>{confirmations.push(c);return confirmWait?await confirmWait:answer;},onDeleted:ids=>deleted.push(...ids)});
  const click=(action,sid)=>controller.click({target:{closest:()=>({dataset:{workspaceAction:action,workspaceSession:sid}})}});
  return {controller,ctx,remote,rows,sent,confirmations,deleted,click,answer:value=>answer=value,wait:value=>confirmWait=value};
}
test('archive delete cancellation is inert; single confirmation sends only the selected id',async()=>{
  const h=deletionHarness();await h.controller.load();
  assert.match(h.controller.renderArchived(h.rows),/全部删除/);
  h.click('delete-archived','s');await tick();assert.equal(h.sent.length,0);
  assert.deepEqual(h.confirmations[0],{count:1,title:'First',all:false});
  h.answer(true);h.click('delete-archived','s');await tick();
  assert.deepEqual(h.sent[0],{path:'/desktop/archived-sessions/delete',body:{revision:0,session_ids:['s']}});
  assert.deepEqual(h.deleted,['s']);assert.equal(h.remote.state.sessions.s2.archived,true);
});
test('delete all uses all archived ids despite search, and freezes targets before confirmation',async()=>{
  const h=deletionHarness();await h.controller.load();h.controller.renderArchived(h.rows,{query:'First'});
  let confirm;h.wait(new Promise(r=>confirm=r));h.click('delete-all-archived');await tick();
  assert.equal(h.confirmations[0].count,2);assert.equal(h.confirmations[0].all,true);
  h.remote.state.sessions.s3={pinned:false,archived:true};h.remote.catalogue.sessions.s3=pid;
  confirm(true);await tick();
  assert.deepEqual(h.sent[0].body.session_ids,['s','s2']);assert.ok(h.remote.state.sessions.s3);
});
test('changed revision and workspace switches cannot delete newly selected data',async()=>{
  const h=deletionHarness();await h.controller.load();h.controller.renderArchived(h.rows);
  let confirm;h.wait(new Promise(r=>confirm=r));h.click('delete-all-archived');await tick();
  h.remote.state.revision++;confirm(true);await tick();
  assert.equal(h.deleted.length,0);assert.match(h.controller.renderArchived(h.rows),/未自动重试/);
  h.wait(new Promise(r=>confirm=r));h.click('delete-all-archived');await tick();
  h.controller.reset();confirm(true);await tick();assert.equal(h.sent.length,1);
});

test('archive groups use project identity and combine project selection with search',async()=>{
  const h=harness();
  h.remote.catalogue.projects[0].name='同名项目';h.remote.catalogue.projects[1].name='同名项目';
  h.remote.catalogue.sessions.s2=other;
  h.remote.state.sessions={s:{archived:true,pinned:false},s2:{archived:true,pinned:false},loose:{archived:true,pinned:false}};
  const rows=[{session_id:'s',title:'Alpha <one>'},{session_id:'s2',title:'Beta'},{session_id:'loose',title:'Loose'}];
  await h.controller.load();
  let html=h.controller.renderArchived(rows);
  assert.equal((html.match(/class="archive-group"/g)||[]).length,3);
  assert.match(html,/未分配项目/);assert.match(html,/Alpha &lt;one&gt;/);
  h.controller.input({target:{id:'archive-project',value:other}});
  html=h.controller.renderArchived(rows);
  assert.match(html,/Beta/);assert.doesNotMatch(html,/Alpha|Loose/);
  assert.match(h.controller.renderArchived(rows,{query:'alpha'}),/没有找到匹配的对话/);
  assert.match(h.controller.renderArchived(rows,{query:'同名项目'}),/Beta/);
  h.click('archive',{workspaceSession:'s2'});await tick();
  assert.match(h.controller.renderArchived(rows),/没有找到匹配的对话/);
  assert.match(h.controller.renderArchived(rows),new RegExp(`value="${other}" selected`));
  h.controller.reset();await h.controller.load();
  assert.match(h.controller.renderArchived(rows),/Alpha/);
});

test('project filter does not narrow the explicitly global delete-all operation',async()=>{
  const h=deletionHarness();h.remote.catalogue.sessions.s2=other;
  await h.controller.load();h.controller.input({target:{id:'archive-project',value:other}});
  const html=h.controller.renderArchived(h.rows);
  assert.doesNotMatch(html,/First/);assert.match(html,/包括筛选之外的对话/);
  h.answer(true);h.click('delete-all-archived');await tick();
  assert.equal(h.confirmations[0].count,2);
  assert.deepEqual(h.sent[0].body.session_ids,['s','s2']);
});
