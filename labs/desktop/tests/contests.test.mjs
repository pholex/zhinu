import test from "node:test";
import assert from "node:assert/strict";
import {renderContestForm,renderContests,contestRequest,buildContestFollowup,mergeContestDraft,createContestController,contestDraftKey} from "../web/contests.mjs";

const caps = () => ({available:true,can_start:true,specs:[{name:"reader",default_model:"worker"}],models:["worker","judge"],default_model:"fallback",default_judge_model:"judge",min_candidates:2,max_candidates:6});
const draft = () => ({spec:"reader",task:"完成任务",criteria:"可靠",judge_model:"",seats:[{id:"one",model:""},{id:"two",model:"special"}]});
const contest = () => ({contest_id:"c",spec:"reader",task:"任务",criteria:"准确",phase:"settled",final:true,revision:99,selection_revision:2,candidates:[{id:"c:1",index:1,status:"completed",model:"one",answer:"方案甲",artifacts:[{id:"a",path:"result.md"}]},{id:"c:2",index:2,status:"completed",model:"two",answer:"方案乙"}],judge:{status:"completed",recommendation_id:"c:2",answer:"推荐乙"},selection:{candidate_id:"c:1",reason:"适合现状",sequence:2},selections:[{candidate_id:"c:2",reason:"初选",sequence:1},{candidate_id:"c:1",reason:"适合现状",sequence:2}]});
const view = c => ({contests:[c],choices:new Map(),followups:new Map(),cursor:null,total:1,persisted:true});

test("form inherits configured defaults and never submits without preview", () => {
  assert.deepEqual(contestRequest(draft(),caps()),{spec:"reader",task:"完成任务",criteria:"可靠",models:["worker","special"],judge_model:"judge"});
  const state = {open:true,draft:draft()};
  assert.doesNotMatch(renderContestForm(state,caps()),/contest-confirm/);
  assert.match(renderContestForm(state,{...caps(),can_start:false}),/type="submit" disabled/);
  assert.match(renderContestForm(state,{...caps(),available:false}),/type="submit" disabled/);
  state.preview = {...contestRequest(state.draft,caps()),count:2};
  assert.match(renderContestForm(state,caps(),true),/id="contest-confirm"[^>]*disabled/);
});
test("recommendation and persisted adoption remain separate; artifact owners are exact", () => {
  const refs = [], html = renderContests(view(contest()),{writable:true,artifactCard:(a,ref) => {refs.push(ref);return "file";}});
  assert.match(html,/判官推荐：候选 2/); assert.match(html,/当前已采纳：候选 1/);
  assert.match(html,/采纳历史（2）/); assert.match(html,/采纳方案不自动应用文件改动/);
  assert.deepEqual(refs,[{contest_id:"c",candidate_id:"c:1"}]);
});
test("failed judge and partial candidate never imply a valid recommendation or selectable success", () => {
  const c = contest(); c.judge.status = "failed"; c.candidates[1].status = "partial";
  const html = renderContests(view(c),{writable:true});
  assert.match(html,/判官未给出有效推荐/); assert.doesNotMatch(html,/判官推荐：候选/);
  assert.match(html,/id="pick-c:2"[^>]*disabled/);
});
test("stale, nonpersistent, unfinished or interrupted records cannot be adopted", () => {
  for (const patch of [{listError:"offline"},{persisted:false}]) {
    assert.match(renderContests({...view(contest()),...patch},{writable:true}),/id="pick-c:1"[^>]*disabled/);
  }
  for (const patch of [{final:false},{interrupted_by_restart:true}]) assert.match(renderContests(view({...contest(),...patch}),{writable:true}),/id="pick-c:1"[^>]*disabled/);
});
test("candidate and model text remain escaped, including saved followup preview", () => {
  const c = contest(); c.task = '<img src=x onerror="bad()">'; c.candidates[0].answer = "<script>bad()</script>";
  const html = renderContests(view(c),{writable:true});
  assert.doesNotMatch(html,/<script>|<img src=x/); assert.match(html,/&lt;script&gt;/);
});
test("followup is saved selected text with provenance and truncation warning, never an automatic send", () => {
  const c = contest(); c.candidates[0].answer_truncated = true;
  const text = buildContestFollowup(c,"先核对");
  assert.match(text,/c:1/); assert.match(text,/方案甲/); assert.doesNotMatch(text,/方案乙/);
  assert.match(text,/正文被截断/); assert.match(text,/不恢复原子任务/);
  assert.throws(() => buildContestFollowup({...c,selection:null}));
  assert.equal(mergeContestDraft("原草稿",text),null);
  assert.equal(mergeContestDraft("原草稿",text,"append"),`原草稿\n\n${text}`);
  assert.equal(mergeContestDraft("原草稿",text,"replace"),text);
});

function harness() {
  const storage = new Map([[contestDraftKey("s"),JSON.stringify(draft())]]), sent = [];
  let snapshot = contest(), getError = false, composer = "已有草稿";
  const ctx = {sid:"s",active:true,writable:true,session:{busy:false},pending:[],conn:{health:{contest_protocol:1},controller:new AbortController(),localState:{getDraft:k=>storage.get(k),setDraft:(k,v)=>storage.set(k,v),deleteDraft:k=>storage.delete(k)},client:{get:async path => {if(getError)throw new Error("offline");return path.endsWith("capabilities") ? caps() : {contests:[snapshot],next_cursor:null,total:1,persisted:true};},post:async (path,body)=>({...body,preview_token:"token",count:2})}}};
  const controller = createContestController({context:()=>ctx,render:()=>{},submit:(scope,a)=>sent.push({scope,...a}),artifactCard:()=>"",getComposer:()=>composer,putComposer:t=>{composer=t;}});
  const click = (action,cid) => controller.click({target:{closest:()=>({dataset:{contestAction:action,contestId:cid}})}});
  const pick = (id,reason) => {controller.input({target:{dataset:{contestPick:id,contestId:"c"},closest:()=>null}});controller.input({target:{dataset:{contestReason:"c"},value:reason,closest:()=>null}});};
  const select = () => controller.onSubmit({target:{dataset:{contestSelection:"c"}},preventDefault:()=>{}});
  return {controller,ctx,storage,sent,click,pick,select,composer:()=>composer,setSnapshot:c=>{snapshot=c;},fail:()=>{getError=true;}};
}
test("selection uses independent revision and idempotent action metadata; pending and busy prevent sends", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.pick("c:2","更适合"); h.select();
  assert.equal(h.sent.length,1); assert.equal(h.sent[0].kind,"selection"); assert.equal(h.sent[0].contestId,"c");
  assert.deepEqual(h.sent[0].body,{candidate_id:"c:2",reason:"更适合",expected_revision:2});
  h.ctx.pending = [h.sent[0]]; h.select(); assert.equal(h.sent.length,1);
  h.ctx.pending = []; h.ctx.session.busy = true; h.select(); assert.equal(h.sent.length,1);
});
test("stale load cannot adopt or prepare a followup from an old snapshot", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.pick("c:2","reason");
  h.fail(); await h.controller.load(h.ctx.conn,"s"); h.select(); h.click("draft","c"); h.click("prepare-draft","c"); h.click("place-draft","c");
  assert.equal(h.sent.length,0); assert.equal(h.composer(),"已有草稿");
});
test("followup conflict leaves old draft intact and requires explicit append; changed selection invalidates preview", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); h.click("draft","c"); h.click("prepare-draft","c"); h.click("place-draft","c");
  assert.equal(h.composer(),"已有草稿"); assert.equal(h.sent.length,0);
  h.click("append-draft","c"); assert.match(h.composer(),/^已有草稿\n\n请根据/);
  h.click("draft","c"); h.click("prepare-draft","c"); const before = h.composer();
  const changed = contest(); changed.selection = {...changed.selection,candidate_id:"c:2",sequence:3}; h.setSnapshot(changed); await h.controller.load(h.ctx.conn,"s");
  h.click("replace-draft","c"); assert.equal(h.composer(),before);
});
test("accepted receipt cleanup keeps a different newly edited form draft", () => {
  const h = harness(), key = contestDraftKey("s"), old = h.storage.get(key);
  h.storage.set(key,"new draft"); h.controller.accepted(h.ctx.conn,{kind:"contest",state:"accepted",sid:"s",draftKey:key,draftText:old});
  assert.equal(h.storage.get(key),"new draft");
  h.storage.set(key,old); h.controller.accepted(h.ctx.conn,{kind:"contest",state:"accepted",sid:"s",draftKey:key,draftText:old});
  assert.equal(h.storage.has(key),false);
});

const settle = () => new Promise(resolve => setImmediate(resolve));
const previewEvent = () => ({target:{id:"contest-form",dataset:{}},preventDefault:()=>{}});
test("preview is read only; confirmation retains its token in the idempotent action", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s");
  h.controller.onSubmit(previewEvent()); await settle();
  assert.equal(h.sent.length,0); h.click("submit"); assert.equal(h.sent.length,1);
  assert.equal(h.sent[0].kind,"contest"); assert.equal(h.sent[0].body.preview_token,"token");
  assert.deepEqual(h.sent[0].body.models,["worker","special"]);
  h.ctx.pending = [h.sent[0]]; h.click("submit"); assert.equal(h.sent.length,1);
});
test("editing during preview or switching connection discards the stale response", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s"); let finish;
  h.ctx.conn.client.post = () => new Promise(resolve => {finish = resolve;});
  h.controller.onSubmit(previewEvent());
  h.controller.input({target:{dataset:{},name:"task",value:"新任务",closest:()=>({})}});
  finish({preview_token:"stale"}); await settle(); h.click("submit"); assert.equal(h.sent.length,0);
  h.controller.onSubmit(previewEvent()); h.ctx.conn = {...h.ctx.conn}; h.controller.reset();
  finish({preview_token:"old-connection"}); await settle(); h.click("submit"); assert.equal(h.sent.length,0);
});
test("expired previews require a new preview and never replace a retry request", async () => {
  const h = harness(); await h.controller.load(h.ctx.conn,"s");
  h.controller.onSubmit(previewEvent()); await settle(); h.click("submit");
  const action = h.sent[0]; h.controller.rejected(action,{status:409}); h.click("submit");
  assert.equal(h.sent.length,1); assert.equal(action.body.preview_token,"token");
});
