import {icon} from "./ui.mjs";
import {escapeHTML as h} from "./client.mjs";

export const projectContextDraftKey = id => `project-context:${id}`;
const pendingKey = body => `workspace-update:${body.group}:${body.key}`;
const object = value => !!value && typeof value === "object" && !Array.isArray(value);
export function validateWorkspace(data) {
  const s=data?.state,c=data?.catalogue;
  if(!s || !Number.isInteger(s.revision) || s.revision<0 || !object(s.projects) || !object(s.sessions) || !c || !Array.isArray(c.projects) || !object(c.sessions) || c.projects.some(p=>typeof p.project_id!=="string" || typeof p.workspace!=="string") || Object.values(c.sessions).some(p=>typeof p!=="string") || Object.values(s.projects).some(p=>typeof p?.context!=="string") || Object.values(s.sessions).some(s=>typeof s?.pinned!=="boolean" || typeof s?.archived!=="boolean"))throw new Error("项目工作台响应格式不正确，未覆盖已有数据。");
  for(const p of c.projects){
    if(p.folders!==undefined && (!Array.isArray(p.folders) || p.folders.some(f=>typeof f!=="string" || !f) || new Set(p.folders).size!==p.folders.length || typeof p.name!=="string" || !p.name.trim() || typeof p.primary!=="string" || p.workspace!==p.primary || (p.folders.length?!p.folders.includes(p.primary):p.primary!=="")))throw new Error("项目文件夹响应格式不正确，未覆盖已有数据。");
  }
  return data;
}
export function organizeWorkspaceSessions(sessions,metadata,{view="active"}={}) {
  const visible=sessions.filter(s=>view==="archived" ? metadata[s.session_id]?.archived===true : view==="all" ? true : !metadata[s.session_id]?.archived);
  return [...visible].sort((a,b)=>Number(!!metadata[b.session_id]?.pinned)-Number(!!metadata[a.session_id]?.pinned) || (b.updated_at || b.created_at || 0)-(a.updated_at || a.created_at || 0));
}
export function projectContextText(project,context) {return `项目上下文（由我明确加入本次任务草稿）\n工作目录：${project.workspace}\n\n${context}\n\n请结合我的具体任务核对以上说明；它不是额外的工具授权。`;}
export function mergeProjectContextDraft(existing,context,mode) {
  if(existing===context || !existing.trim() || mode==="replace")return context;
  if(mode==="append")return `${existing}\n\n${context}`;
  return null;
}
function validSource(source) {
  if(!object(source))return false;
  const keys=Object.keys(source).sort().join();
  return ["run_id","batch_id,item_id","candidate_id,contest_id"].includes(keys) && Object.values(source).every(v=>typeof v==="string" && !!v);
}
export function renderProjectArtifacts(state,{artifactCard=()=>""}={}) {
  if(!state)return "";
  let html=`<section class="project-artifacts"><div class="section-heading"><h3>项目成果</h3><button type="button" data-workspace-action="artifacts" data-project-id="${h(state.project_id)}" ${state.loading ? "disabled" : ""}>刷新成果</button></div>`;
  if(state.loading && !state.data)html+=`<p class="note">正在读取已登记成果…</p>`;
  if(state.error)html+=`<p class="form-error">${h(state.error)}${state.data ? "；以下保留上次结果，可能过期。" : "；不能判断项目是否已有成果。"}</p>`;
  if(state.data){
    html+=state.data.warning ? `<p class="note">${h(state.data.warning)}</p>` : "";
    if(state.data.truncated)html+=`<p class="note">成果列表已截断，当前不是完整项目历史。</p>`;
    if(!state.data.artifacts.length)html+=`<p class="note">尚无已登记的文件成果。</p>`;
    for(const row of state.data.artifacts){const at=typeof row.created_at==="number" && row.created_at>0 ? new Date(row.created_at*1000).toLocaleString("zh-CN") : "时间未记录";html+=`<article>${artifactCard(row.artifact,{session_id:row.session_id,...row.source})}<p class="note">来源会话 ${h(row.session_id)} · ${h(at)}</p><button type="button" class="quiet" data-workspace-action="source" data-workspace-session="${h(row.session_id)}">查看来源会话</button></article>`;}
  }
  return `${html}<p class="note">只显示服务登记的交付成果，不扫描项目中的任意文件。跨会话成果不代表已应用到当前任务。</p></section>`;
}
export function createWorkspaceController(ui) {
  let data=null,error="",loading=false,pending=null,sending=false,generation=0,restored=false,restoreError="",projectError="";
  let deleting=false,confirming=false,deleteNote="",archiveRows=[],archiveProject="";
  const drafts=new Map(),artifacts=new Map(),placements=new Map();
  const context=()=>ui.context(),paint=()=>ui.render();
  const project=id=>data?.catalogue.projects.find(p=>p.project_id===id);
  const sessionMeta=sid=>data?.state.sessions[sid] || {pinned:false,archived:false};
  const writable=()=>!!(context().writable && context().conn && !error && !restoreError && data);
  function restorePending(){
    if(restored || !data)return;restored=true;
    try{
      const keys=[...data.catalogue.projects.map(p=>({group:"projects",key:p.project_id})),...Object.keys(data.catalogue.sessions).map(key=>({group:"sessions",key}))];
      const creating=context().conn?.localState?.getDraft("workspace-new-project");if(creating)keys.push({group:"projects",key:creating});
      const stored=keys.map(b=>({key:pendingKey(b),raw:context().conn?.localState?.getDraft(pendingKey(b))})).find(v=>v.raw);if(!stored)return;
      const value=JSON.parse(stored.raw),b=value.body;
      if(!b || pendingKey(b)!==stored.key || !Number.isInteger(b.revision) || b.revision<0 || !["projects","sessions"].includes(b.group) || typeof b.key!=="string" || !object(b.value) || b.group==="projects" && (typeof b.value.context!=="string" || b.value.folders!==undefined && (!Array.isArray(b.value.folders) || typeof b.value.name!=="string" || typeof b.value.primary!=="string")) || b.group==="sessions" && (Object.keys(b.value).sort().join()!=="archived,pinned" || typeof b.value.pinned!=="boolean" || typeof b.value.archived!=="boolean"))throw new Error();
      pending={body:b,status:"unknown",message:"发现尚待核对的项目更新；不会自动重发。"};
    }catch{restoreError="已保存的项目更新无法读取，原数据未覆盖。";}
  }
  function apply(next){validateWorkspace(next);if(!data || next.state.revision>=data.state.revision)data=next;}
  async function load(conn=context().conn){
    if(!conn || conn!==context().conn || loading)return;
    const epoch=generation;loading=true;restorePending();paint();
    try{const next=await conn.client.get("/desktop/workspace",conn.controller.signal);if(conn!==context().conn || epoch!==generation)return;apply(next);error="";restorePending();}
    catch(e){if(conn===context().conn && epoch===generation)error=e.status===404 ? "当前服务不支持项目工作区，请更新应用服务。" : e.message;}
    finally{if(conn===context().conn && epoch===generation){loading=false;paint();}}
  }
  function draft(id){
    if(!drafts.has(id)){
      const saved=data?.state.projects[id]?.context || "";let value={text:saved,base:saved},failure="";
      try{const raw=context().conn?.localState?.getDraft(projectContextDraftKey(id));if(raw){value=JSON.parse(raw);if(typeof value.text!=="string" || typeof value.base!=="string")throw new Error();}}
      catch{value={text:saved,base:saved};failure="已保存的项目说明草稿无法读取，原数据未覆盖。";}
      drafts.set(id,{...value,error:failure});
    }
    const d=drafts.get(id),saved=data?.state.projects[id]?.context || "";
    if(!d.error && d.text===d.base && d.base!==saved){d.text=saved;d.base=saved;}
    return d;
  }
  function persistDraft(id,d){context().conn?.localState?.setDraft(projectContextDraftKey(id),JSON.stringify({text:d.text,base:d.base}));}
  async function dispatch(){
    const conn=context().conn,epoch=generation,request=pending;if(!conn || !request || sending || !writable())return;
    const known=request.body.group==="projects" ? !!project(request.body.key)||Array.isArray(request.body.value.folders) : Object.hasOwn(data.catalogue.sessions,request.body.key);
    if(!known){request.message="此项目或会话已不在当前工作台，不能重试更新。";paint();return;}
    sending=true;request.status="sending";paint();
    try{
      conn.localState?.setDraft(pendingKey(request.body),JSON.stringify({body:request.body}));await conn.localState?.flush();
      const next=await conn.client.post("/desktop/workspace",request.body,{signal:conn.controller.signal});
      if(conn!==context().conn || epoch!==generation)return;
      apply(next);error="";
      if(request.body.group==="projects"){
        const d=drafts.get(request.body.key);if(d){d.base=request.body.value.context;persistDraft(request.body.key,d);}
      }
      if(request.body.group==="projects" && conn.localState?.getDraft("workspace-new-project")===request.body.key){conn.localState.deleteDraft("workspace-new-project");ui.openProject?.(request.body.key);}
      pending=null;conn.localState?.deleteDraft(pendingKey(request.body));restored=false;restorePending();await conn.localState?.flush();
    }catch(e){
      if(conn===context().conn && epoch===generation){pending=request;request.status=e.status===409 ? "conflict" : "unknown";request.message=e.status===409 ? "项目状态已有新修改，未覆盖。已重新读取；请对照本地草稿与当前保存值。" : `保存结果尚待核对：${e.message}；原请求与原文保留。`;await load(conn);}
    }finally{if(conn===context().conn && epoch===generation){sending=false;paint();}}
  }
  function update(group,key,value){if(!writable() || pending || sending || deleting)return;pending={body:{revision:data.state.revision,group,key,value},status:"ready",message:""};dispatch();}
  function saveContext(id,replace=false){
    const d=draft(id),saved=data?.state.projects[id]?.context || "";if(!project(id) || d.error)return;
    if(sending || pending && !(replace && pending.body.group==="projects" && pending.body.key===id && pending.status==="conflict"))return;
    if(d.text.length>20000){d.validationError="项目说明最多 20000 字符；原文保留，请缩短后保存。";paint();return;}
    if(d.base!==saved && d.text!==saved && !replace){d.conflict=true;paint();return;}
    if(replace && pending?.body.group==="projects" && pending.body.key===id && pending.status==="conflict"){context().conn?.localState?.deleteDraft(pendingKey(pending.body));pending=null;restored=false;restorePending();}
    if(replace)d.base=saved;persistDraft(id,d);update("projects",id,{context:d.text});
  }
  function definition(id){
    const p=project(id);return p?{context:data.state.projects[id]?.context||"",name:p.name||p.workspace.split("/").pop()||"项目",folders:p.folders||[p.workspace],primary:p.primary??p.workspace}:null;
  }
  function configure(id,patch){const value=definition(id);if(value)update("projects",id,{...value,...patch});}
  async function addFolders(id,paths){
    if(!writable()||pending)return;
    const conn=context().conn,epoch=generation;
    try{
      if(!paths){const result=await ui.chooseFolders?.();if(result?.cancelled)return;paths=result?.folders;}
      if(conn!==context().conn||epoch!==generation||!paths?.length)return;
      const value=definition(id);if(!value)return;
      const folders=[...new Set([...value.folders,...paths])];
      if(folders.length>20)throw new Error("每个项目最多关联 20 个文件夹。");
      projectError="";configure(id,{folders,primary:value.primary||folders[0]});
    }catch(e){projectError=e.message;paint();}
  }
  function renderFolders(id){
    const p=definition(id);if(!p)return "";
    return `<section class="project-folders"><form data-project-name="${h(id)}" class="actions"><label>项目名称<input id="project-name-${h(id)}" name="name" value="${h(p.name)}" maxlength="100" required></label><button type="submit" ${!writable()||pending?"disabled":""}>保存名称</button></form><div class="section-heading"><h3>文件夹</h3>${ui.chooseFolders?`<button type="button" data-workspace-action="add-folders" data-project-id="${h(id)}" ${!writable()||pending?"disabled":""}>${icon("plus")}添加文件夹</button>`:""}</div>${p.folders.length?p.folders.map(path=>`<div class="project-folder-row">${icon("folder")}<span class="project-folder-path" title="${h(path)}">${h(path.split("/").filter(Boolean).pop()||path)}</span>${path===p.primary?'<span class="note">主文件夹</span>':`<button type="button" class="quiet" data-workspace-action="primary-folder" data-project-id="${h(id)}" data-folder="${h(path)}" ${!writable()||pending?"disabled":""}>设为主文件夹</button>`}<button type="button" class="quiet" data-workspace-action="remove-folder" data-project-id="${h(id)}" data-folder="${h(path)}" aria-label="移除 ${h(path)}" title="从项目移除，不删除文件" ${!writable()||pending?"disabled":""}>${icon("close")}</button></div>`).join(""):'<p class="note">尚未关联文件夹。可以直接开始对话，稍后再添加。</p>'}${!ui.chooseFolders?`<form data-project-folder="${h(id)}" class="actions"><input name="folder" placeholder="文件夹的完整路径" required><button type="submit">添加文件夹</button></form>`:""}<p class="note">新对话从主文件夹开始，可使用所有关联文件夹。更改仅用于新对话；已有对话保留原来的工作范围。</p>${projectError?`<p class="form-error">${h(projectError)}</p>`:""}</section>`;
  }
  async function loadArtifacts(id){
    const conn=context().conn,epoch=generation;if(!conn || !project(id))return;
    let view=artifacts.get(id);if(view?.loading)return;if(!view){view={project_id:id};artifacts.set(id,view);}view.loading=true;paint();
    try{
      const result=await conn.client.get(`/desktop/projects/${encodeURIComponent(id)}/artifacts`,conn.controller.signal);
      if(conn!==context().conn || epoch!==generation)return;
      if(result.project_id!==id || !Array.isArray(result.artifacts) || result.artifacts.some(row=>!row.artifact?.id || !validSource(row.source) || data.catalogue.sessions[row.session_id]!==id))throw new Error("成果来源不属于此项目或格式不完整。");
      view.data=result;view.error="";
    }catch(e){if(conn===context().conn && epoch===generation)view.error=e.message;}
    finally{if(conn===context().conn && epoch===generation){view.loading=false;paint();}}
  }
  function place(id,mode){
    const p=project(id),d=draft(id),saved=data?.state.projects[id]?.context || "",c=context();
    if(c.active===false || !p || !saved || d.text!==saved || d.error || error || data.catalogue.sessions[c.sid]!==id){placements.set(id,{error:"先保存说明，再选择同项目会话；内容尚未放入任何任务。"});paint();return;}
    const text=projectContextText(p,saved),merged=mergeProjectContextDraft(ui.getComposer(),text,mode);
    if(merged===null)placements.set(id,{conflict:true,text,context:saved,sid:c.sid});
    else{const old=placements.get(id);if(mode && old && (old.context!==saved || old.sid!==c.sid)){placements.set(id,{error:"项目说明或目标会话已变化，请重新检查放入操作。"});paint();return;}ui.putComposer(merged);placements.set(id,{message:"项目说明已放入任务草稿，尚未发送。"});}
    paint();
  }
  async function deleteArchived(sid){
    if(!writable()||pending||sending||deleting||confirming)return;
    const conn=context().conn,epoch=generation;
    const targets=(sid?archiveRows.filter(s=>s.id===sid):archiveRows).map(s=>({...s}));
    if(!targets.length)return;
    const body={revision:data.state.revision,session_ids:targets.map(s=>s.id)};
    confirming=true;
    let confirmed=false;
    try{confirmed=await ui.confirmDelete?.({count:targets.length,title:targets[0].title,all:!sid});}
    finally{if(epoch===generation)confirming=false;}
    if(!confirmed||conn!==context().conn||epoch!==generation||!writable()||pending||sending)return;
    deleting=true;deleteNote="";paint();
    try{
      const result=await conn.client.post("/desktop/archived-sessions/delete",body,{signal:conn.controller.signal});
      if(conn!==context().conn||epoch!==generation)return;
      if(!Array.isArray(result.deleted)||result.deleted.some(id=>!body.session_ids.includes(id)))throw new Error("删除结果无法确认，请刷新核对。");
      apply(result);artifacts.clear();
      await ui.onDeleted?.(result.deleted,conn);
      if(conn!==context().conn||epoch!==generation)return;
      deleteNote=result.incomplete?`已删除 ${result.deleted.length} 个对话；其余未完成，请刷新后重试。`:`已删除 ${result.deleted.length} 个对话。`;
    }catch(e){
      if(conn===context().conn&&epoch===generation){deleteNote=`${e.message} 未自动重试，请核对剩余对话。`;await load(conn);await ui.refreshSessions?.(conn);}
    }finally{if(conn===context().conn&&epoch===generation){deleting=false;paint();}}
  }
  function renderStatus(){
    return `${error || restoreError ? `<p class="form-error">${h(error || restoreError)}；未覆盖原数据。</p>` : ""}${pending ? `<div class="callout warning"><p>${h(pending.message || "正在保存应用项目数据…")}</p><p class="note">${h(pending.body.group)} · ${h(pending.body.key)} · 原版本 ${h(pending.body.revision)}</p><button type="button" data-workspace-action="retry" ${sending || pending.status==="conflict" ? "disabled" : ""}>使用原请求重试</button><button type="button" data-workspace-action="forget" ${sending ? "disabled" : ""}>已核对，清除此更新回执</button></div>` : ""}`;
  }
  return {load,loadArtifacts,dispatch,renderStatus,renderFolders,
    renderArchived(sessions,{query="",titleOf=s=>s.title||s.session_id,date=()=>""}={}){
      const archived=organizeWorkspaceSessions(sessions,data?.state.sessions||{},{view:"archived"});
      archiveRows=archived.filter(s=>Object.hasOwn(data?.catalogue.sessions||{},s.session_id)).map(s=>({id:s.session_id,title:titleOf(s)}));
      const deleteDisabled=!writable()||pending||sending||deleting;
      const projectId=s=>project(data?.catalogue.sessions[s.session_id])?.project_id || "unassigned";
      const projectName=id=>{const p=project(id);return p?.name || p?.workspace || "未分配项目";};
      const choices=(data?.catalogue.projects || []).map(p=>p.project_id);
      if(archived.some(s=>projectId(s)==="unassigned"))choices.push("unassigned");
      if(archiveProject && !choices.includes(archiveProject))archiveProject="";
      const needle=query.trim().toLocaleLowerCase();
      const rows=archived.filter(s=>(!archiveProject || projectId(s)===archiveProject) && (!needle || `${titleOf(s)} ${projectName(projectId(s))}`.toLocaleLowerCase().includes(needle)));
      const groups=new Map();
      for(const s of rows){const id=projectId(s);if(!groups.has(id))groups.set(id,[]);groups.get(id).push(s);}
      const ready=!!data&&!error&&!restoreError,filtered=!!(needle || archiveProject);
      return `<section class="archive-page"><header class="section-heading"><h2>已归档的对话</h2>${archived.length?`<button type="button" class="archive-delete-all" data-workspace-action="delete-all-archived" title="删除当前工作台全部 ${archived.length} 个归档对话（包括筛选之外的对话）" ${deleteDisabled?"disabled":""}>${icon("trash")}全部删除</button>`:""}</header>${deleteNote?`<p class="note" role="status">${h(deleteNote)}</p>`:""}${deleting?'<p class="note" role="status">正在删除…</p>':""}
        <div class="archive-toolbar"><label class="archive-search">${icon("search")}<input id="archive-search" type="search" value="${h(query)}" placeholder="搜索已归档的对话" aria-label="搜索已归档的对话"></label><label class="archive-project-filter">${icon("folder")}<select id="archive-project" aria-label="筛选归档项目"><option value="">所有项目</option>${choices.map(id=>`<option value="${h(id)}" ${id===archiveProject?"selected":""}>${h(projectName(id))}</option>`).join("")}</select></label></div>
        ${renderStatus()}${!data?`<p class="note" role="status">${loading?"正在读取归档对话…":error?"暂时无法读取归档对话。":"正在等待连接…"}</p>`:""}
        ${data?`${filtered?`<p class="archive-count note" role="status">找到 ${rows.length} 个对话</p>`:""}<div class="archive-groups">${[...groups].map(([id,items])=>`<section class="archive-group" data-archive-project="${h(id)}"><div class="archive-group-heading"><h3>${icon("folder")}${h(projectName(id))}</h3><span>${items.length} 个对话</span></div><div class="archive-list">${items.map(s=>`<article class="archive-row"><button type="button" class="archive-open" data-workspace-action="source" data-workspace-session="${h(s.session_id)}"><strong>${h(titleOf(s))}</strong><span>${s.updated_at||s.created_at?h(date(s.updated_at||s.created_at)):""}</span></button><div class="archive-row-actions"><button type="button" class="archive-delete" data-workspace-action="delete-archived" data-workspace-session="${h(s.session_id)}" title="删除对话" aria-label="删除对话：${h(titleOf(s))}" ${deleteDisabled?"disabled":""}>${icon("trash")}</button><button type="button" class="archive-restore" data-workspace-action="archive" data-workspace-session="${h(s.session_id)}" aria-label="取消归档：${h(titleOf(s))}" ${deleteDisabled?"disabled":""}>取消归档</button></div></article>`).join("")}</div></section>`).join("")}</div>${ready&&!rows.length?`<div class="archive-empty">${icon(filtered?"search":"archive")}<h3>${filtered?"没有找到匹配的对话":"暂无归档对话"}</h3><p class="note">${filtered?"试试其他关键词或项目。":"归档后的对话会显示在这里。"}</p></div>`:""}`:""}</section>`;
    },
    renderCreate(){return `<details class="project-create" data-key="project-create"><summary>${icon("plus")}新建项目</summary><form data-project-new class="actions"><input id="new-project-name" name="name" placeholder="项目名称" maxlength="100" required><button type="submit" ${!writable()||pending?"disabled":""}>创建项目</button></form><p class="note">文件夹可以稍后添加。</p></details>`;},
    reset(){archiveProject="";deleting=false;confirming=false;deleteNote="";archiveRows=[];data=null;error="";projectError="";loading=false;pending=null;sending=false;generation++;restored=false;restoreError="";drafts.clear();artifacts.clear();placements.clear();},
    projects:()=>data?.catalogue.projects || [],projectForSession:sid=>data?.catalogue.sessions[sid] || "",projectForWorkspace:workspace=>data?.catalogue.projects.find(p=>p.workspace===workspace)?.project_id || "",sessionMeta,
    filterSessions:(sessions,options)=>organizeWorkspaceSessions(sessions,data?.state.sessions || {},options),
    renderSessionActions(sid,{compact=false}={}){if(!data || !Object.hasOwn(data.catalogue.sessions,sid))return "";const m=sessionMeta(sid);if(compact)return `<span class="session-row-actions"><button type="button" data-workspace-action="pin" data-workspace-session="${h(sid)}" title="${m.pinned?"取消置顶":"置顶"}" aria-label="${m.pinned?"取消置顶":"置顶"}" ${!writable()||pending?"disabled":""}>${icon("pin")}</button><button type="button" data-workspace-action="archive" data-workspace-session="${h(sid)}" title="${m.archived?"恢复到列表":"归档"}" aria-label="${m.archived?"恢复到列表":"归档"}" ${!writable()||pending?"disabled":""}>${icon("archive")}</button></span>`;return `<div class="actions"><button type="button" data-workspace-action="pin" data-workspace-session="${h(sid)}" ${!writable() || pending ? "disabled" : ""}>${icon("pin")}${m.pinned ? "取消置顶" : "置顶会话"}</button><button type="button" data-workspace-action="archive" title="归档仅整理列表，不删除历史或停止任务" data-workspace-session="${h(sid)}" ${!writable() || pending ? "disabled" : ""}>${icon("archive")}${m.archived ? "恢复到工作台" : "归档会话"}</button><span class="note">归档仅整理列表，不删除历史、停止任务或移除成果。</span></div>`;},
    renderProject(id){
      const p=project(id);if(!p)return `<p class="note">${loading ? "正在读取项目…" : error ? h(error) : "请选择已登记项目。"}</p>`;
      const d=draft(id),saved=data.state.projects[id]?.context || "",conflict=d.base!==saved && d.text!==saved,placement=placements.get(id);
      let html=`${renderFolders(id)}<section class="project-context"><p class="note">项目说明属于羽案应用数据。保存不会自动注入模型；仅在你明确放入任务草稿并提交后，模型才会收到。</p>${error || restoreError ? `<p class="form-error">${h(error || restoreError)}；显示的是上次读取的数据。</p>` : ""}<form data-project-context="${h(id)}"><label>项目说明<textarea id="project-context-${h(id)}" data-project-context-draft="${h(id)}" rows="4" maxlength="20000" ${d.error ? "readonly" : ""}>${h(d.text)}</textarea></label>${d.error || d.validationError ? `<p class="form-error">${h(d.error || d.validationError)}</p>` : ""}<div class="actions"><button type="submit" ${!writable() || pending || d.error ? "disabled" : ""}>保存项目说明</button><button type="button" data-workspace-action="place" data-project-id="${h(id)}" ${!saved || saved!==d.text || !!error || d.error ? "disabled" : ""}>明确放入当前任务草稿</button></div></form>`;
      if(conflict)html+=`<div class="callout warning"><p>服务中的项目说明已有变化，本地编辑尚未覆盖它。</p><details open><summary>当前保存值</summary><pre>${h(saved)}</pre></details><button type="button" data-workspace-action="replace-context" data-project-id="${h(id)}" ${sending ? "disabled" : ""}>用本地编辑替换当前保存值</button></div>`;
      if(placement?.conflict)html+=`<div class="callout warning"><p>当前任务已有不同草稿。选择追加或明确替换；原草稿尚未改动。</p><button type="button" data-workspace-action="append" data-project-id="${h(id)}">追加到现有草稿</button><button type="button" data-workspace-action="replace-draft" data-project-id="${h(id)}">替换当前草稿</button></div>`;
      if(placement?.error || placement?.message)html+=`<p class="note">${h(placement.error || placement.message)}</p>`;
      if(pending)html+=`<div class="callout warning"><p>${h(pending.message || "正在保存项目组织数据…")}</p><p class="note">更新 ${h(pending.body.group)} · ${h(pending.body.key)} · 原版本 ${h(pending.body.revision)}</p><button type="button" data-workspace-action="retry" ${sending || pending.status==="conflict" ? "disabled" : ""}>使用原请求重试</button><button type="button" data-workspace-action="forget" ${sending ? "disabled" : ""}>已核对，清除此更新回执</button></div>`;
      html+=`<button type="button" class="quiet" data-workspace-action="reload">重新读取项目状态</button>${artifacts.has(id) ? renderProjectArtifacts(artifacts.get(id),{artifactCard:ui.artifactCard}) : `<button type="button" data-workspace-action="artifacts" data-project-id="${h(id)}">查看项目成果</button>`}</section>`;
      return html;
    },
    input(event){if(event.target.id==="archive-project"){if(archiveProject!==event.target.value){archiveProject=event.target.value;paint();}return true;}const id=event.target.dataset.projectContextDraft;if(!id || !project(id))return false;const d=draft(id);if(d.error)return true;d.text=event.target.value;d.validationError="";persistDraft(id,d);placements.delete(id);paint();return true;},
    onSubmit(event){
      const form=event.target;
      if(form.dataset.projectNew!==undefined){
        event.preventDefault();if(!writable()||pending)return true;
        const name=form.elements.name.value.trim();if(!name)return true;
        const id=crypto.randomUUID().replaceAll("-","").slice(0,24);
        context().conn.localState?.setDraft("workspace-new-project",id);
        update("projects",id,{context:"",name,folders:[],primary:""});return true;
      }
      if(form.dataset.projectName){event.preventDefault();const name=form.elements.name.value.trim();if(name)configure(form.dataset.projectName,{name});return true;}
      if(form.dataset.projectFolder){event.preventDefault();addFolders(form.dataset.projectFolder,[form.elements.folder.value.trim()]);return true;}
      const id=event.target.dataset.projectContext;if(!id)return false;event.preventDefault();saveContext(id);return true;},
    click(event){const node=event.target.closest("[data-workspace-action]");if(!node)return false;const action=node.dataset.workspaceAction,id=node.dataset.projectId,sid=node.dataset.workspaceSession;
      if(action==="delete-archived")deleteArchived(sid);
      if(action==="delete-all-archived")deleteArchived();
      if(action==="add-folders")addFolders(id);
      if(action==="primary-folder")configure(id,{primary:node.dataset.folder});
      if(action==="remove-folder"){
        const value=definition(id);if(value){const folders=value.folders.filter(p=>p!==node.dataset.folder);configure(id,{folders,primary:folders.includes(value.primary)?value.primary:folders[0]||""});}
      }
      if(action==="reload")load();if(action==="retry" && pending?.status!=="conflict")dispatch();if(action==="forget" && pending && !sending){if(context().conn?.localState?.getDraft("workspace-new-project")===pending.body.key)context().conn.localState.deleteDraft("workspace-new-project");context().conn?.localState?.deleteDraft(pendingKey(pending.body));pending=null;restored=false;restorePending();paint();}
      if(["pin","archive"].includes(action) && data?.catalogue.sessions[sid]){const prior=sessionMeta(sid);update("sessions",sid,{...prior,[action==="pin" ? "pinned" : "archived"]:!prior[action==="pin" ? "pinned" : "archived"]});}
      if(action==="replace-context")saveContext(id,true);if(action==="artifacts")loadArtifacts(id);if(action==="source" && data?.catalogue.sessions[sid])ui.openSession(sid);
      if(["place","append","replace-draft"].includes(action))place(id,action==="append" ? "append" : action==="replace-draft" ? "replace" : undefined);return true;},
  };
}
