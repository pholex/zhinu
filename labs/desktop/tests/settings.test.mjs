import test from "node:test";
import assert from "node:assert/strict";
import {createSettingsController,DEFAULT_UI,matchingSections} from "../web/settings.mjs";
import {ReadClient} from "../web/client.mjs";

function fixture() {
  const values={default_model:"",default_mode:"",timezone:"UTC",notifications:{completed:true,failed:true,approval:true,question:true,automation:true},ui:structuredClone(DEFAULT_UI)};
  let revision=0,fail=false,remote=structuredClone(values);const calls=[];
  const conn={controller:new AbortController(),client:{get:async()=>({revision,values:structuredClone(remote),options:{modes:[],models:[]},effective:{}}),post:async(path,body)=>{calls.push(body);if(fail||body.revision!==revision){const e=new Error("conflict");e.status=409;throw e;}remote=structuredClone(body.values);return {revision:++revision,values:remote};}}};
  const controller=createSettingsController({context:()=>({conn,writable:true}),render:()=>{}});
  return {controller,calls,externalChange(){revision++;remote.timezone="Asia/Tokyo";},fail(){fail=true;}};
}
const settle=()=>new Promise(r=>setTimeout(r,0));
test("settings preserve dirty input and original revision after concurrent window writes",async()=>{
  const f=fixture();await f.controller.load();
  f.controller.input({target:{dataset:{setting:"timezone"},value:"Europe/Paris",type:"text"}});
  f.controller.click({target:{closest:()=>({dataset:{settingsSection:"notifications"}})}});
  f.externalChange();await f.controller.load();
  assert.match(f.controller.render(),/Europe\/Paris/);
  f.controller.onSubmit({target:{id:"preferences-form"},preventDefault(){}});await settle();
  assert.equal(f.calls[0].revision,0);
  assert.match(f.controller.render(),/其他窗口修改/);
  assert.match(f.controller.render(),/Europe\/Paris/);
  assert.equal(f.controller.values().timezone,"UTC");
});
test("failed save never applies unsaved preference or changes live session configuration",async()=>{
  const f=fixture();await f.controller.load();f.fail();
  f.controller.input({target:{dataset:{setting:"default_model"},value:"candidate",type:"text"}});
  f.controller.onSubmit({target:{id:"preferences-form"},preventDefault(){}});await settle();
  assert.equal(f.controller.values().default_model,"");assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].values.default_model,"candidate");
});
test("settings search finds categories by described function",()=>{
  assert.equal(matchingSections("权限")[0][0],"models");assert.equal(matchingSections("does not exist").length,0);
});
test("product API client keeps writes narrowly scoped and sends identity headers",async()=>{
  const calls=[];const client=new ReadClient("http://localhost","fixture",async(url,options)=>{calls.push({url,...options});return {ok:true,json:async()=>({})};});
  for(const path of ["/desktop/preferences","/desktop/content/library?type=image","/desktop/content/search?q=hello","/desktop/sessions/s/export?format=markdown","/desktop/terminals/t/output?from=0"])await client.get(path);
  await client.post("/desktop/terminals/t/input",{text:"pwd\n",input_id:1},{instance:"instance"});
  assert.equal(calls.at(-1).headers["X-Xiaoyu-Instance"],"instance");
  const count=calls.length;
  for(const path of ["/desktop/providers/secrets","/desktop/content/../../file","/desktop/terminals/t/execute","/desktop/sessions/s/export","/desktop/terminals/t/input?force=true"])
    await assert.rejects(client.post(path,{}));
  assert.equal(calls.length,count);
});
test("settings block edits while save is in flight and do not lose a newer timezone",async()=>{
  let finish;const values={default_model:'',default_mode:'',timezone:'UTC',notifications:{},ui:structuredClone(DEFAULT_UI)};
  const conn={controller:new AbortController(),client:{get:async()=>({revision:0,values}),post:(_,body)=>new Promise(resolve=>{finish=()=>resolve({revision:1,values:body.values});})}};
  const c=createSettingsController({context:()=>({conn,writable:true}),render(){}});await c.load();
  c.input({target:{dataset:{setting:'timezone'},value:'Europe/Paris',type:'text'}});c.onSubmit({target:{id:'preferences-form'},preventDefault(){}});
  assert.match(c.render(),/fieldset class="preferences-fields" disabled/);
  c.input({target:{dataset:{setting:'timezone'},value:'Asia/Tokyo',type:'text'}});finish();await settle();assert.equal(c.values().timezone,'Europe/Paris');
});
test("file query may contain path separators while API route traversal stays rejected",async()=>{
  const calls=[];const c=new ReadClient('http://localhost','test',async url=>{calls.push(url);return {ok:true,json:async()=>({})};});
  await c.get('/desktop/content/file?project_id=one&path=docs%2Fnotes.md');
  await c.get('/desktop/content/search?q=a%2Fb');
  for(const path of ['/desktop/content/%2e%2e/file','/desktop/content/file?path=x#secret','/desktop/content/../file'])await assert.rejects(c.get(path));
  assert.equal(calls.length,2);
});

test("compact category navigation leaves search and preserves unsaved settings",async()=>{
  const f=fixture();await f.controller.load();
  f.controller.input({target:{id:"settings-search",value:"时区",dataset:{}}});
  f.controller.input({target:{dataset:{setting:"timezone"},value:"Europe/Paris",type:"text"}});
  f.controller.input({target:{id:"settings-section-select",value:"appearance",dataset:{}}});
  assert.match(f.controller.render(),/data-settings-section="appearance" aria-current="page"/);
  assert.match(f.controller.render(),/id="settings-search"[^>]*value=""/);
  f.controller.input({target:{id:"settings-section-select",value:"notifications",dataset:{}}});
  assert.match(f.controller.render(),/Europe\/Paris/);
  assert.equal(f.calls.length,0);
});


test("composer choices persist in order and survive reloading preferences",async()=>{
  const f=fixture();await f.controller.load();
  await Promise.all([f.controller.saveDefaultModel("first"),f.controller.saveDefaultModel("second")]);
  await f.controller.load();
  assert.equal(f.controller.values().default_model,"second");
  assert.deepEqual(f.calls.map(c=>c.revision),[0,1]);
  assert.equal(f.calls[1].values.timezone,"UTC");
});
test("composer choice cannot overwrite a dirty settings form or a conflicting revision",async()=>{
  const f=fixture();await f.controller.load();
  f.controller.input({target:{dataset:{setting:"timezone"},value:"Asia/Tokyo",type:"text"}});
  await assert.rejects(f.controller.saveDefaultModel("first"),/未保存/);
  assert.equal(f.calls.length,0);
  const g=fixture();await g.controller.load();g.externalChange();
  await assert.rejects(g.controller.saveDefaultModel("second"),/其他窗口/);
  assert.equal(g.controller.values().default_model,"");
});


test('settings plugin shortcut and compact selector open the shared manager without a landing page',()=>{
  const destinations=[];
  const c=createSettingsController({context:()=>({}),render(){},navigate:route=>destinations.push(route)});
  assert.match(c.render(),/data-settings-navigate="capabilities"/);
  assert.doesNotMatch(c.render(),/data-settings-section="integrations"/);
  c.click({target:{closest:()=>({dataset:{settingsNavigate:'capabilities'}})}});
  const select={id:'settings-section-select',value:'integrations'};
  c.input({target:select});
  assert.deepEqual(destinations,['capabilities','capabilities']);assert.equal(select.value,'general');
  c.input({target:{id:'settings-search',value:'MCP',dataset:{}}});
  assert.match(c.render(),/data-settings-navigate="capabilities"/);
  assert.doesNotMatch(c.render(),/打开插件管理|打开连接器/);
  assert.equal(destinations.length,2,'search never navigates away while typing');
});
