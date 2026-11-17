import {CONNECTOR_PRESETS} from "./connections.mjs";
import {icon} from "./ui.mjs";
import {escapeHTML as h} from "./client.mjs";

export function validateCapabilities(value){
  if(!value || !Array.isArray(value.skills) || !Array.isArray(value.plugins) || !Array.isArray(value.workflows) || !["installed_user_sources","session_loaded"].includes(value.scope) || value.skills.some(s=>typeof s.id!=="string"||typeof s.description!=="string") || value.plugins.some(p=>typeof p.id!=="string"||typeof p.description!=="string"))throw new Error("插件清单无法读取，请刷新重试。");
  return value;
}
export const workflowDrafts={chat:"",create:"请帮我创作以下内容：\n\n目标：\n受众：\n交付形式：",code:"请在当前项目完成以下工作：\n\n目标：\n约束：\n验收方式："};
const types=[["skills","技能","工作方法与流程"],["connectors","连接器","连接服务与数据"]];
const matches=(item,q)=>[item.name,item.title,item.id,item.description,item.plugin].filter(Boolean).join(" ").toLocaleLowerCase().includes(q.trim().toLocaleLowerCase());
// Display aliases never change the skill identifier sent to the conversation.
const skillLabels={
  'aws-ai-ml':'机器学习与模型训练','aws-auth':'用户登录与身份认证','aws-billing-and-cost-management':'费用与预算','aws-blocks':'全栈应用开发','aws-cdk':'基础设施代码','aws-cloudformation':'基础设施模板','aws-compute':'云服务器','aws-containers':'容器服务','aws-database':'数据库','aws-deployment':'部署与持续交付','aws-iam':'身份与访问权限','aws-messaging-and-streaming':'消息与数据流','aws-networking':'云网络','aws-observability':'监控与日志','aws-sdk-js-v3-usage':'JavaScript SDK','aws-sdk-python-usage':'Python SDK','aws-sdk-swift-usage':'Swift SDK','aws-security':'安全服务','aws-serverless':'无服务器应用','aws-storage':'云存储','amazon-bedrock':'生成式 AI 应用','launch-with-aws':'应用迁移与上线','signing-in-to-aws':'AWS 登录',
  'lark-approval':'审批','lark-apps':'应用搭建','lark-attendance':'考勤打卡','lark-base':'多维表格','lark-calendar':'日历与会议室','lark-contact':'通讯录','lark-doc':'云文档','lark-drive':'云空间与文件','lark-event':'事件订阅','lark-im':'消息与群聊','lark-mail':'邮箱','lark-markdown':'Markdown 文档','lark-meeting':'会议与纪要','lark-minutes':'会议纪要入口','lark-note':'会议笔记入口','lark-okr':'目标与关键结果','lark-openapi-explorer':'开放接口探索','lark-shared':'登录与授权','lark-sheets':'电子表格','lark-skill-maker':'自定义技能','lark-slides':'幻灯片','lark-task':'任务与待办','lark-vc':'视频会议入口','lark-vc-agent':'会议助手入口','lark-whiteboard':'画板','lark-wiki':'知识空间','lark-workflow-meeting-summary':'会议纪要汇总','lark-workflow-standup-report':'日程与待办摘要',
  'remotion-best-practices':'视频制作指南','remotion-captions':'字幕','remotion-create':'创建视频','remotion-docs':'文档查询','remotion-interactivity':'视频交互','remotion-maps':'地图动画','remotion-markup':'内容与动画','remotion-multimedia':'多媒体处理','remotion-render':'视频导出','remotion-saas':'视频应用开发','remotion-studio':'视频预览','remotion-upgrade':'版本升级',
  'pdf':'PDF 文档','documents':'文档编辑','presentations':'演示文稿','spreadsheets':'电子表格','tts':'文字转语音','video-translation':'视频翻译','imagegen':'图像生成','brandkit':'品牌视觉','design-taste-frontend':'前端设计','redesign-existing-projects':'界面改进','ui-ux-design-pro':'交互与界面设计','minimalist-ui':'简约界面设计','image-to-code':'图片转界面','high-end-visual-design':'视觉设计','gpt-taste':'动效与界面设计','industrial-brutalist-ui':'工业风格界面','imagegen-frontend-mobile':'移动端界面设计','imagegen-frontend-web':'网页视觉设计','stitch-design-taste':'设计系统','fireworks-tech-graph':'技术图解',
};
const series={
  aws:{title:'AWS',description:'云服务部署、运维与成本管理'},
  lark:{title:'飞书',description:'消息、文档、日历与审批'},
  remotion:{title:'Remotion',description:'视频制作、字幕与动画'},
  design:{title:'设计与图像',description:'界面设计、图像与视觉创作'},
  documents:{title:'文档与内容',description:'文档、表格、演示与语音'},
  other:{title:'其他技能',description:'独立的工作方法与工具'},
};
const designIds=new Set(['brandkit','design-taste-frontend','design-taste-frontend-v1','redesign-existing-projects','ui-ux-design-pro','minimalist-ui','image-to-code','high-end-visual-design','gpt-taste','industrial-brutalist-ui','imagegen','imagegen-frontend-mobile','imagegen-frontend-web','stitch-design-taste','fireworks-tech-graph']);
const documentIds=new Set(['pdf','documents','presentations','spreadsheets','tts','video-translation','characteristic-voice','daily-news-caster']);
export function skillPresentation(skill){
  const id=skill.id,short=id.split(':').at(-1),label=skillLabels[id]||skillLabels[short];
  const plugin=typeof skill.plugin==='string'?skill.plugin:'';
  // An explicit extension source wins over prefix-based family inference.
  let family=plugin?({'lark-cli':'lark',lark:'lark',aws:'aws',remotion:'remotion'}[plugin]||`package:${plugin}`):'';
  if(!family)family=/^aws-/.test(id)||['amazon-bedrock','launch-with-aws','signing-in-to-aws'].includes(id)?'aws':/^lark-/.test(id)?'lark':/^remotion-/.test(id)?'remotion':designIds.has(short)?'design':documentIds.has(short)?'documents':'other';
  return {name:(skill.name&&skill.name!==id&&skill.name!==short?skill.name:label)||skill.name||id,family,...(series[family]||{title:plugin,description:'来自同一扩展包的技能'})};
}
export function skillGroups(skills,query=''){
  const groups=new Map(),q=query.trim().toLocaleLowerCase();
  for(const skill of skills){
    const presentation=skillPresentation(skill),{family,title,description}=presentation;
    if(!groups.has(family))groups.set(family,{id:family,title,description,total:0,skills:[]});
    const group=groups.get(family);group.total++;
    if(!q||[skill.id,skill.name,skill.description,skill.plugin,presentation.name,title,family==='lark'?'lark-cli feishu':''].filter(Boolean).join(' ').toLocaleLowerCase().includes(q))group.skills.push(skill);
  }
  return [...groups.values()].filter(g=>g.skills.length).sort((a,b)=>a.id==='other'?1:b.id==='other'?-1:b.total-a.total||a.title.localeCompare(b.title,'zh-CN')).map(g=>({...g,skills:g.skills.sort((a,b)=>skillPresentation(a).name.localeCompare(skillPresentation(b).name,'zh-CN'))}));
}

export function createCapabilitiesController(ui){
  let snapshot=null,loading=false,error="",notice="",query="",generation=0,snapshotConn=null,snapshotSid="",view="installed",type="skills",reloading=false,toggling=false;
  const expandedGroups=new Set(),searchCollapsed=new Set();
  const ctx=()=>ui.context(),paint=()=>ui.render?.();
  const current=()=>snapshotConn===ctx().conn&&snapshotSid===(ctx().sid||"")?snapshot:null;
  async function load(){
    const {conn,sid=""}=ctx();if(!conn)return;const gen=++generation;
    if(snapshotConn!==conn||snapshotSid!==sid)snapshot=null;
    loading=true;error="";paint();
    try{
      const requests=[conn.client.get("/desktop/capabilities",conn.controller?.signal)];
      if(sid)requests.push(conn.client.get(`/desktop/capabilities?session_id=${encodeURIComponent(sid)}`,conn.controller?.signal));
      const [all,session]=await Promise.allSettled(requests);
      if(gen!==generation||ctx().conn!==conn||(ctx().sid||"")!==sid)return;
      if(all.status!=="fulfilled")throw all.reason;
      const installed=validateCapabilities(all.value),rows=new Map(installed.skills.map(s=>[s.id,{...s,loaded:false}]));
      let warning=installed.warning||"",sessionEnabled=null;
      if(session?.status==="fulfilled"){
        const loaded=validateCapabilities(session.value);sessionEnabled=loaded.skills_enabled;
        for(const s of loaded.skills)rows.set(s.id,{...s,loaded:s.loaded!==false});
      }else if(session)warning=[warning,"当前会话的技能状态未能读取，请刷新重试。"].filter(Boolean).join(" ");
      snapshot={...installed,skills:[...rows.values()],warning,sessionEnabled};snapshotConn=conn;snapshotSid=sid;
    }catch(e){if(gen===generation&&ctx().conn===conn)error=e.message;}
    finally{if(gen===generation){loading=false;paint();}}
    if(gen===generation&&type==="connectors")await ui.connectors?.load();
  }
  function sources(data){
    const d=data?.discovery;if(!d)return "";
    return `<details class="plugin-sources" data-key="plugin-sources"><summary>来源与加载设置</summary><p class="note">${d.enabled===false?"技能自动加载已由核心配置关闭。":`已发现 ${h(d.count)} 项技能。新会话自动建立可用技能索引，正文按需读取。`}</p>${d.disabled_patterns?.length?`<p class="note">已停用：${h(d.disabled_patterns.join("、"))}</p>`:""}${(d.sources||[]).map(s=>`<p>${s.kind==="plugin"?`来自 ${h(s.plugin)}`:"本机技能目录"}<br><code>${h(s.path)}</code></p>`).join("")}${data.startup_discovery?.status==="error"?`<p role="alert">${h(data.startup_discovery.warning)}</p>`:""}</details>`;
  }
  function packages(data){
    if(!data?.plugins?.length)return "";
    return `<details class="plugin-sources" data-key="plugin-packages"><summary>安装来源 · ${data.plugins.length} 个扩展包</summary>${data.plugins.map(p=>`<section><strong>${h(p.name||p.id)}</strong> <small>${h(p.version)}</small><p>${h(p.description)}</p>${p.skills?.length?`<p class="note">技能：${h(p.skills.join("、"))}</p>`:""}${p.connections?.length?`<p class="note">声明的连接器：${h(p.connections.join("、"))}。可用状态需在对应会话中确认。</p>`:""}</section>`).join("")}</details>`;
  }
  const canToggle=()=>!!(ctx().writable&&ctx().conn?.healthy&&Number.isInteger(current()?.skill_settings?.revision)&&!loading&&!toggling&&!error);
  function skillSwitch(ids,checked,label,locked=false,scope='skill'){return `<button id="skill-switch-${scope}-${h(encodeURIComponent(ids.join('|')))}" type="button" class="skill-switch" role="${scope==='group'?'checkbox':'switch'}" aria-checked="${checked}" aria-label="${h(label)}" title="${h(locked?'核心配置已停用此技能':label)}" data-capability-action="toggle-skill" data-skill-ids="${h(JSON.stringify(ids))}" data-enable="${checked!==true}" ${!canToggle()||locked||!ids.length?'disabled':''}><span></span></button>`;}
  function skillRow(s){return `<div class="skill-item ${s.enabled===false?'skill-disabled':''}"><details class="plugin-entry" data-key="plugin-skill-${h(s.id)}"><summary>${icon("document")}<span class="plugin-entry-text"><strong>${h(skillPresentation(s).name)}</strong><span>${h(skillPresentation(s).name!==s.id?s.id:s.description||"查看技能说明")}</span></span>${s.enabled===false?'<span class="skill-disabled-label">已禁用</span>':''}${icon("chevron")}</summary><div class="plugin-entry-body"><p class="note">技能 ID：<code>${h(s.id)}</code></p><p>${h(s.description)}</p><p class="note">${s.plugin?`来自 ${h(s.plugin)}`:{user:"本机技能",project:"项目技能",plugin:"扩展包技能"}[s.source]||"本机技能"} · ${s.loaded?"本会话已加载索引，正文按需读取。":"尚未加载到当前会话；新会话将按配置读取。"}</p>${s.enabled===false?`<p class="note">${s.toggle_locked?"由核心配置停用。":"已禁用，文件保留，可随时重新启用。"}${s.loaded?" 当前对话尚未更新，将在下一轮应用。":""}</p>`:""}${s.when_not?`<p>不适用于：${h(s.when_not)}</p>`:""}<button type="button" data-capability-action="skill" data-skill="${h(s.id)}" ${s.enabled===false?"disabled":""}>${icon("edit")}在对话中使用</button>${s.directory?`<details><summary>文件位置</summary><code>${h(s.directory)}</code></details>`:""}</div></details>${skillSwitch([s.id],s.enabled!==false,`${s.enabled===false?"启用":"禁用"} ${skillPresentation(s).name}`,s.toggle_locked)}</div>`;}
  function skills(data){
    const groups=skillGroups(data?.skills||[],query),count=groups.reduce((n,g)=>n+g.skills.length,0),searching=!!query.trim(),{sid,writable,conn}=ctx();
    const rows=groups.map(g=>{
      const open=searching?!searchCollapsed.has(g.id):expandedGroups.has(g.id),key=encodeURIComponent(g.id),eligible=g.skills.filter(s=>!s.toggle_locked),enabled=g.skills.filter(s=>s.enabled!==false).length,eligibleEnabled=eligible.filter(s=>s.enabled!==false).length,checked=eligibleEnabled===eligible.length&&eligible.length?true:eligibleEnabled===0?false:"mixed";
      return `<section class="skill-group"><div class="skill-group-header"><h3><button id="skill-group-${h(key)}" type="button" class="skill-group-toggle" data-capability-action="group" data-group="${h(g.id)}" aria-expanded="${open}" aria-controls="skill-group-body-${h(key)}">${icon("chevron")}<span class="skill-group-text"><strong>${h(g.title)}</strong><span>${h(g.description)}</span></span><span class="skill-group-count">已启用 ${enabled} / ${g.skills.length}</span></button></h3>${skillSwitch(eligible.map(s=>s.id),checked,`${checked===true?"禁用":"启用"}${searching?"匹配的":"整组"} ${g.title} 技能`,false,"group")}</div><div id="skill-group-body-${h(key)}" class="skill-group-body" ${open?'':'hidden'}>${g.skills.map(skillRow).join('')}</div></section>`;
    }).join('');
    return `<div class="plugin-list-heading"><p class="note" role="status">${data?(searching?`找到 ${count} 个技能`:`${count} 个技能 · ${groups.length} 个分组`):''}</p>${sid?`<button type="button" class="quiet" data-capability-action="reload" ${!writable||!conn?.healthy||loading||reloading?"disabled":""}>${icon("refresh")}更新当前会话</button>`:""}</div>${data?.skills_enabled===false||data?.sessionEnabled===false?'<p class="note">技能自动加载已由核心配置关闭。</p>':""}<div class="plugin-list">${rows||(data||!loading&&!error?`<div class="plugin-empty">${icon("document")}<h3>${searching?"没有匹配的技能":"还没有添加技能"}</h3><p>${searching?"换个名称或关键词试试。":"添加工作流程，让小羽按你的方法完成任务。"}</p>${searching?'':`<button type="button" data-capability-action="discover">添加技能</button>`}</div>`:"")}</div>${sources(data)}${packages(data)}`;
  }
  function discover(data){
    const options=type==="skills"?[
      {id:"local",title:"从本机添加技能",description:"将技能文件放入技能目录，刷新后即可查看。",symbol:"folder"},
      {id:"bundle",title:"从扩展包添加",description:"通过已有安装入口，添加一组技能或连接器。",symbol:"capabilities"},
    ]:[...CONNECTOR_PRESETS.map(p=>({...p,preset:true})),{id:"mcp",title:"添加 MCP 连接器",description:"连接远程服务，或启动本机 MCP 服务。",symbol:"connection"}];
    const rows=options.filter(s=>matches(s,query));
    return `<p class="note plugin-list-heading">${type==="skills"?"添加新的工作方法与流程":"接入你需要的服务和数据"}</p><div class="plugin-discovery">${rows.map(o=>`<article><div class="plugin-discovery-icon">${o.brandIcon?`<img class="plugin-brand-icon" data-brand="${h(o.id)}" src="./${h(o.brandIcon)}" alt="" width="32" height="32">`:icon(o.symbol)}</div><h3>${o.title}</h3><p>${o.description}</p>${o.preset?`<button type="button" data-capability-action="configure-connector" data-connector-preset="${h(o.id)}" ${!ctx().writable||!ui.connectors?.openPreset?"disabled":""}>配置</button>`:o.id==="mcp"?`<button type="button" data-capability-action="add-connector" ${!ctx().writable||!ui.connectors?"disabled":""}>${icon("plus")}添加连接器</button>`:o.id==="local"?`<details data-key="plugin-add-local"><summary>查看添加方法</summary><p>每个技能单独一个文件夹，其中包含 <code>SKILL.md</code>。放入下面的本机技能目录后，刷新清单。</p>${(data?.discovery?.sources||[]).filter(s=>s.kind!=="plugin").map(s=>`<code class="plugin-source-path">${h(s.path)}</code>`).join("")||'<p class="note">连接服务并刷新后，可查看当前配置的技能目录。</p>'}<button type="button" data-capability-action="rescan" ${loading?"disabled":""}>刷新技能清单</button></details>`:`<details data-key="plugin-add-bundle"><summary>查看安装方法</summary><p>在终端使用下列命令，将“来源”替换为扩展包路径或地址，审阅安装提示后继续。</p><code class="plugin-source-path">${h(data?.management?.install?.command||"xiaoyu plugin add <来源>")}</code><p class="note">安装完成后刷新技能清单；新连接器需单独配置和连接。</p><button type="button" data-capability-action="rescan" ${loading?"disabled":""}>刷新技能清单</button></details>`}</article>`).join("")||'<p class="plugin-empty">没有匹配的添加方式。试试“本机”“扩展包”或“MCP”。</p>'}</div>`;
  }
  function render(){
    const data=current();
    return `<section class="capabilities-page plugin-hub"><header class="plugin-heading"><div><h2>插件</h2><p>添加技能，连接服务，让小羽完成更多工作。</p></div><button type="button" class="quiet icon-button" data-capability-action="refresh" aria-label="刷新插件清单" title="刷新插件清单" ${loading?"disabled":""}>${icon("refresh")}</button></header><nav class="plugin-views" aria-label="插件浏览方式">${[["installed","已添加"],["discover","发现"]].map(([id,label])=>`<button id="plugin-view-${id}" type="button" data-capability-action="view" data-view="${id}" aria-pressed="${view===id}">${label}</button>`).join("")}</nav><div class="plugin-toolbar"><nav class="plugin-types" aria-label="插件类型">${types.map(([id,label,description])=>`<button id="plugin-type-${id}" type="button" data-capability-action="type" data-type="${id}" title="${description}" aria-pressed="${type===id}">${icon(id==="skills"?"document":"connection")}${label}</button>`).join("")}</nav><label class="plugin-search">${icon("search")}<input id="plugin-search" data-capability-search type="search" value="${h(query)}" placeholder="搜索${type==="skills"?"技能":"连接器"}" aria-label="搜索${type==="skills"?"技能":"连接器"}"></label></div>${loading?'<p role="status" class="note">正在读取插件清单…</p>':""}${error?`<p role="alert" class="form-error">${h(error)}${data?" 当前显示上次读取的清单。":""}</p>`:""}${notice?`<p role="status" class="note">${h(notice)}</p>`:""}${data?.warning?`<p role="alert" class="note">${h(data.warning)}</p>`:""}<div id="plugin-content">${view==="discover"?discover(data):type==="skills"?skills(data):ui.connectors?.render({embedded:true,query})||'<p class="plugin-empty">当前服务不支持连接器管理。</p>'}</div></section>`;
  }
  async function toggleSkills(ids,enabled,focusId=""){
    if(!canToggle()||!Array.isArray(ids)||!ids.length||ids.some(id=>!current()?.skills.some(s=>s.id===id&&!s.toggle_locked)))return;
    const {conn,sid=''}=ctx(),policy=current().skill_settings;let problem='';toggling=true;notice='';paint();
    try{const result=await conn.client.post('/desktop/skills/settings',{revision:policy.revision,ids,enabled,session_id:sid},{instance:conn.health?.instance_id});if(ctx().conn===conn)notice=result.note;}
    catch(e){problem=e.status===409?'设置已在其他窗口变化，请核对最新状态后重试。':`未能确认保存结果：${e.message}。请刷新核对，未自动重试。`;}
    finally{if(ctx().conn===conn){await load();toggling=false;if(problem)notice=problem;paint();if(focusId&&typeof document!=="undefined"&&(document.activeElement===document.body||document.activeElement?.id===focusId))document.getElementById(focusId)?.focus({preventScroll:true});}}
  }
  async function reload(){const {conn,sid}=ctx();if(!conn||!sid||reloading)return;const gen=generation;reloading=true;error="";paint();try{const result=await conn.client.post(`/desktop/sessions/${encodeURIComponent(sid)}/skills/reload`,{},{instance:conn.health?.instance_id});if(gen===generation&&ctx().conn===conn&&ctx().sid===sid){notice=result.note;await load();}}catch(e){if(gen===generation&&ctx().conn===conn)error=e.message;}finally{reloading=false;paint();}}
  function showType(next){if(!types.some(([id])=>id===next))return;type=next;query="";notice="";paint();if(type==="connectors")ui.connectors?.load();}
  function click(e){
    const n=e.target.closest("[data-capability-action]");if(!n)return false;const a=n.dataset.capabilityAction;
    if(a==="group"){const id=n.dataset.group;if(skillGroups(current()?.skills||[],query).some(g=>g.id===id)){const set=query.trim()?searchCollapsed:expandedGroups;set.has(id)?set.delete(id):set.add(id);paint();}}
    else if(a==="toggle-skill"){try{toggleSkills(JSON.parse(n.dataset.skillIds),n.dataset.enable==="true",n.id);}catch{}}
    else if(a==="refresh")load();
    else if(a==="view"&&["installed","discover"].includes(n.dataset.view)){view=n.dataset.view;query="";notice="";paint();}
    else if(a==="type")showType(n.dataset.type);
    else if(a==="discover"){view="discover";query="";paint();}
    else if(a==="rescan"){view="installed";query="";load();}
    else if(a==="configure-connector"&&ctx().writable&&CONNECTOR_PRESETS.some(p=>p.id===n.dataset.connectorPreset)){view="installed";query="";paint();ui.connectors?.openPreset?.(n.dataset.connectorPreset);}
    else if(a==="add-connector"){view="installed";query="";paint();ui.connectors?.open();}
    else if(a==="reload")reload();
    else if(a==="skill"){const s=current()?.skills.find(s=>s.id===n.dataset.skill);if(s&&s.enabled!==false)ui.draftTask?.({text:`请使用技能 ${s.id} 协助完成以下任务：\n\n`,skill:s.id,workflow:"chat"});}
    return true;
  }
  function input(e){if(e.target?.dataset&&Object.hasOwn(e.target.dataset,"capabilitySearch")){query=e.target.value;searchCollapsed.clear();paint();return true;}return false;}
  function reset(){expandedGroups.clear();searchCollapsed.clear();generation++;snapshot=null;snapshotConn=null;snapshotSid="";loading=false;error="";notice="";query="";view="installed";type="skills";reloading=false;toggling=false;}
  return {load,render,click,input,reset,showType};
}
