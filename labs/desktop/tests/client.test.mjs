import test from "node:test";
import assert from "node:assert/strict";
import {sessionActionAllowed, ReadClient, endpoint, stateOf, visibleSessions, eventWindow, mergeEvents, escapeHTML, reconcileAcceptedDraft} from "../web/client.mjs";

test("desktop management routes use real client methods and preserve JSON, bearer, key and service instance", async()=>{
  const calls=[],client=new ReadClient("http://127.0.0.1:8420","local-fixture",async(url,options)=>{calls.push({url,...options});return {ok:true,json:async()=>({ok:true})};});
  const reads=["/desktop/automations","/desktop/connections","/desktop/connections/c/authorizations/a","/session/s/connections"];
  for(const path of reads)await client.get(path);
  const writes=[
    ["/desktop/automations/configure",{revision:2,task_id:"stable-id",create:true,definition:{name:"定时核对",workspace:"/w",model:"scripted",prompt:"检查",schedule:{kind:"interval",seconds:60}}}],
    ["/desktop/automations/task/enabled",{revision:3,enabled:true}],
    ["/desktop/automation-runs/run/acknowledge",{revision:4,note:"已核对"}],
    ["/desktop/connections",{revision:0,connection_id:"stable-connection",definition:{name:"工具",transport:"http",url:"https://example.test/mcp"}}],
    ["/desktop/connections/c/delete",{revision:1}],
    ["/desktop/connections/c/authorizations",{session_id:"s"}],
    ["/desktop/connections/c/authorizations/a/cancel",{}],
    ["/desktop/connections/c/revoke",{session_id:"s"}],
  ];
  for(const [path,body]of writes)await client.post(path,body);
  for(const action of ["mount","start","stop","remove","reconnect","approve_changes"]){
    const body={action,configuration_revision:7,...(action==="approve_changes" ? {expected_fingerprint:"reviewed-declaration"} : {})};
    await client.post("/session/s/connections/c",body,{key:`key-${action}`,instance:"current-service"});
    const call=calls.at(-1);assert.equal(call.headers["Idempotency-Key"],`key-${action}`);assert.equal(call.headers["X-Xiaoyu-Instance"],"current-service");assert.deepEqual(JSON.parse(call.body),body);
  }
  for(let i=0;i<calls.length;i++){const call=calls[i];assert.equal(call.method,i<reads.length ? "GET" : "POST");assert.equal(call.headers.Authorization,"Bearer local-fixture");assert.equal(call.redirect,"error");assert.equal(call.credentials,"omit");assert.equal(call.cache,"no-store");assert.ok(call.url.startsWith("http://127.0.0.1:8420/"));}
  for(let i=0;i<writes.length;i++)assert.deepEqual(JSON.parse(calls[reads.length+i].body),writes[i][1]);
  for(const call of calls.slice(0,reads.length)){assert.equal(call.body,undefined);assert.equal(call.headers["Idempotency-Key"],undefined);assert.equal(call.headers["X-Xiaoyu-Instance"],undefined);}
});

test("management client never exposes native authorization URLs, callbacks, arbitrary credentials or wrong methods",async()=>{
  let requests=0;const client=new ReadClient("http://localhost","fixture",async()=>{requests++;return {ok:true,json:async()=>({})};});
  for(const path of ["/desktop/connections/c/authorizations/a/open-url","/desktop/oauth/callback/a","/desktop/oauth/callback/a?code=private","/desktop/connections/c/token","/desktop/connections/c/credentials"]){await assert.rejects(client.get(path));await assert.rejects(client.post(path,{}));}
  for(const path of ["/desktop/automations/configure","/desktop/automations/t/enabled","/desktop/automation-runs/r/acknowledge","/desktop/connections/c/delete","/desktop/connections/c/revoke","/desktop/connections/c/authorizations","/desktop/connections/c/authorizations/a/cancel","/session/s/connections/c"])await assert.rejects(client.get(path));
  for(const path of ["/desktop/automations","/desktop/connections/c/authorizations/a","/session/s/connections","/desktop/automations/t/run-now","/session/s/connections/c/mount","/desktop/connections/c/force"])await assert.rejects(client.post(path,{}));
  assert.equal(requests,0);
});

test("management paths reject traversal, encoded separators, queries and fragments before transport",async()=>{
  const calls=[],client=new ReadClient("http://localhost","fixture",async(url)=>{calls.push(url);return {ok:true,json:async()=>({})};});
  const invalid=[
    "/desktop/connections/c%2Fa/authorizations/a","/desktop/connections/c%5Ca/authorizations/a","/desktop/connections/../authorizations/a",
    "/desktop/connections/%2e%2e/authorizations/a","/desktop/connections/c/authorizations/a#fragment",
    "/desktop/automations?run_now=true","/session/s/connections/c?force=true","/session/s/connections/c#fragment",
    "/session/s/connections/../c","/desktop/connections/c\\x/authorizations/a",
  ];
  for(const path of invalid){await assert.rejects(client.get(path),path);await assert.rejects(client.post(path,{}),path);}
  assert.equal(calls.length,0);
});

test("fleet snapshots allow authenticated reads without exposing board lifecycle controls", async () => {
  const calls = [];
  const client = new ReadClient("http://127.0.0.1:8420", "fixture", async (url, options) => {
    calls.push([url, options]);
    return {ok:true,json:async()=>({fleets:[]})};
  });
  await client.get("/session/s/fleets?cursor=0&limit=20");
  assert.equal(calls[0][1].method,"GET");
  assert.equal(calls[0][1].headers.Authorization,"Bearer fixture");
  for (const path of ["/session/s/fleets", "/session/s/fleets/init", "/session/s/fleets/b/merge", "/session/s/fleets/b/send", "/session/s/fleets/b/teardown"]) {
    await assert.rejects(client.post(path,{}));
  }
  await assert.rejects(client.get("/session/s/fleets/../../state.json"));
  assert.equal(calls.length,1);
});

test("member commands retain exact generation and key after a lost reply", async () => {
  for (const [kind, suffix, body] of [
    ["fleet_message", "messages", {run_id:"original-run",text:"先核对边界"}],
    ["fleet_stop", "stop", {run_id:"original-run"}],
  ]) {
    const storage = memoryStorage(), box = new Outbox(storage,"fleet"), calls = [];
    const path = `/session/s/fleets/b/members/worker/${suffix}`;
    box.prepare("request", {kind, path, body, sid:"s"});
    const client = new ReadClient("http://localhost","",async (url,options) => {
      calls.push({url,...options});
      if (calls.length === 1) throw new TypeError("reply dropped");
      return {ok:true,json:async()=>({operation:{status:"unknown"},duplicate:true})};
    });
    await assert.rejects(box.send("request",client));
    const restored = new Outbox(storage,"fleet");
    assert.equal(calls.length,1);
    assert.equal(restored.items.request.state,"unknown");
    await restored.send("request",client);
    assert.equal(calls[0].url,calls[1].url);
    assert.equal(calls[0].body,calls[1].body);
    assert.equal(calls[0].headers["Idempotency-Key"],calls[1].headers["Idempotency-Key"]);
    assert.equal(restored.items.request.state,"accepted");
    assert.equal(restored.items.request.body.run_id,"original-run");
  }
});

test("member command paths never permit lifecycle changes or encoded traversal", async () => {
  let requests = 0;
  const client = new ReadClient("http://localhost","",async()=>{
    requests++; return {ok:true,json:async()=>({})};
  });
  for (const path of [
    "/session/s/fleets/b/members/w/spawn", "/session/s/fleets/b/members/w/teardown",
    "/session/s/fleets/b/members/w/merge", "/session/s/fleets/b/members/w/stop?force=true",
    "/session/s/fleets/b/members/w%2fx/stop", "/session/s/fleets/b/members/../stop",
    "/session/s/fleets/b/members/w%5cx/messages",
  ]) await assert.rejects(client.post(path,{}));
  await assert.rejects(client.get("/session/s/fleets/b/members/w/stop"));
  assert.equal(requests,0);
});

test("weave controls preserve execution identity and nonpersistent stop warning", async () => {
  for (const kind of ["weave_message", "weave_stop"]) {
    const storage=memoryStorage(), box=new Outbox(storage,"weave-controls"), calls=[];
    const path=`/session/s/weaves/b/items/b%3A0/${kind === "weave_message" ? "messages" : "stop"}`;
    const body={execution_id:"exact-child",...(kind === "weave_message" ? {text:"核对边界"} : {})};
    const key=box.prepare("operation",{kind,sid:"s",path,body}).key;
    const client=new ReadClient("http://localhost","",async (url,options)=>{
      calls.push({url,...options});
      if(calls.length===1)throw new TypeError("lost reply");
      return {ok:true,json:async()=>({operation:{operation_id:"operation-one",status:"stop_requested",persisted:false,warning:"仅本次进程可核对"}})};
    });
    await assert.rejects(box.send("operation",client));
    const restored=new Outbox(storage,"weave-controls");await restored.send("operation",client);
    assert.equal(restored.items.operation.key,key);
    assert.deepEqual(restored.items.operation.body,body);
    assert.equal(calls[0].body,calls[1].body);
    assert.equal(restored.items.operation.result.operation_id,"operation-one");
    assert.equal(restored.items.operation.result.operation_persisted,false);
    assert.equal(restored.items.operation.result.operation_warning,"仅本次进程可核对");
    await client.get("/session/s/weave-operations/operation-one");
    for(const unsafe of ["/session/s/weaves/b/items/b%2F0/stop","/session/s/weaves/b/items/i/spawn"])
      await assert.rejects(client.post(unsafe,body));
  }
});

test("fleet orchestration preview and durable submission use exact supported paths", async () => {
  const calls = [];
  const client = new ReadClient("http://localhost", "", async (url,options) => {
    calls.push({url,...options}); return {ok:true,json:async()=>({session_id:"s",run_id:"r",duplicate:true})};
  });
  await client.get("/session/s/fleet-commands/capabilities");
  await client.post("/session/s/fleet-commands/preview", {action:"init",mode:"new"});
  const storage=memoryStorage(), first=new Outbox(storage,"fleet-command");
  const action={kind:"fleet_command",sid:"s",path:"/session/s/fleet-commands",
    body:{action:"init",mode:"new",preview_token:"reviewed"},draftKey:"fleet-command:s:init",draftText:"original"};
  const item=first.prepare("command",action);
  await assert.rejects(first.send("command",{post:async()=>{throw new TypeError("response lost");}}));
  const restored=new Outbox(storage,"fleet-command");
  assert.equal(calls.length,2);
  await restored.send("command",client);
  assert.equal(calls[2].headers["Idempotency-Key"],item.key);
  assert.deepEqual(JSON.parse(calls[2].body),action.body);
  for(const path of ["/session/s/fleet-commands/merge","/session/s/fleet-commands/../preview",
    "/session/s/fleet-commands/capabilities"]) await assert.rejects(client.post(path,{}));
  await assert.rejects(client.get("/session/s/fleet-commands/preview"));
});

test("restoring accepted receipts removes only the identical submitted draft", () => {
  const drafts = new Map([["s1", "已提交原文"], ["s2", "用户后来写的新草稿"]]);
  const store = {getDraft: sid => drafts.get(sid) || "", deleteDraft: sid => drafts.delete(sid)};
  const item = {state: "accepted", kind: "prompt", sid: "s1", body: {text: "已提交原文"}};
  assert.equal(reconcileAcceptedDraft(store, {...item, state: "unknown"}), false);
  assert.equal(drafts.get("s1"), "已提交原文");
  assert.equal(reconcileAcceptedDraft(store, item), true);
  assert.equal(drafts.has("s1"), false);
  assert.equal(reconcileAcceptedDraft(store, {...item, sid: "s2", kind: "steer"}), false);
  assert.equal(drafts.get("s2"), "用户后来写的新草稿");
  assert.equal(reconcileAcceptedDraft(store, {...item, kind: "answer"}), false);
  assert.equal(reconcileAcceptedDraft(null, item), false);
});

test("only explicit loopback endpoints without credentials or extra URL parts", () => {
  for (const host of ["127.0.0.1", "localhost", "[::1]"]) assert.equal(endpoint(`http://${host}:8420/`), `http://${host}:8420`);
  for (const value of ["https://example.org", "http://127.0.0.1.evil.test", "http://user:secret@localhost", "http://localhost/?token=secret", "http://localhost/path", "file:///tmp/a", "http://localhost/#a"]) assert.throws(() => endpoint(value));
});

test("reads use fixed GET, no credentials storage, no redirects and exact destination", async () => {
  const calls = [];
  const client = new ReadClient("http://localhost:8420", "test-token", async (url, options) => {
    calls.push({url, options}); return {ok: true, json: async () => ({sessions: []})};
  });
  assert.deepEqual(await client.get("/session"), {sessions: []});
  const {url, options} = calls[0];
  assert.equal(url, "http://localhost:8420/session");
  assert.equal(options.method, "GET");
  assert.equal(options.redirect, "error");
  assert.equal(options.credentials, "omit");
  assert.equal(options.headers.Authorization, "Bearer test-token");
  for (const path of ["//evil.test", "/session/a/prompt_async", "/session/a/abort", "/session/a/permissions"]) await assert.rejects(client.get(path));
  assert.equal(calls.length, 1);
});

test("HTTP failures are explicit and do not echo a response body or token", async () => {
  for (const status of [401, 403, 404, 500]) {
    const client = new ReadClient("http://localhost", "private-token", async () => ({ok: false, status, json: async () => ({detail: "private-token"})}));
    await assert.rejects(client.get("/session"), e => e.status === status && !e.message.includes("private-token"));
  }
});

test("catalog and collaboration reads stay inside the explicit read-only protocol", async () => {
  const calls = [];
  const client = new ReadClient("http://localhost", "", async (url, options) => {
    calls.push({url, options}); return {ok: true, json: async () => ({})};
  });
  for (const path of ["/catalog/sessions?limit=100", "/catalog/sessions/source-1/history?cursor=0",
    "/session/session-1/weaves?limit=64"]) await client.get(path);
  for (const path of ["/catalog/sessions/source-1/delete", "/catalog/sessions/../../history",
    "/catalog/sessions/a%2fb/history", "/session/session-1/weaves/retry"]) {
    await assert.rejects(client.get(path));
  }
  await assert.rejects(client.post("/catalog/sessions", {}));
  await assert.rejects(client.post("/session/session-1/weaves/retry", {}));
  assert.equal(calls.length, 3);
  assert(calls.every(call => call.options.method === "GET"));
});

test("native fetch is never called with the client instance as its receiver", async () => {
  const previous = globalThis.fetch;
  globalThis.fetch = async function () {
    assert.equal(this instanceof ReadClient, false);
    return {ok: true, json: async () => ({sessions: []})};
  };
  try { assert.deepEqual(await new ReadClient("http://localhost").get("/session"), {sessions: []}); }
  finally { globalThis.fetch = previous; }
});

test("disconnect cancellation reaches the transport and is not reported as timeout", async () => {
  const controller = new AbortController();
  const client = new ReadClient("http://localhost", "", (_url, options) => new Promise((_resolve, reject) => {
    options.signal.addEventListener("abort", () => reject(new DOMException("cancelled", "AbortError")));
  }));
  const request = client.get("/session", controller.signal);
  controller.abort();
  await assert.rejects(request, {name: "AbortError"});
});

test("idle is not success; approvals and durable questions take priority over finished", () => {
  const base = {status: "idle", detail: "finished", turns: 1};
  assert.equal(stateOf(base).label, "已完成");
  assert.equal(stateOf({...base, pending_approvals: [{}]}).label, "等待审批");
  assert.equal(stateOf({...base, questionItems: [{state: "pending"}]}).label, "等待回答");
  assert.equal(stateOf({...base, questionItems: [{state: "queued"}]}).label, "答案待接纳");
  assert.equal(stateOf({...base, questionError: "unavailable"}).label, "待答状态未知");
  assert.equal(stateOf({...base, detail: "interrupted_by_restart"}).label, "重启中断");
  assert.equal(stateOf({...base, detail: "budget_reached"}).label, "预算已用尽");
  assert.equal(stateOf({...base, detail: "recovered"}).label, "已恢复 · 待继续");
  assert.equal(stateOf({...base, detail: "recovered", last_result: {stopped: "done"}}).label, "已完成");
  assert.equal(stateOf({...base, detail: "recovered", last_result: {stopped: "turn_cap"}}).label, "达到轮次上限");
  assert.equal(stateOf({...base, last_result: {stopped: "turn_cap"}}).attention, true);
  assert.notEqual(stateOf({status: "idle", turns: 0}).label, "已完成");
  assert.equal(stateOf({...base, status: "error"}).label, "执行失败");
});

test("project, search and attention filters are combined without mutating source order", () => {
  const sessions = [{session_id: "done", workspace: "/a", model: "small", detail: "finished"},
    {session_id: "question", workspace: "/b", model: "small", questionItems: [{state: "pending"}]},
    {session_id: "failed", workspace: "/a", model: "large", status: "error"}];
  assert.deepEqual(visibleSessions(sessions, "/a", "attention", "LARGE").map(s => s.session_id), ["failed"]);
  assert.equal(visibleSessions(sessions, "", "all", "")[0].session_id, "failed");
  assert.equal(sessions[0].session_id, "done");
  assert.equal(visibleSessions([{...sessions[0], busy:true, pending_approvals:[{}]}], "", "running", "").length, 0);
});

test("idle background results needing a new turn are surfaced without automatic waking", () => {
  const session = {status: "idle", detail: "finished", pending_notifications: [{wake: true, text: "后台完成"}]};
  assert.equal(stateOf(session).label, "后台结果待处理");
  assert.equal(stateOf({...session, busy: true, status: "running"}).label, "执行中");
  assert.equal(stateOf({...session, pending_notifications: [{wake: false}]}).label, "已完成");
});

test("event windows retain loss information, bound memory and reject cursor regression", () => {
  const initial = eventWindow({next_seq: 503, first_seq: 20});
  assert.equal(initial.cursor, 303);
  assert.equal(initial.gap, true);
  assert.equal(initial.clipped, true);
  const next = mergeEvents(initial, {first_seq: 401, next: 601, events: Array.from({length: 200}, (_, i) => ({seq: i + 401}))});
  assert.equal(next.events.length, 200);
  assert.equal(next.gap, true);
  const appended = mergeEvents(next, {first_seq: 401, next: 602, events: [{seq: 601}]});
  assert.equal(appended.events[0].seq, 402);
  assert.equal(appended.cursor, 602);
  assert.throws(() => mergeEvents(appended, {events: [], next: 1}));
  assert.throws(() => mergeEvents(appended, {events: [{seq: 604}, {seq: 603}], next: 605}));
  assert.equal(mergeEvents(eventWindow({}), {events: [{seq: 3}], first_seq: 1, next: 4}).gap, true);
});

test("external content is rendered as text, including markup and quotes", () => {
  assert.equal(escapeHTML('<img src=x onerror="alert(1)"> & \'quoted\''), "&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#39;quoted&#39;");
});

const {Outbox, conversation, renderText} = await import("../web/client.mjs");
function memoryStorage() {
  const values = new Map();
  return {getItem: key => values.get(key) || null, setItem: (key, value) => values.set(key,value), values};
}
test("contest submissions and human selections retain exact request identities after a lost reply", async () => {
  for (const action of [
    {kind:"contest",path:"/session/s/contests",body:{spec:"reviewer",task:"compare",models:["a","b"],preview_token:"reviewed"}},
    {kind:"selection",path:"/session/s/contests/c/selection",body:{candidate_id:"c:1",reason:"明确选择",expected_revision:0}},
  ]) {
    const storage = memoryStorage(), box = new Outbox(storage,"contest"), calls = [];
    box.prepare("request", {...action,sid:"s"});
    const client = new ReadClient("http://localhost","",async (_url,options) => {
      calls.push(options);
      if(calls.length === 1) throw new TypeError("reply dropped");
      return {ok:true,json:async()=>({duplicate:true})};
    });
    await assert.rejects(box.send("request",client));
    const restored = new Outbox(storage,"contest");
    assert.equal(calls.length,1);
    assert.equal(restored.items.request.state,"unknown");
    await restored.send("request",client);
    assert.equal(calls[0].body,calls[1].body);
    assert.equal(calls[0].headers["Idempotency-Key"],calls[1].headers["Idempotency-Key"]);
    assert.equal(restored.items.request.state,"accepted");
  }
});
test("contest paths permit explicit selection but never file application or arbitrary mutations", async () => {
  const calls=[];
  const client=new ReadClient("http://localhost","",async(url,options)=>{
    calls.push({url,options});return {ok:true,json:async()=>({})};
  });
  for(const path of ["/session/s/contests?cursor=0","/session/s/contests/capabilities","/session/s/artifacts/a?contest_id=c&candidate_id=c%3A1"]) await client.get(path);
  for(const path of ["/session/s/contests/preview","/session/s/contests","/session/s/contests/c/selection"]) await client.post(path,{});
  for(const path of ["/session/s/contests/c/apply","/session/s/contests/c/delete","/session/s/contests/c%2fd/selection","/session/s/contests/../selection"]) await assert.rejects(client.post(path,{}));
  await assert.rejects(client.get("/session/s/contests/c/selection"));
  assert.equal(calls.length,6);
});
test("a lost batch response retries the same reviewed command and key after reload", async () => {
  const storage = memoryStorage(), box = new Outbox(storage, "weave");
  const body = {source_batch_id: "old", item_ids: ["old:1"], instruction: "补齐验证", preview_token: "reviewed"};
  box.prepare("w", {kind: "weave", sid: "s", path: "/session/s/weaves", body});
  const calls = [];
  const client = new ReadClient("http://localhost", "", async (_url, options) => {
    calls.push(options);
    if (calls.length === 1) throw new TypeError("lost response");
    return {ok: true, json: async () => ({session_id: "s", run_id: "r"})};
  });
  await assert.rejects(box.send("w", client));
  const restored = new Outbox(storage, "weave");
  assert.equal(calls.length, 1);
  await restored.send("w", client);
  assert.equal(calls[0].body, calls[1].body);
  assert.equal(calls[0].headers["Idempotency-Key"], calls[1].headers["Idempotency-Key"]);
  assert.equal(restored.items.w.state, "accepted");
});

test("artifact inspection and batch preview have narrow supported endpoints", async () => {
  const calls = [];
  const client = new ReadClient("http://localhost", "", async (url, options) => {
    calls.push({url, options}); return {ok: true, json: async () => ({})};
  });
  await client.get("/session/s/weaves/capabilities");
  await client.post("/session/s/weaves/preview", {spec: "reader"});
  await client.get("/session/s/artifacts/a?run_id=r");
  for (const path of ["/session/s/artifacts/a/location", "/session/s/artifacts/../../secret", "/session/s/weaves/execute"]) {
    await assert.rejects(client.get(path));
  }
  await assert.rejects(client.post("/session/s/artifacts/a", {}));
  assert.equal(calls.length, 3);
});
test("lost response survives reload with identical body/key, no automatic resend", async () => {
  const storage = memoryStorage(), box = new Outbox(storage,"service");
  const action = {kind:"prompt",sid:"a",path:"/session/a/prompt_async",body:{text:"只执行一次"}};
  const original = box.prepare("prompt",action);
  assert.equal(box.prepare("prompt",{...action,body:{text:"different"}}).key,original.key);
  const calls=[];let executions=0;const seen=new Set();
  const client=new ReadClient("http://localhost","secret",async(url,options)=>{
    calls.push(options);const key=options.headers["Idempotency-Key"];
    if(!seen.has(key)){seen.add(key);executions++;throw new TypeError("response lost");}
    return {ok:true,json:async()=>({accepted:true,session_id:"a"})};
  });
  await assert.rejects(box.send("prompt",client));
  assert.equal(box.items.prompt.state,"unknown");
  const restored=new Outbox(storage,"service");assert.equal(calls.length,1);
  await restored.send("prompt",client);
  assert.equal(executions,1);assert.equal(calls[0].body,calls[1].body);
  assert.equal(calls[0].headers["Idempotency-Key"],calls[1].headers["Idempotency-Key"]);
  assert.ok(!storage.getItem(box.key).includes("secret"));
});
test("double click is one in-flight mutation and cannot replace the pending body", async () => {
  const box=new Outbox(memoryStorage(),"s");box.prepare("create",{kind:"create",path:"/session",body:{workspace:"/x"}});
  let release,calls=0;
  const client={post:async()=>{calls++;return await new Promise(resolve=>{release=resolve;});}};
  const first=box.send("create",client);assert.equal(await box.send("create",client),null);
  assert.throws(()=>box.forget("create"));release({session_id:"a"});await first;assert.equal(calls,1);
  assert.equal(await box.send("create",client),null);assert.equal(calls,1);
});
test("answers use the same durable key in JSON after a dropped response",async()=>{
  const box=new Outbox(memoryStorage(),"s");box.prepare("q",{kind:"answer",path:"/session/a/questions/q/answers",body:{answers:[{item_id:"i",custom:"text"}]}});
  const bodies=[];const client={post:async(path,body)=>{bodies.push(body);if(bodies.length===1)throw new Error("lost");return {question:{state:"queued"}};}};
  await assert.rejects(box.send("q",client));await box.send("q",client);
  assert.deepEqual(bodies[0],bodies[1]);assert.ok(bodies[0].idempotency_key);
});
test("uncertain steer cannot be replayed; a durable recorded prompt cannot be treated as fresh",async()=>{
  const box=new Outbox(memoryStorage(),"s");box.prepare("s",{kind:"steer",path:"/session/a/steer",body:{text:"hi",turn:1}});
  let calls=0;const client={post:async()=>{calls++;throw new Error("lost");}};
  await assert.rejects(box.send("s",client));await assert.rejects(box.send("s",client));assert.equal(calls,1);
  box.prepare("p",{kind:"prompt",path:"/session/a/prompt_async",body:{text:"hi"}});
  await assert.rejects(box.send("p",{post:async()=>{throw Object.assign(new Error("recorded"),{recorded:true,status:409});}}));
  assert.equal(box.items.p.state,"recorded");
});
test("storage failure prevents sending, including failure at the sending transition",async()=>{
  const storage=memoryStorage(),box=new Outbox(storage,"s");box.prepare("p",{kind:"prompt",path:"/session/a/prompt_async",body:{text:"hi"}});
  storage.setItem=()=>{throw new Error("quota");};let calls=0;
  await assert.rejects(box.send("p",{post:async()=>{calls++;}}));assert.equal(calls,0);
  assert.throws(()=>box.prepare("new",{kind:"create"}));assert.equal(box.items.new,undefined);
});
test("mutations stay on whitelisted endpoints and carry explicit turn/instance",async()=>{
  const calls=[],client=new ReadClient("http://localhost","",async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>({aborted:true})};});
  await client.post("/session/a/abort",{turn:7},{instance:"epoch-1"});
  assert.equal(calls[0].options.headers["X-Xiaoyu-Instance"],"epoch-1");assert.deepEqual(JSON.parse(calls[0].options.body),{turn:7});
  for(const path of ["/agent","/session/a/budget","//evil","/session/a%2fb/abort","/session/../abort"])await assert.rejects(client.post(path,{}));
  assert.equal(calls.length,1);
});
test("streamed conversation separates turns, tool steps and accepted steer; text cannot inject HTML",()=>{
  const blocks=conversation([{seq:1,kind:"run.started",prompt:"do"},{seq:2,kind:"text.delta",text:"one"},{seq:3,kind:"text.delta",text:"two"},{seq:4,kind:"tool.pending"},{seq:5,kind:"steer.accepted",text:"change"},{seq:6,kind:"text.delta",text:"done",text_truncated:true}]);
  assert.deepEqual(blocks.map(b=>b.text),["do","onetwo","change","done"]);assert.equal(blocks.at(-1).clipped,true);
  const rendered=renderText('## Hello\n\n**bold** <img src=x onerror=alert(1)>\n\n```js\n<script>bad</script>\n```');
  assert.ok(rendered.includes("<strong>bold</strong>"));assert.ok(!rendered.includes("<img"));assert.ok(!rendered.includes("<script>"));
});

const {LocalState, historyConversation} = await import("../web/client.mjs");
test("native state persists draft and receipt before a model request, without port identity or credentials", async () => {
  let disk = {revision: 0, drafts: {}, outbox: {}}, submissions = 0;
  const client = {
    get: async () => structuredClone(disk),
    post: async (path, body) => {
      if (path === "/desktop/state") {
        assert.equal(body.revision, disk.revision);
        disk = {...structuredClone(body), revision: disk.revision + 1};
        return {revision: disk.revision};
      }
      submissions++;
      assert.equal(disk.outbox.p.state, "sending");
      assert.equal(disk.drafts.s, "未发送的草稿");
      return {session_id: "s"};
    },
  };
  const local = await LocalState.load(client);
  local.setDraft("s", "未发送的草稿");
  const box = new Outbox(local.storage, "native");
  box.prepare("p", {kind: "prompt", sid: "s", path: "/session/s/prompt_async", body: {text: "work"}});
  await box.send("p", client);
  assert.equal(submissions, 1);
  const reopened = await LocalState.load(client);
  assert.equal(reopened.getDraft("s"), "未发送的草稿");
  const recovered = new Outbox(reopened.storage, "different-port");
  assert.equal(recovered.items.p.key, box.items.p.key);
  assert.equal(submissions, 1);
  local.deleteDraft("s"); await local.flush();
  assert.equal(disk.drafts.s, undefined);
});

test("native state flush includes edits made while a save is in flight", async () => {
  let release; const requests = [];
  const local = new LocalState({post: async (_path, body) => {
    requests.push(body);
    if (requests.length === 1) await new Promise(resolve => { release = resolve; });
    return {revision: body.revision + 1};
  }}, {revision: 0, drafts: {}, outbox: {}});
  local.setDraft("s", "first"); const saved = local.flush();
  local.setDraft("s", "second"); const concurrent = local.flush(); release();
  await Promise.all([saved, concurrent]);
  assert.deepEqual(requests.map(r => [r.revision, r.drafts.s]), [[0, "first"], [1, "second"]]);
});

test("failed native persistence prevents task submission and preserves original key", async () => {
  let submissions = 0;
  const local = new LocalState({post: async () => { throw Object.assign(new Error("state conflict"), {status: 409}); }},
    {revision: 0, drafts: {}, outbox: {}});
  const box = new Outbox(local.storage, "native");
  const key = box.prepare("p", {kind: "prompt", path: "/session/s/prompt_async", body: {text: "work"}}).key;
  await assert.rejects(box.send("p", {post: async () => { submissions++; }}));
  clearTimeout(local.timer);
  assert.equal(submissions, 0);
  assert.equal(box.items.p.key, key);
});

test("history helpers omit system/tool messages and retain non-text disclosure", () => {
  assert.deepEqual(historyConversation([
    {id: "1", role: "system", content: "private"},
    {id: "2", role: "user", content: "hello"},
    {id: "3", role: "assistant", content: [{type: "text", text: "answer"}, {type: "image"}]},
  ]), [{id: "2", kind: "user", text: "hello"}, {id: "3", kind: "assistant", text: "answer\n[非文本内容，请在原始成果中查看]"}]);
});

test("fleet changes are read only and remain inside the selected mission route", async () => {
  const calls = [];
  const client = new ReadClient("http://127.0.0.1:8420", "", async (url, options) => {
    calls.push([url, options.method]);
    return {ok:true,json:async()=>({can_merge:false,blockers:[]})};
  });
  await client.get("/session/s/fleets/b/missions/M1/changes");
  assert.deepEqual(calls, [["http://127.0.0.1:8420/session/s/fleets/b/missions/M1/changes", "GET"]]);
  await assert.rejects(client.post("/session/s/fleets/b/missions/M1/changes",{}));
  await assert.rejects(client.get("/session/s/fleets/b/missions/../changes"));
  await assert.rejects(client.get("/session/s/fleets/b/missions/M1/changes/file"));
});

test('project workspace metadata paths allow only scoped reads and explicit state writes', async () => {
  const calls=[];
  const client=new ReadClient('http://localhost','',async(url,options)=>{calls.push({url,...options});return {ok:true,json:async()=>({})};});
  await client.get('/desktop/workspace');
  await client.post('/desktop/workspace',{revision:0,group:'projects',key:'p',value:{context:'说明'}});
  await client.get('/desktop/projects/p/artifacts');
  for(const path of ['/desktop/projects/p/artifacts','/desktop/projects/p/delete','/desktop/workspace/extra']) await assert.rejects(client.post(path,{}));
  await assert.rejects(client.get('/desktop/projects/%2fetc/artifacts'));
  assert.equal(calls.length,3);
});

test('lost execution ownership blocks new work while preserving safe stop and denial', () => {
  const session={writable:false};
  for (const kind of ['prompt','steer','weave','contest','fleet_command','fleet_message','weave_message','answer']) assert.equal(sessionActionAllowed(session,{kind}),false);
  assert.equal(sessionActionAllowed(session,{kind:'approval',body:{decision:'allow'}}),false);
  for(const kind of ['abort','fleet_stop','weave_stop']) assert.equal(sessionActionAllowed(session,{kind}),true);
  assert.equal(sessionActionAllowed(session,{kind:'approval',body:{decision:'deny'}}),true);
  assert.equal(sessionActionAllowed({writable:true},{kind:'prompt'}),true);
  assert.equal(sessionActionAllowed({}, {kind:'prompt'}),true);
});

test("desktop state rejection explains storage incompatibility without echoing server content",async()=>{
  const client=new ReadClient("http://127.0.0.1:8420","",async()=>({ok:false,status:422,json:async()=>({detail:"private fixture"})}));
  await assert.rejects(client.post("/desktop/state",{}),error=>error.status===422 && error.message.includes("保存格式不兼容") && !error.message.includes("工作目录") && !error.message.includes("private fixture"));
});

test('skill settings use the explicit desktop write route', async()=>{
  const calls=[];
  const client=new ReadClient('http://127.0.0.1:8420','',async(url,options)=>{
    calls.push({url,options});return new Response('{}',{status:200});
  });
  await client.post('/desktop/skills/settings',{revision:0,ids:['a'],enabled:false,session_id:''},{instance:'test'});
  assert.equal(calls.length,1);
  assert.equal(calls[0].options.headers['X-Xiaoyu-Instance'],'test');
  await assert.rejects(client.get('/desktop/skills/settings'));
  await assert.rejects(client.post('/desktop/skills/arbitrary',{}));
});
