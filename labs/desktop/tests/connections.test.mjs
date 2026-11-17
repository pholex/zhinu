import test from "node:test";
import assert from "node:assert/strict";
import {createConnectionController,connectionDefinition,connectionControls} from "../web/connections.mjs";
const tick=()=>new Promise(r=>setTimeout(r,0));
const definition=()=>({name:"工具服务",transport:"http",url:"https://example.test/mcp",timeout:30,oauth:{client_id:"public-app",authorization_endpoint:"https://example.test/auth",token_endpoint:"https://example.test/token"}});
function harness(){
 const catalog={revision:0,connections:[{id:"c",revision:0,definition:definition()}]},session={instance_id:"instance-one",can_manage:true,reason:"",connections:[{id:"c",configuration_revision:0,mounted:false,state:"unmounted",tools:[],declaration_fingerprint:"",oauth_status:"unauthorized"}]},saved=new Map(),calls=[],events=[],bridge=[],jobs=new Map();let failRead=false,lose=false,flushFail=false,bridgeFail=false;
 const ctx={sid:"s",sessions:[{session_id:"s",title:"当前工作",workspace:"/work"}],writable:true,conn:{health:{instance_id:"instance-one"},controller:new AbortController(),localState:{getDraft:k=>saved.get(k),setDraft:(k,v)=>saved.set(k,v),deleteDraft:k=>saved.delete(k),flush:async()=>{events.push("flush");if(flushFail)throw new Error("disk");}},client:{get:async path=>{if(failRead)throw new Error("offline");if(path==="/desktop/connections")return structuredClone(catalog);if(path.startsWith("/session/"))return structuredClone(session);return structuredClone(jobs.get(path.split("/").at(-1)));},post:async(path,body,options)=>{events.push("post");calls.push({path,body:structuredClone(body),key:options?.key,instance:options?.instance});let result={};
 if(path==="/desktop/connections"){const prior=catalog.connections.find(r=>r.id===body.connection_id);if(prior && JSON.stringify(prior.definition)===JSON.stringify(body.definition)){}else{if(body.revision!==catalog.revision)throw Object.assign(new Error("conflict"),{status:409});const value={id:body.connection_id,revision:++catalog.revision,definition:structuredClone(body.definition)};if(prior)Object.assign(prior,value);else catalog.connections.push(value);}}
 else if(path.startsWith("/session/")){const r=session.connections[0];if(body.action==="mount"){r.mounted=true;r.state="ready";}if(body.action==="stop")r.state="stopped";if(body.action==="approve_changes")r.tools.forEach(t=>t.quarantined=false);}
 else if(path.endsWith("/authorizations")){result={authorization_id:"auth-one",status:"pending",expires_at:2000000000,access_token:"MUST_NOT_PERSIST",url:"https://example.test/auth?code=SECRET"};jobs.set("auth-one",result);session.connections[0].oauth_status="pending";}
 else if(path.endsWith("/cancel"))jobs.get("auth-one").status="cancelled";
 else if(path.endsWith("/revoke"))session.connections[0].oauth_status="unauthorized";
 else if(path.endsWith("/delete"))catalog.connections=[];
 if(lose){lose=false;throw new Error("lost reply");}return result;}}}};
 const c=createConnectionController({context:()=>ctx,render:()=>{},selectSession:s=>{ctx.sid=s;},openAuthorization:async ids=>{bridge.push(ids);if(bridgeFail)throw new Error("browser blocked");return {opened:true};}});
 const click=(action,extra={})=>c.click({target:{closest:()=>({dataset:{connectionAction:action,connectionId:"c",connectionTarget:"s",...extra}})}});
 const manage=(action,extra={})=>click("",{connectionManage:action,connectionRevision:"0",connectionInstance:"instance-one",...extra});
 const auth=action=>click("",{connectionAuth:action});
 const input=(field,value)=>c.input({target:{dataset:{connectionField:field},value}});
 const submit=()=>c.onSubmit({target:{dataset:{connectionForm:""}},preventDefault:()=>{}});
 return {c,ctx,catalog,session,saved,calls,events,bridge,jobs,click,manage,auth,input,submit,lose:()=>{lose=true;},failRead:()=>{failRead=true;},failFlush:()=>{flushFail=true;},failBridge:()=>{bridgeFail=true;},async form(){await c.load();c.open();input("name","新连接");input("url","https://other.test/mcp");}};
}
test("configuration uses stable ID and CAS persistence without mounting or accepting secrets",async()=>{
 const h=harness();await h.form();h.submit();await tick();assert.equal(h.events[0],"flush");assert.equal(h.calls.length,1);assert.equal(h.calls[0].path,"/desktop/connections");assert.equal(h.session.connections[0].mounted,false);assert.match(h.c.render(),/选择对话后即可连接使用/);assert.throws(()=>connectionDefinition({name:"n",transport:"http",url:"https://u:p@example.test",timeout:"30"}),/凭据/);
});
test("lost configuration reply retries same ID and body after reset only on explicit request",async()=>{
 const h=harness();await h.form();h.lose();h.submit();await tick();const body=h.calls[0].body;h.c.reset();await h.c.load();assert.equal(h.calls.length,1);h.click("retry");await tick();assert.deepEqual(h.calls[1].body,body);assert.equal(h.catalog.connections.length,2);
});
test("local storage failure blocks configuration network writes and preserves text",async()=>{
 const h=harness();await h.form();h.failFlush();h.submit();await tick();assert.equal(h.calls.length,0);assert.match(h.saved.get("connection-definition-v2"),/新连接/);assert.match(h.c.render(),/disk/);
});
test("mount requires the exact session, service instance and configuration revision without durable outbox",async()=>{
 const h=harness();await h.c.load();h.manage("mount",{connectionRevision:"old"});await tick();assert.equal(h.calls.length,0);h.manage("mount",{connectionInstance:"old-instance"});await tick();assert.equal(h.calls.length,0);h.manage("mount");await tick();assert.equal(h.calls[0].path,"/session/s/connections/c");assert.deepEqual(h.calls[0].body,{action:"mount",configuration_revision:0});assert.ok(h.calls[0].key);assert.equal(h.calls[0].instance,"instance-one");assert.equal(h.saved.size,0);
});
test("lost management receipt is read and manually reconciled before any new operation",async()=>{
 const h=harness();await h.c.load();h.lose();h.manage("mount");await tick();assert.equal(h.session.connections[0].state,"ready");h.manage("stop");await tick();assert.equal(h.calls.length,1);assert.match(h.c.render(),/已核对当前状态/);h.click("reconcile");h.manage("stop");await tick();assert.equal(h.calls.length,2);assert.notEqual(h.calls[1].key,h.calls[0].key);
});
test("declaration approval binds the displayed fingerprint and unknown states cannot approve",async()=>{
 const h=harness(),r=h.session.connections[0];Object.assign(r,{mounted:true,state:"quarantined",tools:[{name:"inspect",description:"<script>",inputSchema:{type:"object"},quarantined:true}],declaration_fingerprint:"new"});await h.c.load();h.manage("approve_changes",{connectionFingerprint:"old"});await tick();assert.equal(h.calls.length,0);h.manage("approve_changes",{connectionFingerprint:"new"});await tick();assert.equal(h.calls[0].body.expected_fingerprint,"new");assert.doesNotMatch(h.c.render(),/<script>/);assert.equal(connectionControls({...r,state:"unexpected"},true).approve_changes,false);
});
test("busy sessions block tool changes while authorization status and cancellation remain available",async()=>{
 const h=harness();await h.c.load();h.auth("begin");await tick();h.session.can_manage=false;h.session.reason="成员仍在执行";await h.c.loadSession();h.manage("mount");assert.equal(h.calls.length,1);h.auth("refresh");await tick();h.auth("cancel");await tick();assert.equal(h.jobs.get("auth-one").status,"cancelled");assert.match(h.c.render(),/成员仍在执行/);
});
test("OAuth uses only IDs through system browser bridge and keeps all authorization material out of storage",async()=>{
 const h=harness();await h.c.load();h.auth("begin");await tick();assert.equal(h.saved.size,0);assert.doesNotMatch(h.c.render(),/MUST_NOT_PERSIST|code=SECRET|access_token/);h.auth("open");await tick();assert.deepEqual(h.bridge,[{connection_id:"c",authorization_id:"auth-one"}]);assert.equal(h.saved.size,0);h.jobs.get("auth-one").status="authorized";h.session.connections[0].oauth_status="authorized";h.auth("refresh");await tick();assert.equal(h.session.connections[0].mounted,false);h.c.reset();await h.c.load();assert.doesNotMatch(h.c.render(),/在系统浏览器继续授权/);assert.equal(h.calls.length,1);
});
test("browser failure remains visible without automatic retry and revoke requires idle unmounted state",async()=>{
 const h=harness();await h.c.load();h.auth("begin");await tick();h.failBridge();h.auth("open");await tick();assert.match(h.c.render(),/browser blocked/);assert.equal(h.bridge.length,1);h.session.connections[0].mounted=true;h.session.connections[0].oauth_status="authorized";await h.c.loadSession();h.auth("revoke");await tick();assert.equal(h.calls.length,1);h.session.connections[0].mounted=false;await h.c.loadSession();h.auth("revoke");await tick();assert.equal(h.calls.at(-1).path,"/desktop/connections/c/revoke");
});
test("failed reads and corrupt saved updates preserve data and cannot submit configuration",async()=>{
 const h=harness();await h.c.load();h.failRead();await h.c.load();assert.match(h.c.render(),/工具服务/);assert.match(h.c.render(),/可能过期/);h.manage("mount");assert.equal(h.calls.length,0);
 const bad=harness();bad.saved.set("connection-configuration-pending-v2",JSON.stringify({kind:"save",body:{revision:0,connection_id:"c",definition:{name:"n",headers:{Authorization:"secret"}}}}));await bad.c.load();bad.click("retry");assert.equal(bad.calls.length,0);assert.match(bad.c.render(),/原数据保留/);
});
test("pasting callback or credential URLs stays in memory and never enters LocalState",async()=>{
 const h=harness();await h.form();const before=h.saved.get("connection-definition-v2");h.input("url","https://example.test/callback?code=PRIVATE");assert.equal(h.saved.get("connection-definition-v2"),before);h.submit();await tick();assert.equal(h.calls.length,0);assert.doesNotMatch([...h.saved.values()].join(),/PRIVATE/);h.input("url","https://example.test/clean");h.submit();await tick();assert.equal(h.calls.length,1);
});
test("service unknown receipt is not success and declaration limits cannot be approved",async()=>{
 const h=harness();await h.c.load();h.ctx.conn.client.post=async()=>({status:"unknown",warning:"回执未确认",duplicate:true});h.manage("mount");await tick();assert.match(h.c.render(),/管理结果未知/);assert.match(h.c.render(),/已核对当前状态/);assert.doesNotMatch(h.c.render(),/请求已返回/);
 h.session.connections[0].declaration_warning="工具声明超过展示限制";h.session.connections[0].declaration_fingerprint="";await h.c.loadSession();assert.match(h.c.render(),/工具声明超过展示限制/);assert.doesNotMatch(h.c.render(),/明确批准当前指纹/);
});
test("configuration explains saved direct values and environment references",async()=>{
 const h=harness();await h.form();h.input("transport","stdio");assert.match(h.c.render(),/直接填写的值会保存在本机/);assert.match(h.c.render(),/连接时只读取指定变量/);assert.doesNotMatch(h.c.render(),/不提供静态请求头/);
});

test("changing target sessions rejects old management and authorization DOM actions",async()=>{
 const h=harness();await h.c.load();h.ctx.sid="other";await h.c.loadSession();h.manage("mount");h.auth("begin");await tick();assert.equal(h.calls.length,0);assert.equal(h.bridge.length,0);assert.match(h.c.render(),/目标会话已变化/);
 h.manage("mount",{connectionTarget:"other"});await tick();assert.equal(h.calls[0].path,"/session/other/connections/c");
});
test("opening connection settings before LocalState arrives cannot skip restored configuration and pending CAS update",async()=>{
 const h=harness();await h.form();h.lose();h.submit();await tick();const original=h.calls[0].body,savedDraft=h.saved.get("connection-definition-v2"),store=h.ctx.conn.localState;
 h.c.reset();delete h.ctx.conn.localState;h.c.render();await h.c.load();assert.equal(h.calls.length,1);
 h.ctx.conn.localState=store;await h.c.load();h.c.open();assert.match(h.c.render(),/发现待核对的连接配置更新/);assert.match(h.c.render(),/新连接/);assert.equal(h.saved.get("connection-definition-v2"),savedDraft);assert.equal(h.calls.length,1);
 h.click("retry");await tick();assert.deepEqual(h.calls[1].body,original);assert.equal(h.catalog.connections.length,2);
});

test('connector list uses collapsed rows and searches configured services without inventing readiness',async()=>{
 const h=harness();await h.c.load();
 assert.match(h.c.render({embedded:true}),/<details class="plugin-entry connection-card"/);
 assert.doesNotMatch(h.c.render({embedded:true}),/<h2>连接器<\/h2>/);
 assert.match(h.c.render({query:'no such service'}),/没有匹配的连接器/);
 assert.doesNotMatch(h.c.render({query:'no such service'}),/data-key="connector-c"/);
 h.ctx.sid='';assert.match(h.c.render(),/plugin-entry-state">已配置/);
 assert.match(h.c.render(),/远程 MCP 服务/);
});


test('connector editor separates creation from session selection and returns to list after saving',async()=>{
 const h=harness();await h.form();
 const html=h.c.render();assert.match(html,/connection-type-switch/);assert.doesNotMatch(html,/id="connection-session"|class="connection-card"|还没有添加连接器/);
 assert.match(html,/<details class="connection-advanced"/);assert.doesNotMatch(html,/<details class="connection-advanced"[^>]* open/);
 h.click('close');assert.match(h.c.render(),/id="connection-session"/);h.c.open();assert.match(h.c.render(),/value="新连接"/);
 h.submit();await tick();assert.doesNotMatch(h.c.render(),/data-connection-form/);assert.equal(h.saved.has('connection-definition-v2'),false);
 h.c.open();assert.match(h.c.render(),/id="connection-name"[^>]*value=""/);h.click('discard');assert.doesNotMatch(h.c.render(),/data-connection-form/);
});

test('transport switches retain typed values and argument rows preserve exact order and spaces',async()=>{
 const h=harness();await h.form();h.input('client_id','public');h.input('authorization_endpoint','https://other.test/auth');h.input('token_endpoint','https://other.test/token');
 h.click('transport',{connectionTransport:'stdio'});h.input('command','npx');h.input('args','-y\npackage\na path with spaces');
 h.click('remove-arg',{connectionIndex:'1'});h.click('add-arg');
 h.c.input({target:{dataset:{connectionArg:'2'},value:'--read-only'}});
 h.click('transport',{connectionTransport:'http'});assert.match(h.c.render(),/value="https:\/\/other.test\/mcp"/);
 h.click('transport',{connectionTransport:'stdio'});assert.match(h.c.render(),/a path with spaces/);
 h.submit();await tick();assert.deepEqual(h.calls[0].body.definition,{name:'新连接',transport:'stdio',command:'npx',args:['-y','a path with spaces','--read-only'],timeout:30});
});


test('HTTP configuration round-trips direct headers and environment references',async()=>{
 const h=harness();await h.form();h.input('headers',JSON.stringify([['X-Client','desktop']]));h.input('env_headers',JSON.stringify([['X-Tenant','MCP_TENANT']]));h.input('bearer_token_env_var','MCP_BEARER_TOKEN');
 h.submit();await tick();const def=h.calls[0].body.definition;assert.deepEqual(def.headers,{'X-Client':'desktop'});assert.deepEqual(def.env_headers,{'X-Tenant':'MCP_TENANT'});assert.equal(def.bearer_token_env_var,'MCP_BEARER_TOKEN');
 h.click('edit',{connectionId:h.calls[0].body.connection_id});assert.match(h.c.render(),/MCP_TENANT/);assert.match(h.c.render(),/MCP 服务器名称/);assert.match(h.c.render(),/流式 HTTP/);assert.doesNotMatch(h.c.render(),/知识库/);
});

test('STDIO configuration sends fixed and inherited variables with cwd, preserving row deletion',async()=>{
 const h=harness();await h.form();h.input('transport','stdio');h.input('command','npx');h.input('env',JSON.stringify([['FIRST','one'],['SECOND','two']]));h.click('remove-pair',{connectionPair:'env',connectionIndex:'0'});h.input('inherit_env','MCP_HOME');h.input('cwd','~/code');
 h.submit();await tick();const def=h.calls[0].body.definition;assert.deepEqual(def.env,{SECOND:'two'});assert.deepEqual(def.inherit_env,['MCP_HOME']);assert.equal(def.cwd,'~/code');assert.equal(def.url,undefined);
});

test('header validation rejects duplicate and conflicting authorization without sending',async()=>{
 const h=harness();await h.form();h.input('headers',JSON.stringify([['X-Test','a'],['x-test','b']]));h.submit();await tick();assert.equal(h.calls.length,0);assert.match(h.c.render(),/键名重复/);
 h.input('headers',JSON.stringify([['Authorization','value']]));h.input('bearer_token_env_var','TOKEN');h.submit();await tick();assert.equal(h.calls.length,0);assert.match(h.c.render(),/一种授权方式/);
});


test('legacy drafts migrate once and missing original fields are still rejected',async()=>{
 const h=harness();h.ctx.conn.localState.getDraft=k=>h.saved.get(k)??"";await h.form();const old=JSON.parse(h.saved.get('connection-definition-v2'));for(const k of ['headers','env_headers','env','inherit_env','bearer_token_env_var','cwd'])delete old[k];
 h.saved.delete('connection-definition-v2');h.saved.set('connection-definition',JSON.stringify(old));h.c.reset();await h.c.load();h.c.open();assert.match(h.c.render(),/新连接/);assert.equal(h.saved.has('connection-definition'),false);assert.ok(h.saved.has('connection-definition-v2'));
 h.click('discard');h.c.reset();await h.c.load();h.c.open();assert.doesNotMatch(h.c.render(),/value="新连接"/);
 const bad=harness();bad.saved.set('connection-definition-v2','{}');await bad.c.load();bad.c.open();assert.match(bad.c.render(),/草稿损坏/);assert.equal(bad.saved.get('connection-definition-v2'),'{}');
});

test('map preset saves standard configuration only after an explicit submit, with a masked key',async()=>{
 const h=harness();await h.c.load();h.c.openPreset('amap');
 assert.match(h.c.render(),/配置高德地图/);assert.match(h.c.render(),/type="password"/);assert.doesNotMatch(h.c.render(),/id="connection-type-http"/);
 assert.equal(h.calls.length,0);h.submit();await tick();assert.equal(h.calls.length,0);assert.match(h.c.render(),/请填写高德地图/);
 h.c.input({target:{dataset:{connectionPair:'env',connectionIndex:'0',connectionPart:'1'},value:'fixture-'+'map-key'}});
 h.click('close');h.c.openPreset('amap');assert.match(h.c.render(),/配置高德地图/);
 h.submit();await tick();assert.equal(h.calls.length,1);const saved=h.calls[0].body;
 assert.equal(saved.definition.command,'npx');assert.deepEqual(saved.definition.args,['-y','@amap/amap-maps-mcp-server']);
 assert.deepEqual(saved.definition.env,{AMAP_MAPS_API_KEY:'fixture-'+'map-key'});assert.equal(saved.definition.transport,'stdio');
 assert.equal(h.session.connections[0].mounted,false);assert.doesNotMatch(h.c.render(),/fixture-map-key/);
 h.c.open(saved.connection_id);assert.match(h.c.render(),/配置高德地图/);assert.match(h.c.render(),/type="password"/);
});
test('map preset cannot overwrite another unfinished connector or a pending save',async()=>{
 const h=harness();await h.form();const before=h.saved.get('connection-definition-v2');h.c.openPreset('amap');
 assert.equal(h.saved.get('connection-definition-v2'),before);assert.match(h.c.render(),/已有未完成的连接配置/);
 h.click('discard');h.c.openPreset('unknown');assert.doesNotMatch(h.c.render(),/data-connection-form/);
 h.c.openPreset('amap');h.click('discard');assert.equal(h.saved.has('connection-definition-v2'),false);
});

test('map help uses the native browser callback with a fixed official destination',async()=>{
 const opened=[];const c=createConnectionController({context:()=>({}),render(){},openGuide:async value=>opened.push(value)});
 let prevented=false;
 c.click({preventDefault(){prevented=true;},target:{closest:()=>({dataset:{connectionAction:'preset-guide',connectionPreset:'amap',url:'https://invalid.test/'}})}});
 await tick();assert.equal(prevented,true);assert.equal(opened[0].url,'https://lbs.amap.com/api/mcp-server/create-project-and-key');
 c.click({preventDefault(){},target:{closest:()=>({dataset:{connectionAction:'preset-guide',connectionPreset:'unknown'}})}});
 assert.equal(opened.length,1);
});

test('cloud service token presets save official HTTP configurations without mounting or exposing tokens',async()=>{
 for(const [id,title,url] of [['supabase','Supabase','https://mcp.supabase.com/mcp'],['cloudflare','Cloudflare','https://mcp.cloudflare.com/mcp'],['github','GitHub','https://api.githubcopilot.com/mcp/']]){
  const h=harness();await h.c.load();h.c.openPreset(id);
  assert.match(h.c.render(),new RegExp(`配置 ${title}`));assert.match(h.c.render(),/id="connection-token"[^>]+type="password"/);
  h.submit();assert.equal(h.calls.length,0);assert.match(h.c.render(),/请填写有效的/);
  h.c.input({target:{dataset:{connectionToken:''},value:'fixture-'+id+'-token'}});
  h.click('close');h.c.reset();await h.c.load();h.c.openPreset(id);
  assert.match(h.c.render(),new RegExp(`配置 ${title}`));
  h.submit();await tick();assert.equal(h.calls.length,1);
  const d=h.calls[0].body.definition;assert.equal(d.transport,'http');assert.equal(d.url,url);
  assert.deepEqual(d.headers,{Authorization:'Bearer fixture-'+id+'-token'});assert.equal(d.oauth,undefined);
  assert.equal(h.session.connections[0].mounted,false);assert.doesNotMatch(h.c.render(),/fixture-.*-token/);
  h.c.open(h.calls[0].body.connection_id);assert.match(h.c.render(),/id="connection-token"[^>]+type="password"/);
  h.c.input({target:{dataset:{connectionToken:''},value:'Bearer invalid'}});h.submit();assert.equal(h.calls.length,1);
 }
});
test('Vercel preset saves a pinned authorization bridge only and preserves approval and credential ownership notes',async()=>{
 const h=harness();await h.c.load();h.c.openPreset('vercel');
 assert.match(h.c.render(),/客户端通过审核/);assert.match(h.c.render(),/断开对话不会撤销账号授权/);
 assert.doesNotMatch(h.c.render(),/connection-token|connection-env-0-1/);
 h.submit();await tick();assert.equal(h.calls.length,1);const def=h.calls[0].body.definition;
 assert.deepEqual(def,{name:'Vercel',transport:'stdio',timeout:120,command:'npx',args:['-y','mcp-remote@0.15.1','https://mcp.vercel.com','--auth-timeout','90']});
 h.c.open(h.calls[0].body.connection_id);assert.match(h.c.render(),/配置 Vercel/);
 assert.equal(h.bridge.length,0);assert.equal(h.session.connections[0].mounted,false);
});
test('custom authentication settings are never hidden by a service template',async()=>{
 const h=harness();await h.c.load();h.c.openPreset('supabase');
 h.input('env_headers',JSON.stringify([['X-Tenant','TENANT']]));
 assert.match(h.c.render(),/connection-env_headers-0-0/);assert.doesNotMatch(h.c.render(),/配置 Supabase/);
});
