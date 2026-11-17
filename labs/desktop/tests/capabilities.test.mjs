import test from 'node:test';
import assert from 'node:assert/strict';
import {createCapabilitiesController,validateCapabilities,skillGroups,skillPresentation} from '../web/capabilities.mjs';
import {createProvidersController} from '../web/providers.mjs';
const tick=()=>new Promise(r=>setTimeout(r,0));
test('capabilities are actual loaded rows and selection drafts without starting execution',async()=>{
  const calls=[],drafts=[],workflows=[];
  const value={scope:'session_loaded',skills:[{id:'one',name:'<script>',description:'help',source:'user',loaded:true}],plugins:[],workflows:[{id:'code',label:'工程',kind:'task'},{id:'weave',label:'批量执行',kind:'qixiang'}],management:{}};
  const ctx={sid:'s',writable:true,conn:{healthy:true,health:{instance_id:'i'},client:{get:async path=>{calls.push(path);return value;},post:async()=>{throw new Error('must not submit');}}}};
  const c=createCapabilitiesController({context:()=>ctx,draftTask:x=>drafts.push(x),openWorkflow:x=>workflows.push(x)});await c.load();assert.doesNotMatch(c.render(),/<script>/);assert.match(c.render(),/本会话已加载/);
  const click=data=>c.click({target:{closest:()=>({dataset:data})}});
  click({capabilityAction:'skill',skill:'one'});assert.match(drafts[0].text,/one/);assert.doesNotMatch(c.render(),/开始一种工作|data-workflow/);assert.equal(calls.length,2);
  assert.throws(()=>validateCapabilities({skills:[],plugins:[],workflows:[],scope:'fake'}));
});
test('provider secret only goes in explicit POST then disappears from form state, never local drafts',async()=>{
  let sent=null;const ctx={writable:true,conn:{client:{get:async()=>({revision:'a'.repeat(64),providers:[]}),post:async(path,data)=>{sent={path,data:structuredClone(data)};return {revision:'b'.repeat(64),providers:[]};}},localState:{setDraft:()=>{throw new Error('credential must never enter drafts');}}}};
  const c=createProvidersController({context:()=>ctx});await c.load();
  for(const [field,value]of Object.entries({id:'myservice',base_url:'https://example.test/v1',models:'m1,m2',api_key_action:'set',api_key:'private-secret'}))c.input({target:{dataset:{providerField:field},value}});
  assert.doesNotMatch(c.render(),/private-secret/);c.onSubmit({target:{hasAttribute:x=>x==='data-provider-form'},preventDefault(){}});await tick();
  assert.equal(sent.path,'/desktop/providers');assert.equal(sent.data.provider.api_key,'private-secret');assert.deepEqual(sent.data.provider.models,['m1','m2']);assert.doesNotMatch(c.render(),/private-secret/);assert.match(c.render(),/重启服务/);
});
test('provider refresh preserves dirty revision; form rerender does not require retyping an in-memory key',async()=>{
  let revision='a'.repeat(64),sent;const conn={controller:new AbortController(),client:{get:async()=>({revision,providers:[]}),post:async(_,body)=>{sent=body;return {revision:'c'.repeat(64),providers:[]};}}};
  const c=createProvidersController({context:()=>({conn,writable:true}),render(){}});await c.load();
  for(const [field,value] of Object.entries({id:'local',base_url:'https://example.invalid',models:'m',api_key_action:'set',api_key:'test-secret-never-render'}))c.input({target:{dataset:{providerField:field},value}});
  assert.match(c.render(),/已输入新密钥（仅内存）/);assert.doesNotMatch(c.render(),/test-secret-never-render/);
  revision='b'.repeat(64);await c.load();assert.match(c.render(),/配置已有更新/);
  c.onSubmit({target:{hasAttribute:()=>true},preventDefault(){}});await new Promise(r=>setTimeout(r,0));assert.equal(sent.revision,'a'.repeat(64));
});
test('startup discovery is distinguished from loaded session index and shows configured sources',async()=>{
  const value={scope:'installed_user_sources',skills_enabled:true,skills:[{id:'shared',name:'shared',description:'local',source:'user',directory:'/home/test/.agents/skills/shared',loaded:false}],plugins:[],workflows:[],discovery:{status:'ready',enabled:true,count:1,sources:[{kind:'user',path:'/home/test/.agents/skills'}],disabled_patterns:['excluded-*']},startup_discovery:{status:'ready'}};
  const conn={controller:new AbortController(),client:{get:async()=>value}};
  const c=createCapabilitiesController({context:()=>({conn,sid:''})});await c.load();
  assert.match(c.render(),/已发现 1 项/);assert.match(c.render(),/新会话自动建立可用技能索引/);
  assert.match(c.render(),/尚未加载到当前会话/);assert.doesNotMatch(c.render(),/本会话已加载索引/);
  assert.match(c.render(),/\.agents\/skills/);assert.match(c.render(),/excluded-\*/);
});
test('explicit disabled discovery does not claim startup loading succeeded',async()=>{
  const value={scope:'installed_user_sources',skills_enabled:false,skills:[],plugins:[],workflows:[],discovery:{status:'disabled',enabled:false,count:0,sources:[],disabled_patterns:[]},startup_discovery:{status:'disabled'}};
  const conn={client:{get:async()=>value}};const c=createCapabilitiesController({context:()=>({conn,sid:''})});await c.load();
  assert.match(c.render(),/技能自动加载已由核心配置关闭/);assert.doesNotMatch(c.render(),/已发现 0 项/);
});
test('late discovery from an old connection or selected session cannot replace current capabilities',async()=>{
  let resolve;const value={scope:'session_loaded',skills:[{id:'old',description:'old session',name:'old',loaded:true}],plugins:[],workflows:[]};
  const conn={client:{get:path=>path.includes('?')?new Promise(r=>resolve=r):Promise.resolve({...value,skills:[]})}},ctx={conn,sid:'old'};
  const c=createCapabilitiesController({context:()=>ctx});const pending=c.load();ctx.sid='new';resolve(value);await pending;
  assert.doesNotMatch(c.render(),/old session/);
  const second=c.load();ctx.conn={client:{}};resolve(value);await second;assert.doesNotMatch(c.render(),/old session/);
});

test('installed skills merge user inventory with session skills without calling installed items loaded',async()=>{
  const inventory={scope:'installed_user_sources',skills:[{id:'shared',description:'shared method'},{id:'new',description:'newly installed'}],plugins:[],workflows:[]};
  const loaded={...inventory,scope:'session_loaded',skills:[{id:'shared',description:'shared method'},{id:'project-only',description:'project method'}]};
  const ctx={sid:'s',conn:{client:{get:async p=>p.includes('?')?loaded:inventory}}};
  const c=createCapabilitiesController({context:()=>ctx});await c.load();const html=c.render();
  assert.match(html,/newly installed/);assert.match(html,/project method/);
  assert.match(html,/data-key="plugin-skill-new"[\s\S]*?尚未加载到当前会话/);
  assert.doesNotMatch(html,/plugin-entry-state/);
  assert.match(html,/data-key="plugin-skill-shared"[\s\S]*?本会话已加载索引/);
  ctx.sid='different';assert.doesNotMatch(c.render(),/newly installed|project method/);
});
test('plugin views separate discovery from management and keep connector transport below category',async()=>{
  const calls=[];const c=createCapabilitiesController({context:()=>({writable:true,conn:{client:{get:async()=>({scope:'installed_user_sources',skills:[{id:'notes',description:'meeting workflow'}],plugins:[{id:'bundle',description:'a package',connections:['server']}],workflows:[]})}}}),connectors:{load:()=>calls.push('load'),render:({query})=>`connector-list:${query}`,open:()=>calls.push('open')}});
  // Keep one stable service identity as the real application does.
  const click=data=>c.click({target:{closest:()=>({dataset:data})}});
  click({capabilityAction:'type',type:'connectors'});assert.equal(calls[0],'load');
  assert.match(c.render(),/connector-list:/);assert.doesNotMatch(c.render(),/data-type="mcp"|data-type="plugins"/);
  c.input({target:{dataset:{capabilitySearch:''},value:'old query'}});
  click({capabilityAction:'view',view:'discover'});assert.match(c.render(),/添加 MCP 连接器/);assert.doesNotMatch(c.render(),/old query|connector-list:/);
  click({capabilityAction:'add-connector'});assert.equal(calls.at(-1),'open');assert.match(c.render(),/connector-list:/);
  click({capabilityAction:'type',type:'skills'});click({capabilityAction:'discover'});
  assert.match(c.render(),/从本机添加技能/);assert.match(c.render(),/从扩展包添加/);assert.doesNotMatch(c.render(),/data-capability-action="skill"/);
});
test('failed session inventory retains installed skill discovery with honest status',async()=>{
  const conn={client:{get:async p=>{if(p.includes('?'))throw new Error('offline');return {scope:'installed_user_sources',skills:[{id:'one',description:'installed item'}],plugins:[],workflows:[]};}}};
  const c=createCapabilitiesController({context:()=>({conn,sid:'s'})});await c.load();
  assert.match(c.render(),/installed item/);assert.match(c.render(),/当前会话的技能状态未能读取/);assert.doesNotMatch(c.render(),/本会话已加载索引/);
});


test('skill families respect explicit package provenance and preserve every identifier',()=>{
 const skills=[{id:'aws-compute',description:'compute'},{id:'aws-auth',description:'auth'},{id:'lark-approval',description:'approve'},{id:'aws-custom',plugin:'team-tools',description:'custom'},{id:'awesome-tool',description:'aws mentioned only in description'},{id:'pdf',description:'docs'}];
 const groups=skillGroups(skills);assert.equal(groups.find(g=>g.id==='aws').total,2);assert.equal(groups.find(g=>g.id==='package:team-tools').skills[0].id,'aws-custom');
 assert.equal(groups.find(g=>g.id==='other').skills[0].id,'awesome-tool');assert.deepEqual(groups.flatMap(g=>g.skills.map(s=>s.id)).sort(),skills.map(s=>s.id).sort());
 assert.equal(skillPresentation(skills[2]).name,'审批');assert.equal(skillGroups(skills,'审批')[0].skills[0].id,'lark-approval');assert.equal(skillGroups(skills,'lark-cli')[0].id,'lark');
 assert.equal(skillGroups(skills,'  AWS-AUTH  ')[0].skills.length,1);assert.equal(skillGroups(skills,'不存在').length,0);
});

test('search expands matches while preserving manual family expansion and exact skill use',async()=>{
 const value={scope:'installed_user_sources',skills:[{id:'aws-auth',description:'User auth'},{id:'aws-compute',description:'Virtual machines'},{id:'lark-approval',description:'审批任务'}],plugins:[],workflows:[]},drafts=[];
 const conn={client:{get:async()=>value}},c=createCapabilitiesController({context:()=>({conn}),draftTask:v=>drafts.push(v)});await c.load();
 const click=data=>c.click({target:{closest:()=>({dataset:data})}}),search=value=>c.input({target:{dataset:{capabilitySearch:''},value}});
 const expanded=id=>new RegExp('id="skill-group-'+id+'"[^>]*aria-expanded="true"').test(c.render());
 assert.equal(expanded('aws'),false);assert.match(c.render(),/3 个技能 · 2 个分组/);
 click({capabilityAction:'group',group:'aws'});assert.equal(expanded('aws'),true);
 search('审批');assert.equal(expanded('lark'),true);assert.match(c.render(),/找到 1 个技能/);assert.doesNotMatch(c.render(),/plugin-skill-aws-auth/);
 click({capabilityAction:'group',group:'lark'});assert.equal(expanded('lark'),false);
 search('AWS');assert.equal(expanded('aws'),true);search('');assert.equal(expanded('aws'),true);assert.equal(expanded('lark'),false);
 search('auth');click({capabilityAction:'skill',skill:'aws-auth'});assert.equal(drafts[0].skill,'aws-auth');assert.match(drafts[0].text,/技能 aws-auth/);
 c.reset();await c.load();assert.equal(expanded('aws'),false);
});


test('skill switches save explicit choices, keep disabled rows and scope filtered group changes',async()=>{
 let revision=0,disabled=new Set(),pendingResolve,fail=false;const requests=[],drafts=[];
 const inventory=()=>({scope:'installed_user_sources',skills:[{id:'aws-auth',description:'auth',enabled:!disabled.has('aws-auth')},{id:'aws-compute',description:'compute',enabled:!disabled.has('aws-compute')},{id:'lark-im',description:'chat',enabled:false,toggle_locked:true}],plugins:[],workflows:[],skill_settings:{revision,disabled:[...disabled]}});
 const conn={healthy:true,health:{instance_id:'i'},client:{get:async()=>inventory(),post:async(path,body,options)=>{requests.push({path,body,options});if(fail)throw Object.assign(new Error('conflict'),{status:409});await new Promise(r=>pendingResolve=r);for(const id of body.ids)body.enabled?disabled.delete(id):disabled.add(id);revision++;return {note:'设置已保存。'};}}};
 const c=createCapabilitiesController({context:()=>({conn,writable:true}),draftTask:d=>drafts.push(d)});await c.load();
 const click=data=>c.click({target:{closest:()=>({dataset:data})}}),toggle=(ids,enabled)=>click({capabilityAction:'toggle-skill',skillIds:JSON.stringify(ids),enable:String(enabled)});
 toggle(['aws-auth'],false);toggle(['aws-compute'],false);assert.equal(requests.length,1);assert.equal(requests[0].body.revision,0);assert.equal(requests[0].options.instance,'i');pendingResolve();await tick();await tick();
 assert.match(c.render(),/skill-disabled/);assert.match(c.render(),/已启用 1 \/ 2/);assert.match(c.render(),/role="checkbox" aria-checked="mixed"/);
 click({capabilityAction:'skill',skill:'aws-auth'});assert.equal(drafts.length,0);
 c.input({target:{dataset:{capabilitySearch:''},value:'compute'}});assert.match(c.render(),/data-skill-ids="\[&quot;aws-compute&quot;\]"/);assert.doesNotMatch(c.render(),/plugin-skill-aws-auth/);
 toggle(['lark-im'],true);assert.equal(requests.length,1);
 fail=true;toggle(['aws-compute'],false);await tick();await tick();assert.match(c.render(),/其他窗口变化/);assert.equal(disabled.has('aws-compute'),false);
});

test('connector discovery offers map setup without connecting and filters by service name',()=>{
 const opened=[],ctx={writable:true,conn:{healthy:true}};
 const c=createCapabilitiesController({context:()=>ctx,connectors:{openPreset:id=>opened.push(id),load(){},render(){return 'configured list';}}});
 const click=dataset=>c.click({target:{closest:()=>({dataset})}});
 click({capabilityAction:'type',type:'connectors'});click({capabilityAction:'view',view:'discover'});
 assert.match(c.render(),/高德地图/);assert.match(c.render(),/data-connector-preset="amap"/);assert.equal(opened.length,0);
 click({capabilityAction:'configure-connector',connectorPreset:'unknown'});assert.equal(opened.length,0);
 click({capabilityAction:'configure-connector',connectorPreset:'amap'});assert.deepEqual(opened,['amap']);assert.match(c.render(),/configured list/);
 ctx.writable=false;click({capabilityAction:'configure-connector',connectorPreset:'amap'});assert.equal(opened.length,1);
});

test('cloud connector discovery searches and configures all retained providers without Tailscale',()=>{
 const opened=[],ctx={writable:true,conn:{healthy:true}};
 const c=createCapabilitiesController({context:()=>ctx,connectors:{openPreset:id=>opened.push(id),load(){},render(){return '';}}});
 const click=data=>c.click({target:{closest:()=>({dataset:data})}});
 click({capabilityAction:'type',type:'connectors'});click({capabilityAction:'view',view:'discover'});
 assert.doesNotMatch(c.render(),/Tailscale|WPS/i);
 for(const id of ['supabase','vercel','cloudflare','github']){
  click({capabilityAction:'view',view:'discover'});
  c.input({target:{dataset:{capabilitySearch:''},value:id.toUpperCase()}});
  assert.match(c.render(),new RegExp(`data-connector-preset="${id}"`));
  assert.doesNotMatch(c.render(),/data-connector-preset="amap"/);
  click({capabilityAction:'configure-connector',connectorPreset:id});
 }
 assert.deepEqual(opened,['supabase','vercel','cloudflare','github']);
});
