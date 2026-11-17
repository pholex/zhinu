import {composerControls,conversationMessage,composerEnterAction,historyTimeline,liveTimeline,timelineHTML,activityHTML,showSubmissionReceipt} from "./conversation_ui.mjs";
import {terminalRows,mergeRecent,terminalLabel,isContinued,takeover} from "./terminal_sessions.mjs";
import {createComposerAdditions} from "./composer_additions.mjs";
import {executionModePresentation,executionPermissionChoices} from "./execution_modes.mjs";
import {validViewID,viewState,newConversationInput,scopeViewDrafts,claimLegacyDrafts,syncViewReceipts,actionBelongsToView,workStatus} from "./view_state.mjs";
import {createViewHistory,viewHistoryKey} from "./view_history.mjs";
import {createModelPicker,defaultComposerModel} from "./model_picker.mjs";
import {icon,hydrateIcons,welcomeHTML,taskStarters,fitComposer,positionMenu} from "./ui.mjs";
import {createShortcutsController,shortcutAction,DEFAULT_SHORTCUTS} from "./shortcuts.mjs";
import {createDataController} from "./data.mjs";
import {orderedSessions,sessionUnread,readMetadata,hasRecentActivity} from "./organization.mjs";
import {createSettingsController,DEFAULT_UI} from "./settings.mjs";
import {createContentController} from "./content.mjs";
import {createCapabilitiesController} from "./capabilities.mjs";
import {createPanes} from "./panes.mjs";
import {createSideConversation,sideConversationEntries} from "./sidechat.mjs";
import {createProvidersController} from "./providers.mjs";
import {renderLocalConflicts,resolveLocalConflict} from "./state_conflicts.mjs";
import {createConnectionController} from "./connections.mjs";
import {createAutomationController} from "./automations.mjs";
import {createWorkspaceController} from "./workspace.mjs";
import {createWeaveControlController,renderWeaveApprovalIdentity,weaveActionTarget} from "./weave_controls.mjs";
import {createFleetCommandController} from "./fleet_commands.mjs";
import {createFleetController,renderFleetApprovalIdentity,fleetActionTarget} from "./fleets.mjs";
import {createContestController} from "./contests.mjs";
import {WORKFLOW_CHOICES, renderWeaves, renderWeaveForm, renderArtifactInspection} from "./workflows.mjs";
import {sessionActionAllowed, ReadClient, Outbox, LocalState, reconcileAcceptedDraft, stateOf, projectName, visibleSessions, eventWindow, mergeEvents, conversation, historyConversation, renderText, escapeHTML as h} from "./client.mjs";

const $ = id => document.getElementById(id);
hydrateIcons();
const modelPicker=createModelPicker({root:$("model-picker"),select:$("composer-model")});
const permissionPicker=createModelPicker({root:$("permission-picker"),select:$("composer-mode"),label:"执行权限",symbol:"shield",heading:"任务如何执行",defaultLabel:"使用默认设置",searchable:false,hint:"选择新会话的执行权限",compact:true});
const workflowPicker=createModelPicker({root:$("workflow-picker"),select:$("work-mode"),label:"工作方式",symbol:"capabilities",heading:"工作方式",searchable:false,hint:$("work-mode").parentElement.title,compact:true,activateOnReselect:true});
const native = window.__XIAOYU_DESKTOP__;
delete window.__XIAOYU_DESKTOP__;
const desktop = native?.origin === location.origin && typeof native?.token === "string";
const viewID = desktop && validViewID(native.view_id) ? native.view_id.toLowerCase() : "";
const ownsAction = action => actionBelongsToView(action,viewID);
const nativeTitlebar = desktop && native.titlebar_sidebar === true;
document.documentElement.classList.toggle("native-titlebar", nativeTitlebar);
const nativePanelTitlebar = desktop && native.titlebar_panel === true;
document.documentElement.classList.toggle("native-panel-titlebar", nativePanelTitlebar);
const fixture = desktop ? native.fixture : new URLSearchParams(location.search).get("fixture") === "1";
let connection = null, sessions = [], selected = "", project = "", filter = "all", search = "", source = "workbench";
const messageCopies=new Map();
const views = new Map(), drafts = new Map(), titles = new Map(), archives = new Map();
let projectSelected = "";
let archiveQuery = "";
let sidebarVisibilityOverride = null;
let cliList = {items: [], state: "initial", cursor: null, total: 0}, cliSelected = "", cliListMode = false;
const cliViews = new Map(), weaves = new Map();
const weaveForms = new Map(), artifactViews = new Map(), artifactRefs = new Map();
let eventController = null, feedback = null;
const pageControllers = new Map();
let panes = null;
const sideConversations=[];
let startingDraft = "";
let pendingWorkflow="";
let collaborationPage="";
let pendingModel="";
let inputWorkspace="";
let inputProject="";
const memoryInputs=new Map();
function newInput(workspace=inputWorkspace,text,pid=inputProject){
  const key=pid?`project:${pid}`:workspace;
  if(connection?.localState){
    const state=connection.localState;
    // Only a migrated one-folder project may adopt the old path-scoped draft.
    const p=workspaceController.projects().find(p=>p.project_id===pid);
    if(pid && p?.workspace===workspace && workspaceController.projectForWorkspace(workspace)===pid && !state.getDraft(`project-input-migrated:${pid}`)){
      const legacy=newConversationInput(state,workspace);
      if(legacy && !newConversationInput(state,key))newConversationInput(state,key,legacy);
      newConversationInput(state,workspace,"");state.setDraft(`project-input-migrated:${pid}`,"1");
    }
    return newConversationInput(state,key,text);
  }
  if(text!==undefined){if(text)memoryInputs.set(key,text);else memoryInputs.delete(key);}
  return memoryInputs.get(key)||"";
}
function inputProjectOptions(){return inputProject?{project_id:inputProject,...(inputWorkspace?{workspace:inputWorkspace}:{})}:{workspace:inputWorkspace||connection?.health?.root};}
const modelSaves=new Set();
let historyTarget=null;
const notificationStates=new Map();
let automationNoticeIds=null;const automationSessions=new Set();
let newTask = !!native?.force_new || !!viewID && !native?.session_id;
let navigationHistory = null;
let navigationUnavailable = "";
const badge = s => `<span class="badge ${stateOf(s).tone}">${h(stateOf(s).label)}</span>`;
const json = value => h(JSON.stringify(value, null, 2));
const date = value => value ? new Date(value * 1000).toLocaleString("zh-CN") : "—";
const sidPath = sid => `/session/${encodeURIComponent(sid)}`;
const session = () => sessions.find(s => s.session_id === selected);
function preparationDraftIdentity(sid){
  const current=sessions.find(s=>s.session_id===sid);
  if(!current || hasRecentActivity(current))return sid;
  const store=connection?.localState;
  if(!store)return sid;
  const identity=current.project_id||workspaceController.projectForSession(sid)||current.workspace;
  const raw=store.getDraft("preparation-folders"),ids=raw?JSON.parse(raw):{};
  if(!Object.hasOwn(ids,identity)){
    Object.defineProperty(ids,identity,{value:crypto.randomUUID(),enumerable:true});
    store.setDraft("preparation-folders",JSON.stringify(ids));
  }
  return `new-${ids[identity]}`;
}
const scopeFor = (sid, kind, id = "") => `${sid}:${kind}:${id}`;
const pendingFor = sid => Object.entries(connection?.outbox?.items || {}).filter(([scope, a]) => !scope.startsWith("side:") && a.sid === sid);
const writable = () => connection?.healthy && connection?.health?.desktop_protocol === 1 && connection?.health?.persist && connection?.outbox;
const contests = createContestController({
  draftIdentity:preparationDraftIdentity,
  context: () => ({conn:connection,sid:selected,active:source === "workbench",session:session(),writable:!!writable(),pending:pendingFor(selected).map(([,a]) => a)}),
  render:renderThread, submit, artifactCard,
  getComposer:() => $("message").value,
  putComposer:text => { choose(selected); $("message").value = text; saveDraft(); renderComposer(); notice("后续任务已放入输入框，尚未发送。请检查后再提交。"); $("message").focus(); },
});
const fleets = createFleetController({
  context:() => ({conn:connection,sid:selected,active:source === "workbench",session:session(),writable:!!writable(),pending:pendingFor(selected).map(([,a]) => a)}),
  submit,
  render:() => { renderThread(); renderComposer(); },
});
const fleetCommands = createFleetCommandController({
  draftIdentity:preparationDraftIdentity,
  context:() => ({conn:connection,sid:selected,active:source === "workbench",session:session(),writable:!!writable(),pending:pendingFor(selected).map(([,a]) => a)}),
  submit, render:() => { renderThread(); renderComposer(); },
});
const weaveControls = createWeaveControlController({
  context:() => ({conn:connection,sid:selected,active:source === "workbench",writable:!!writable(),pending:pendingFor(selected).map(([,a]) => a)}),
  state:sid => weaves.get(sid), submit, render:renderThread,
});
const workspaceController = createWorkspaceController({
  context:() => ({conn:connection,sid:selected,projectId:projectSelected,active:source !== "cli",writable:desktop && !!writable()}),
  render, artifactCard, openProject,
  confirmDelete:({count,title,all})=>new Promise(resolve=>{
    const dialog=$("archive-delete-dialog");
    $("archive-delete-title").textContent=all?"删除全部归档对话？":"删除对话？";
    $("archive-delete-description").textContent=all?`将删除当前工作台中的全部 ${count} 个归档对话。`:`将删除“${title}”。`;
    dialog.querySelector('[value="delete"]').textContent=all?`删除 ${count} 个对话`:"删除";
    dialog.returnValue="cancel";
    dialog.addEventListener("close",()=>resolve(dialog.returnValue==="delete"),{once:true});
    dialog.showModal();
  }),
  onDeleted:async(ids,conn)=>{
    if(connection!==conn)return;
    const removed=new Set(ids);
    sessions=sessions.filter(s=>!removed.has(s.session_id));
    for(const sid of ids){drafts.delete(sid);conn.localState?.deleteDraft(sid);views.delete(sid);archives.delete(sid);weaves.delete(sid);}
    if(removed.has(selected)){eventController?.abort();selected="";$("message").value="";rememberNavigation(true);}
    render();await conn.localState?.flush();
  },
  refreshSessions:conn=>refresh(conn),
  chooseFolders:window.webkit?.messageHandlers?.workspace?()=>window.webkit.messageHandlers.workspace.postMessage({operation:"choose_project_folders"}):null,
  getComposer:() => $("message").value,
  putComposer:text => { $("message").value = text; saveDraft(); if (source !== "workbench" || collaborationPage) choose(selected); notice("项目说明已放入任务草稿，尚未发送。请核对后再提交。"); $("message").focus(); },
  openSession:sid => { if (sessions.some(s => s.session_id === sid)) choose(sid); else notice("来源会话当前不可用，请刷新项目状态。"); },
});
const automations = createAutomationController({
  onSnapshot:notifyAutomation,
  context:() => ({conn:connection,writable:desktop && !!writable(),projects:workspaceController.projects()}),
  render:() => { if (source === "automations") renderThread(); },
  openSession:sid => { if (sessions.some(s => s.session_id === sid)) choose(sid); else notice("关联会话当前不可用，请刷新工作台后核对；不会重新触发自动化。"); },
});
let connectionTarget = "";
const connections = createConnectionController({
  context:() => ({conn:connection,sid:connectionTarget,sessions,writable:desktop && !!writable()}),
  render:() => { if (source === "connections" || source === "capabilities") renderThread(); },
  selectSession:sid => { connectionTarget = sid; renderThread(); },
  openGuide:openContentPane,
  focusEditor:() => { $("thread-scroll").scrollTop=0; $("connection-name")?.focus({preventScroll:true}); },
  openAuthorization:desktop && window.webkit?.messageHandlers?.openConnectionAuthorization ? ids => window.webkit.messageHandlers.openConnectionAuthorization.postMessage(ids) : null,
});
function viewSnapshot() {
  return {source,selected,project,projectSelected,filter,cliSelected,newTask,connectionTarget,collaborationPage,
    inputProject,workspace:inputWorkspace,workflow:$("work-mode").value,model:$("composer-model").value,mode:$("composer-mode").value};
}
function normalizeWorkflow(mode){return ["weave","contest","fleet"].includes(mode)?mode:"auto";}
function prepareNavigation(){navigationHistory?.replace(viewSnapshot());}
function rememberNavigation(replace=false){navigationHistory?.visit(viewSnapshot(),{replace});}
function navigationStateChanged(state) {
  const payload={canGoBack:!!state.canGoBack,canGoForward:!!state.canGoForward};
  window.dispatchEvent(new CustomEvent("xiaoyu-navigation-state",{detail:{...payload}}));
  if(desktop&&window.webkit?.messageHandlers?.navigationState) {
    try {Promise.resolve(window.webkit.messageHandlers.navigationState.postMessage(payload)).catch(()=>{});}catch{}
  }
}
function restoreView(target) {
  if($("execution-dialog").open)$("execution-dialog").close();
  eventController?.abort();modelPicker.close();permissionPicker.close();workflowPicker.close();
  collaborationPage=["weave","contest","fleet"].includes(target.collaborationPage)?target.collaborationPage:"";
  source=target.source;selected=target.selected;project=target.project;projectSelected=target.projectSelected;
  navigationUnavailable=source==="workbench"&&selected&&!sessions.some(s=>s.session_id===selected)?selected:"";
  filter=target.filter;cliSelected=target.cliSelected;newTask=target.newTask;connectionTarget=target.connectionTarget;
  $("workspace-path").value=target.workspace;$("work-mode").value=normalizeWorkflow(target.workflow);
  pendingModel=target.selected?"":target.model;$("composer-model").value=target.model;$("composer-mode").value=target.mode;
  inputProject=target.inputProject||workspaceController.projectForSession(selected)||"";
  inputWorkspace=target.workspace??connection?.health?.root??"";
  $("message").value=selected?(drafts.get(selected)??connection?.localState?.getDraft(selected)??""):newInput();
  $("create-panel").hidden=true;$("thread-scroll").scrollTop=0;notice();
  if(navigationUnavailable)notice("历史中的会话当前不可用。草稿已保留，可刷新状态或另选会话；不会创建替代任务。");
  if(pageControllers.has(source))pageControllers.get(source).render(source==="content-project"?"project":source,{projectId:projectSelected});
  render();
  if(!connection)return;
  if(source==="workbench"&&selected&&!navigationUnavailable){watch(connection,selected);loadArchive(connection,selected);loadWeaves(connection,selected);contests.load(connection,selected);fleets.load(connection,selected);fleetCommands.load(connection,selected);}
  else if(source==="project"){workspaceController.load(connection);if(projectSelected)workspaceController.loadArtifacts(projectSelected);}
  else if(source==="archived")workspaceController.load(connection);
  else if(source==="cli"){if(cliList.state==="initial")loadCLIList(connection);if(cliSelected)loadCLIHistory(connection,cliSelected);}
  else if(source==="settings"){settingsController.load();providersController.load();dataController.load();}
  else if(source==="connections")connections.load(connection);
  else if(source==="automations")automations.load(connection);
  else if(pageControllers.has(source)){const c=pageControllers.get(source);c.load(connection);if(source==="library")c.loadLibrary?.();}
}
function traverseNavigation(direction) {
  if(!navigationHistory)return;
  saveDraft();prepareNavigation();
  const target=direction==="back"?navigationHistory.back():navigationHistory.forward();
  if(target)restoreView(target);
}
function openManagement(kind) {
  if (!desktop) return;
  if(kind==="connections"){capabilitiesController.showType("connectors");openPage("capabilities");return;}
  prepareNavigation();saveDraft(); source = kind; eventController?.abort(); $("create-panel").hidden = true; $("thread-scroll").scrollTop = 0; notice();
  if (kind === "connections" && !sessions.some(s => s.session_id === connectionTarget)) connectionTarget = selected;
  render();
  rememberNavigation();
  if(connection)(kind === "connections" ? connections : automations).load(connection);
}
const settingsController = createSettingsController({
  context:()=>({conn:connection,writable:!!writable(),desktop}),
  render:()=>{if(source==="settings")renderThread();},
  navigate:openPage,exportFile:downloadFile,version:native?.app_version||"开发模式",
  requestNotifications:async()=>window.webkit?.messageHandlers?.workspace?.postMessage({operation:"notification_permission"}),
  onApplied:values=>{applyNavigation(values.ui);panes?.applyLayout?.({width:values.ui.panel_width});renderComposer();},
  extra:section=>section==="models"?providersController.render():section==="data"?dataController.render():section==="usage"?dataController.renderUsage():section==="keyboard"?shortcutsController.render():"",
});
const shortcutsController=createShortcutsController({values:()=>settingsController.values().ui?.shortcuts||DEFAULT_SHORTCUTS,onChange:value=>settingsController.saveUI({shortcuts:value}),render:()=>{if(source==="settings")renderThread();}});
const dataController=createDataController({context:()=>({conn:connection,writable:!!writable()}),render:()=>{if(source==="settings")renderThread();},exportFile:downloadFile,onRestored:()=>{settingsController.load();workspaceController.load(connection);}});
const providersController=createProvidersController({context:()=>({conn:connection,writable:!!writable()}),render:()=>{if(source==="settings")renderThread();}});
const contentController=createContentController({
  context:()=>({conn:connection,writable:!!writable(),sid:selected||"new-task",active:["library","spaces","pages","search","content-project"].includes(source)}),
  render:()=>{if(["library","spaces","pages","search","content-project"].includes(source))renderThread();},
  artifactCard,openSession:openSearchSource,
  getComposer:()=>$("message").value,putComposer:setComposer,
  exportFile:downloadFile,openPane:openContentPane,
});
for(const id of ["library","spaces","pages","search","content-project"])pageControllers.set(id,contentController);
const capabilitiesController=createCapabilitiesController({
  context:()=>({conn:connection,sid:selected,writable:!!writable()}),
  render:()=>{if(source==="capabilities")renderThread();},
  connectors:{
    load:()=>{if(!sessions.some(s=>s.session_id===connectionTarget))connectionTarget=selected;return connections.load(connection);},
    render:options=>connections.render(options),open:()=>connections.open(),openPreset:id=>connections.openPreset(id),
  },
  draftTask:value=>putReference(typeof value==="string"?value:value.text||""),
});
pageControllers.set("capabilities",capabilitiesController);
function downloadFile({filename,mime_type,text,content}) {
  if(window.webkit?.messageHandlers?.workspace){window.webkit.messageHandlers.workspace.postMessage({operation:"export_text",filename:filename||"羽案导出.txt",mime_type:mime_type||"text/plain",text:text??content??""}).then(r=>notice(r.saved?"已保存导出文件。":"已取消导出。")).catch(e=>notice(`导出失败：${e.message}`));return;}
  const blob=new Blob([text??content??""],{type:mime_type||"text/plain;charset=utf-8"});
  const url=URL.createObjectURL(blob),a=document.createElement("a");a.href=url;a.download=filename||"羽案导出.txt";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
function applyNavigation(ui={}) {
  sidebarCollapsed(sidebarVisibilityOverride ?? !!ui.sidebar_hidden,false,false);
  document.querySelectorAll("[data-planned]").forEach(n=>n.hidden=ui.show_planned===false);
  const parent=document.querySelector(".global-nav");
  for(const [id,command] of [["new-session","new_task"],["search-toggle","search"],["settings","settings"]]){const key=$(id)?.querySelector("kbd"),binding=(ui.shortcuts||DEFAULT_SHORTCUTS)[command];if(key)key.textContent=binding?binding.replace("mod+","⌘").replace("shift+","⇧").toUpperCase():"";}

  for(const id of ui.nav_order||DEFAULT_UI.nav_order){const node=$(id);if(node){if(id!=="projects")parent.append(node);node.hidden=!["spaces","capabilities","automations"].includes(id)&&(ui.hidden_nav||[]).includes(id);}}
}
function openPage(kind,options={}) {
  if(kind==="connection"){$("connection-panel").hidden=false;$("connection-panel").scrollIntoView({block:"nearest"});return;}
  if(kind==="project"){openProject(workspaceController.projectForSession(selected)||projectSelected||workspaceController.projects()[0]?.project_id||"");return;}
  if(kind==="cli"){openCLI();return;}
  if(kind==="panes"){panes?.toggle();return;}
  if(["connections","automations"].includes(kind)){openManagement(kind);return;}
  prepareNavigation();saveDraft();if(options.projectId!==undefined)projectSelected=options.projectId;source=kind;eventController?.abort();$("create-panel").hidden=true;$("thread-scroll").scrollTop=0;notice();render();
  rememberNavigation();
  if(kind==="settings"){settingsController.load();providersController.load();dataController.load();}
  else if(kind==="archived")workspaceController.load(connection);
  else {const c=pageControllers.get(kind);if(c){c.render(kind==="content-project"?"project":kind,{projectId:projectSelected});c.load(connection);if(kind==="library")c.loadLibrary?.();}}
}
function setComposer(text){if(source!=="workbench" || collaborationPage){if(selected)choose(selected);else{prepareNavigation();source="workbench";newTask=true;render();rememberNavigation();}}$("message").value=text;saveDraft();renderComposer();}
function putReference(text){
  setComposer([$("message").value,text].filter(Boolean).join("\n\n"));$("message").focus();notice("引用已放入草稿，尚未发送。");
}
function newConversation(options={}){pendingModel="";if(options.record!==false)prepareNavigation();saveDraft();navigationUnavailable="";startingDraft="";pendingWorkflow="";collaborationPage="";$("work-mode").value=normalizeWorkflow(settingsController.values().ui?.default_workflow);selected="";newTask=true;source="workbench";eventController?.abort();inputProject=options.projectId??(options.workspace?workspaceController.projectForWorkspace(options.workspace):inputProject||workspaceController.projectForSession(selected))??"";const entry=workspaceController.projects().find(p=>p.project_id===inputProject);inputWorkspace=entry?entry.workspace:options.workspace||inputWorkspace||connection?.health?.root||"";$("workspace-path").value=inputWorkspace;connection?.localState?.setDraft("new-input-workspace",inputWorkspace);connection?.localState?.setDraft("new-input-project",inputProject);$("message").value=newInput();$("create-panel").hidden=true;notice();render();if(options.record!==false)rememberNavigation();$("message").focus();}
function renderSessionMenu(){
  const sid=selected;
  region($("session-menu-items"),sid?`${workspaceController.renderSessionActions(sid)}${workspaceController.projectForSession(sid) ? `<button type="button" data-session-command="project">${icon("folder")}项目说明与成果</button>` : ""}<button type="button" data-session-command="rename">${icon("edit")}重命名…</button><button type="button" data-session-command="group">${icon("group")}设置会话分组…</button><hr><button type="button" data-session-command="side">${icon("message")}新建侧边会话</button><button type="button" data-session-command="side-history">${icon("clock")}恢复侧边会话…</button><button type="button" data-session-command="fork">${icon("fork")}从历史另开分支…</button><button type="button" data-session-command="automation">${icon("clock")}创建自动化…</button><hr><button type="button" data-session-command="details">${icon("info")}运行详情</button><button type="button" data-session-command="copy">${icon("copy")}复制对话</button><button type="button" data-session-command="export">${icon("export")}导出 Markdown</button><button type="button" data-session-command="json">${icon("document")}导出 JSON</button><button type="button" data-session-command="window">${icon("window")}在新窗口中打开</button><form id="session-command-form" hidden><label id="session-command-label" for="session-command-value"></label><input id="session-command-value" autocomplete="off"><button type="submit">确认</button><button type="button" data-session-command="cancel">${icon("close")}取消</button></form>`:"");
}
async function sessionCommand(command){
  if(!selected||!connection)return;
  const conn=connection,sid=selected;
  if(["rename","fork","group"].includes(command)){
    $("session-command-form").hidden=false;$("session-command-form").dataset.command=command;
    $("session-command-label").textContent=command==="group"?"分组名称（留空移出分组）":command==="rename"?"会话标题":"复制前几轮（留空复制全部；文件不会回退）";
    $("session-command-value").value=command==="group"?(settingsController.values().ui?.session_meta?.[sid]?.group||""):command==="rename"?titleOf(session()):"";$("session-command-value").focus();return;
  }
  if(command==="cancel"){$("session-command-form").hidden=true;return;}
  $("session-menu").open=false;
  try{
    if(["copy","export","json"].includes(command)){
      const data=await conn.client.get(`/desktop/sessions/${encodeURIComponent(sid)}/export?format=${command==="json"?"json":"markdown"}`,conn.controller.signal);
      if(command==="copy"){await navigator.clipboard.writeText(data.content);notice("对话已复制。");}else downloadFile(data);
      if(data.warning)notice(data.warning);return;
    }
    if(command==="details"){$("execution-dialog").showModal();return;}
    if(command==="project"){openProject(workspaceController.projectForSession(sid));return;}
    if(command==="window"){if(window.webkit?.messageHandlers?.workspace)window.webkit.messageHandlers.workspace.postMessage({operation:"new_window",session_id:sid});else notice("多窗口需要在桌面应用中使用。");return;}
    if(command==="side"){await openSideConversation();return;}
    if(command==="side-history"){await openSideHistory();return;}

    if(command==="automation"){const text=$("message").value||titleOf(session());openManagement("automations");automations.fromSession?.({session_id:sid,workspace:session()?.workspace,project_id:session()?.project_id||workspaceController.projectForSession(sid),text});return;}
  }catch(e){notice(e.message);}
}
async function openSideConversation(id){
  let side=id&&sideConversations.find(s=>s.id===id);
  if(!side){const workspace=session()?.workspace||connection?.health?.root,projectId=session()?.project_id||workspaceController.projectForSession(selected)||inputProject;side=createSideConversation({id,context:()=>({conn:connection,workspace,projectId}),openMain:async id=>{await refreshAfterAction(connection);choose(id);}});sideConversations.push(side);}
  await panes.openContent({kind:"custom",id:side.id,title:"侧边会话",node:side.node});
}
async function openSideHistory(){const node=document.createElement("section"),entries=sideConversationEntries(connection?.localState);node.className="content-panel";node.innerHTML=`<h3>侧边会话草稿与提交记录</h3><p>恢复原会话和原请求，不自动重新发送。</p>${entries.length?entries.map(e=>`<button type="button" data-side-restore="${h(e.id)}">${h(e.sid||"尚未确认创建")} · ${h(e.text_preview||"已保存讨论")}${e.pending?" · 有待核对提交":""}${e.invalid?" · 草稿格式异常":""}</button>`).join(""):"<p>没有已保存的侧边会话。</p>"}`;node.addEventListener("click",e=>{const b=e.target.closest("[data-side-restore]");if(b)openSideConversation(b.dataset.sideRestore);});await panes.openContent({kind:"custom",id:"side-history",title:"侧边会话记录",node});}
async function openContentPanel(view="pages",pageId=""){
  if(!desktop)return;const node=document.createElement("section");node.className="content-panel";
  const conn=connection,projectId=workspaceController.projectForSession(selected)||projectSelected||workspaceController.projects()[0]?.project_id||"";
  let c;const paint=()=>{if(c)region(node,c.render(pageId?"page-panel":c.currentView()));};
  c=createContentController({draftNamespace:pageId?`page:${pageId}`:`panel:${view}`,context:()=>({conn:connection,writable:connection===conn&&!!writable(),sid:selected||"new-task",active:true}),render:paint,artifactCard,openSession:choose,getComposer:()=>$("message").value,putComposer:setComposer,exportFile:downloadFile,openPane:openContentPane});
  node.addEventListener("click",e=>c.click(e));node.addEventListener("input",e=>c.input(e));node.addEventListener("change",e=>c.input(e));node.addEventListener("submit",e=>c.onSubmit(e));
  c.render(pageId?"page-panel":view,{projectId});await c.load(conn);if(pageId)c.openPage(pageId);else if(view==="project")c.openProject(projectId);paint();
  await panes.openContent({kind:"custom",id:pageId?`page:${pageId}`:`content-panel:${view}:${projectId}`,title:pageId?"页面编辑":view==="project"?"项目文件与资料":"页面与空间",node});
}
async function openContentPane(value){if(value.kind==="page")return openContentPanel("pages",value.source.page_id);return panes.openContent(value.kind==="file"?{...value,kind:"custom"}:value);}
async function openCollaborationPanel(){
  if(!selected){notice("请先创建或选择会话，再打开协作看板。");return;}
  const node=document.createElement("section"),sid=selected,conn=connection;node.className="content-panel";
  const paint=()=>{node.innerHTML=`<h3>协作工作区</h3><p>当前会话的协作状态快照。配置、控制与审批在来源会话中处理。</p><div class="actions"><button data-collab="weave">批量执行</button><button data-collab="contest">方案比选</button><button data-collab="fleet">统筹协作</button><button data-board-refresh>刷新看板</button></div><p class="note">${h(sid)} · ${new Date().toLocaleTimeString()}</p><fieldset disabled class="preferences-fields">${renderWeaves(weaves.get(sid),{artifactCard:(a,c)=>artifactCard(a,c)})}${contests.results(sid)}${fleets.render(sid)}</fieldset>`;};
  const reload=async()=>{await Promise.allSettled([loadWeaves(conn,sid),contests.load(conn,sid),fleets.load(conn,sid)]);if(conn===connection)paint();};
  node.addEventListener("click",e=>{const b=e.target.closest("[data-collab]");if(b){choose(sid);activateWorkflow(b.dataset.collab);}if(e.target.closest("[data-board-refresh]"))reload();});paint();await panes.openContent({kind:"custom",id:`collaboration:${sid}`,title:"协作看板",node,replace:true,source:{session_id:sid}});await reload();
}

function notifyAutomation(snapshot){
  automationSessions.clear();for(const run of Object.values(snapshot.state?.runs||{}))if(run.session_id)automationSessions.add(run.session_id);
  const next=new Set((snapshot.notifications||[]).map(n=>n.id));if(automationNoticeIds&&settingsController.values().notifications?.automation!==false){for(const n of snapshot.notifications||[])if(!automationNoticeIds.has(n.id)&&window.webkit?.messageHandlers?.workspace)window.webkit.messageHandlers.workspace.postMessage({operation:"notify",title:"羽案 · 自动化运行更新",body:`${n.state} · ${n.message||"请查看运行记录"}`,session_id:n.session_id||""}).catch(()=>{});}automationNoticeIds=next;
}
function notifyChanges(next){
  const prefs=settingsController.values().notifications||{};
  for(const s of next){const state=stateOf(s),kind=state.label==="等待审批"?"approval":state.label==="等待回答"?"question":s.status==="error"?"failed":!s.busy&&s.last_result?"completed":"";
    const signature=JSON.stringify([kind,s.turns,s.pending_approvals?.map(a=>a.id),s.questionItems?.filter(q=>["open","pending"].includes(q.state)).map(q=>q.id)]),old=notificationStates.get(s.session_id);notificationStates.set(s.session_id,signature);
    if(old&&old!==signature&&kind&&!automationSessions.has(s.session_id)&&prefs[kind]!==false&&window.webkit?.messageHandlers?.workspace)window.webkit.messageHandlers.workspace.postMessage({operation:"notify",title:`羽案 · ${state.label}`,body:titleOf(s),session_id:s.session_id}).catch(()=>{});
  }
}
function openProject(id = "") {
  if (!desktop) return;
  prepareNavigation();saveDraft(); source = "project"; projectSelected = id; project = workspaceController.projects().find(p => p.project_id === id)?.workspace || connection?.health?.root || "";
  eventController?.abort(); $("thread-scroll").scrollTop = 0; $("create-panel").hidden = true; notice(); render();
  rememberNavigation();
  if (connection) { workspaceController.load(connection); if (id) workspaceController.loadArtifacts(id); }
}
function renderProjectThread() {
  const entry = workspaceController.projects().find(p => p.project_id === projectSelected), target = entry?.workspace;
  $("title").textContent = entry ? entry.name||projectName(target) : "项目";
  $("subtitle").textContent = entry ? (entry.folders?.length?`${entry.folders.length} 个文件夹`:"未关联文件夹") : "组织对话与文件夹";
  $("session-state").textContent = "";
  const choices = workspaceController.projects();
  let html = `<section class="project-overview">${workspaceController.renderCreate()}<label>项目<select id="project-choice"><option value="">选择已登记的项目</option>${choices.map(p => `<option value="${h(p.project_id)}" ${p.project_id === projectSelected ? "selected" : ""}>${h(p.name||p.workspace)}</option>`).join("")}</select></label>`;
  if (!entry) html += `${workspaceController.renderStatus()}<p class="note">${choices.length ? "选择项目以编辑说明、查看会话和成果。" : "尚未读取到项目目录；重新读取会保留本机编辑。"}</p><button type="button" data-workspace-action="reload">重新读取项目状态</button>`;
  else {
    const projectSessions = workspaceController.filterSessions(sessions.filter(s => workspaceController.projectForSession(s.session_id) === projectSelected && hasRecentActivity(s)),{view:"all"});
    html += `<div class="actions"><button type="button" class="primary" data-project-create="${h(entry.project_id)}" ${!writable() ? "disabled" : ""}>在此项目新建对话</button>${workspaceController.projectForSession(selected) === projectSelected ? `<button type="button" data-project-return="${h(selected)}">返回当前会话</button>` : ""}</div>${workspaceController.renderProject(entry.project_id)}<button type="button" data-project-materials="${h(entry.project_id)}">管理项目指令与资料</button><section class="project-session-list"><h3>项目会话（${projectSessions.length}）</h3>${projectSessions.length ? projectSessions.map(s => `<div class="project-session-row"><button type="button" data-project-return="${h(s.session_id)}"><strong>${h(titleOf(s))}</strong> · ${h(stateOf(s).label)}${workspaceController.sessionMeta(s.session_id).pinned ? " · 已置顶" : ""}${workspaceController.sessionMeta(s.session_id).archived ? " · 已归档" : ""}</button>${workspaceController.renderSessionActions(s.session_id)}</div>`).join("") : `<p class="note">此项目还没有对话。</p>`}</section>`;
  }
  region($("thread"),`${html}</section>`);
}
function renderApprovalIdentity(identity) {
  if (identity?.kind === "qixiang") return renderWeaveApprovalIdentity(identity);
  return renderFleetApprovalIdentity(identity);
}
function banner(text = "") {
  if (connection?.storageError && !text.includes(connection.storageError)) text += `${text ? "\n" : ""}本机草稿尚未保存：${connection.storageError}。请保留输入内容。`;
  $("banner").textContent = text; $("banner").hidden = !text;
  region($("state-conflicts"),renderLocalConflicts(connection?.localState));
}
function connectionBanner(conn) {
  const warnings = [];
  if (conn.storageError) warnings.push(`本机草稿保存失败：${conn.storageError}。请保留输入内容，保存成功前不会提交任务。`);
  if (conn.health?.desktop_protocol !== 1 || !conn.health?.persist) warnings.push("当前服务缺少 P1 持久化提交能力，仅能查看。请使用本实验分支并开启持久化。");
  banner(warnings.join("\n"));
}
function notice(text = "") { feedback = null; $("notice").textContent = text; }
function updateFeedback() {
  if (source !== "workbench" || !feedback || feedback.sid !== selected) return;
  if (feedback.kind === "answer") {
    const q = session()?.questionItems?.find(q => q.question_id === feedback.qid);
    $("notice").textContent = q?.state === "answered" ? "模型已接纳答案。" : q?.state === "queued"
      ? session()?.busy ? "答案已排队，等待模型接纳。" : "答案已排队；会话空闲，请点击“根据答案继续”。"
      : "回答已提交，请核对问题的最新状态。";
  } else if (feedback.kind === "steer") {
    const accepted = views.get(selected)?.events.some(e => e.kind === "steer.accepted" && e.seq >= feedback.seq && e.text === feedback.text);
    $("notice").textContent = accepted ? "插话已被模型接纳。" : "插话已排队，尚未观察到模型接纳事件。";
  }
}
function status(text, kind = "") { $("connection-state").textContent = text; $("connection-state").title=text;$("connection-state").dataset.connected=String(text==="已连接"); $("connection-state").className = kind; }

// Preserve unsent answers, focus, selection and expanded details across snapshots.
function region(root, html) {
  if (root._html === html) return;
  const expanded = [...root.querySelectorAll("details[data-key]")].map(n => [n.dataset.key, n.open]);
  const values = [...root.querySelectorAll("input[id],textarea[id]")].filter(n => !n.hasAttribute("data-capability-search") && !n.hasAttribute("data-connection-arg") && !n.hasAttribute("data-connection-pair") && !n.hasAttribute("data-connection-token") && !n.hasAttribute("data-connection-inherit") && !n.dataset.resumeItem && !n.dataset.fleetDraft && !n.dataset.weaveMessageDraft && !n.dataset.projectContextDraft && !n.dataset.autoField && !n.dataset.autoNote && !n.dataset.connectionField && !n.closest(".contest-compose,.contest-selection-form,.contest-followup,.fleet-command-section") && !n.dataset.contestControlled && !n.dataset.setting && !n.closest(".settings-layout,.content-workspace")).map(n => [n.id, n.value, n.checked]);
  const focus = root.contains(document.activeElement) ? document.activeElement : null;
  const attribute=focus&&["data-setting","data-content-field","data-content-edit","data-provider-field","data-data-text","data-shortcut","data-settings-section","data-nav-hidden","data-nav-move"].find(a=>focus.hasAttribute(a));
  const selector=focus?.hasAttribute("data-nav-move")?`[data-nav-move="${CSS.escape(focus.dataset.navMove)}"][data-direction="${CSS.escape(focus.dataset.direction)}"]`:focus?.id?`#${CSS.escape(focus.id)}`:attribute?`[${attribute}="${CSS.escape(focus.getAttribute(attribute))}"]`:null;
  const position = focus ? [selector, focus.selectionStart, focus.selectionEnd] : null;
  root.innerHTML = html; root._html = html;
  for (const [id, value, checked] of values) { const node = $(id); if (node && root.contains(node)) { node.value = value; node.checked = checked; } }
  for (const [key, open] of expanded) { const node = root.querySelector(`details[data-key="${CSS.escape(key)}"]`); if (node) node.open = open; }
  if (position?.[0]) { const node = root.querySelector(position[0]); if (node) { node.focus({preventScroll: true}); if (["TEXTAREA","INPUT"].includes(node.tagName) && position[1] !== null) {try{node.setSelectionRange(position[1], position[2]);}catch{}} } }
}
function choose(id,{record=true}={}) {
  if(!id){newConversation({record});return;}
  if (id === selected && source === "workbench" && !collaborationPage && !(navigationUnavailable && sessions.some(s=>s.session_id===id))) return;
  if(record)prepareNavigation();
  saveDraft();
  source = "workbench"; collaborationPage=""; $("work-mode").value="auto"; project = ""; newTask = false;navigationUnavailable="";
  selected = id; inputProject=sessions.find(s=>s.session_id===id)?.project_id||workspaceController.projectForSession(id)||""; inputWorkspace=sessions.find(s=>s.session_id===id)?.workspace||inputWorkspace; $("message").value = (id && sessions.some(s=>s.session_id===id && !hasRecentActivity(s)) ? newInput() : undefined) ?? drafts.get(id) ?? (connection?.localState?.getDraft(id) || pendingFor(id).find(([,a]) => ownsAction(a) && ["prompt","steer"].includes(a.kind))?.[1].body.text || "");
  eventController?.abort(); $("thread-scroll").scrollTop = 0;
  notice(); render();
  if(record)rememberNavigation();
  const target=sessions.find(s=>s.session_id===selected),meta=target&&readMetadata(target,settingsController.values().ui);
  if(meta)settingsController.saveUI({session_meta:meta}).then(()=>render()).catch(()=>{});
  if (connection && selected) { watch(connection, selected); loadArchive(connection, selected); loadWeaves(connection, selected); contests.load(connection, selected); fleets.load(connection, selected); fleetCommands.load(connection, selected); }
}
function saveDraft() {
  if (!selected || session() && !hasRecentActivity(session())) { if (source === "workbench") newInput(session()?.workspace||inputWorkspace,$("message").value); return; }
  const text = $("message").value;
  drafts.set(selected, text);
  connection?.localState?.setDraft(selected, text);
}
function titleOf(s) { return s.title || titles.get(s.session_id) || `${projectName(s.workspace)} · ${s.session_id.slice(-6)}`; }
function sessionButton(s, prefix) {
  const active=source==="workbench"&&selected===s.session_id,state=stateOf(s),unread=!active&&sessionUnread(s,settingsController.values().ui);
  const marker=state.attention?`<span class="session-signal ${h(state.tone)}" title="${h(state.label)}" aria-label="${h(state.label)}">${icon("bell")}</span>`:state.tone==="running"?`<span class="session-signal running" title="执行中" aria-label="执行中">${icon("play")}</span>`:unread?'<span class="unread-dot" title="有未读更新" aria-label="有未读更新">●</span>':"";
  return `<div class="session-row" data-current="${active}"><button id="${prefix}-${h(s.session_id)}" type="button" class="session" data-session="${h(s.session_id)}" aria-pressed="${active}" title="${h(titleOf(s))} · ${h(projectName(s.workspace))} · ${h(state.label)}"><span class="session-name">${h(titleOf(s))}</span>${isContinued(s)?'<span class="origin-tag" title="这场会话续写的是一段终端历史">终端</span>':""}${marker}</button>${workspaceController.renderSessionActions(s.session_id,{compact:true})}</div>`;
}
function terminalButton(item, prefix) {
  const active=source==="cli"&&cliSelected===item.catalog_id,label=terminalLabel(item);
  return `<div class="session-row" data-current="${active}"><button id="${prefix}-${h(item.catalog_id)}" type="button" class="session" data-cli-session="${h(item.catalog_id)}" aria-pressed="${active}" title="${h(cliTitle(item))} · ${h(projectName(item.workspace))} · ${h(label)}"><span class="session-name">${h(cliTitle(item))}</span><span class="origin-tag${item.locked?" busy":""}" aria-label="${h(label)}">${item.locked?"使用中":item.in_scope===false?h(projectName(item.workspace)):"终端"}</span></button></div>`;
}
function recentRows(rows,terminal,prefix,limit) {
  const html=mergeRecent(rows,terminal).map(e=>e.kind==="session"?sessionButton(e.value,prefix):terminalButton(e.value,prefix));
  return html.slice(0,limit).join("")+(html.length>limit?`<details class="sidebar-show-more" data-key="more-${h(prefix)}"><summary>展开显示 <span>${html.length-limit}</span></summary>${html.slice(limit).join("")}</details>`:"");
}
function sidebarSection(key,title,content,{open=true,extra=""}={}) {
  return `<details class="sidebar-section" data-key="section-${h(key)}" ${open?"open":""}><summary><span>${h(title)}</span>${icon("chevron")}${extra}</summary><div class="sidebar-section-content">${content}</div></details>`;
}
function sidebarRows(rows,prefix,limit=5) {
  return rows.slice(0,limit).map(s=>sessionButton(s,prefix)).join("")+(rows.length>limit?`<details class="sidebar-show-more" data-key="more-${h(prefix)}"><summary>展开显示 <span>${rows.length-limit}</span></summary>${rows.slice(limit).map(s=>sessionButton(s,prefix)).join("")}</details>`:"");
}

function render() {
  document.querySelector(".shell").classList.toggle("settings-open",source === "settings");
  $("home").setAttribute("aria-pressed",String(source === "workbench"));
  $("settings").setAttribute("aria-pressed",String(source === "settings"));
  $("automations").disabled=!desktop;
  $("automations").setAttribute("aria-pressed",String(source === "automations"));
  $("projects").hidden = !desktop; $("archive-sessions").hidden = !desktop;
  $("projects").setAttribute("aria-pressed", String(source === "project"));
  $("new-contest").hidden = source !== "workbench" || !selected || connection?.health?.contest_protocol !== 1;
  $("new-weave").hidden = source !== "workbench" || !selected || connection?.health?.weave_command_protocol !== 1;
  $("new-fleet").hidden = source !== "workbench" || !selected || connection?.health?.fleet_command_protocol !== 1;
  $("collaboration-menu").hidden = $("new-contest").hidden && $("new-weave").hidden && $("new-fleet").hidden;
  $("n-attention").textContent = visibleSessions(sessions.filter(hasRecentActivity), "", "attention", "").length;
  $("n-running").textContent = visibleSessions(sessions.filter(hasRecentActivity), "", "running", "").length;
  $("activity-dot").hidden=Number($("n-attention").textContent)===0;
  $("sidebar-activity").classList.toggle("has-filter",filter!=="all");
  $("sidebar-activity").querySelector("summary").title=`会话动态 · 待处理 ${$("n-attention").textContent} · 执行中 ${$("n-running").textContent}`;
  document.querySelectorAll("[data-filter]").forEach(n => n.setAttribute("aria-pressed", String(source === "workbench" && n.dataset.filter === filter)));
  $("cli-sessions").setAttribute("aria-pressed", String(source === "cli"));
  if (source !== "cli") cliListMode = false;
  const utility = source === "cli" && cliListMode ? "Xiaoyu CLI 会话" : source === "archived" ? "已归档" : "";
  const terminal = connection?.health?.catalog_protocol === 1 && filter === "all" ? terminalRows(cliList.items, sessions, search) : [];
  $("sidebar-more").classList.toggle("has-current", !!utility);
  $("more-label").textContent = utility ? `更多 · ${utility}` : "更多";
  const candidates = visibleSessions(sessions.filter(hasRecentActivity), "", filter === "archived" ? "all" : filter, "").filter(s => !search || `${titleOf(s)} ${s.session_id} ${s.workspace} ${s.model}`.toLowerCase().includes(search.toLowerCase()));
  const visible = workspaceController.filterSessions(candidates,{view:filter === "archived" ? "archived" : ["attention","running"].includes(filter) ? "all" : "active"});
  const recent = orderedSessions(visible,settingsController.values().ui,sid=>workspaceController.sessionMeta(sid),titleOf);
  const customGroups=[...new Set(recent.map(s=>settingsController.values().ui?.session_meta?.[s.session_id]?.group).filter(Boolean))].sort();
  const customHTML=customGroups.map(group=>sidebarSection(`custom-${group}`,group,sidebarRows(recent.filter(s=>settingsController.values().ui?.session_meta?.[s.session_id]?.group===group),`group-${group}`))).join("");
  const groups = workspaceController.projects();
  const pinned=recent.filter(s=>workspaceController.sessionMeta(s.session_id).pinned);
  const projectHTML=groups.map((entry,i)=>{
    const path=entry.workspace,pid=entry.project_id,rows=recent.filter(s=>workspaceController.projectForSession(s.session_id)===pid);
    return `<details class="project-group" data-key="project-${h(pid)}"><summary title="${h(path)}">${icon("folder")}<span>${h(entry.name||projectName(path))}</span>${pid?`<button type="button" class="project-row-details project-row-action" data-project="${h(pid)}" title="项目设置与成果" aria-label="项目设置与成果">${icon("more")}</button><button type="button" class="project-row-new project-row-action" data-project-new-chat="${h(pid)}" title="在此项目新建对话" aria-label="在 ${h(entry.name||projectName(path))} 中新建对话" ${!writable()?"disabled":""}>${icon("edit")}</button>`:""}${icon("chevron")}</summary><div>${sidebarRows(rows,`project-${i}`)}${!rows.length?'<p class="sidebar-note">暂无对话</p>':""}</div></details>`;
  }).join("");
  const list=search||filter!=="all"?`<div class="sidebar-results-heading"><span>${search?"搜索结果":filter==="archived"?"已归档":filter==="attention"?"待处理":"执行中"}</span><button type="button" class="quiet" data-filter="all" data-sidebar-clear title="返回全部会话">${icon("close")}</button></div>${search?recentRows(visible,terminal,"match",200):visible.map(s=>sessionButton(s,"match")).join("")}${!visible.length&&!(search&&terminal.length)?'<p class="sidebar-note">没有匹配的会话</p>':""}`:
    sidebarSection("pinned","置顶",pinned.length?sidebarRows(pinned,"pinned"): '<p class="sidebar-note">悬停会话可置顶</p>',{open:pinned.length>0})+customHTML+
    sidebarSection("projects","项目",projectHTML||'<p class="sidebar-note">暂无项目</p>',{extra:desktop?'<button type="button" class="sidebar-project-manage" data-sidebar-projects title="管理项目" aria-label="管理项目">'+icon("more")+'</button>':""})+
    sidebarSection("recent","最近",recentRows(recent.filter(s=>hasRecentActivity(s)&&!workspaceController.sessionMeta(s.session_id).pinned),terminal,"recent",12));
  if (source === "cli" && cliListMode) renderCLIList();
  else region($("sessions"), search || filter !== "all" || visible.length || groups.length || terminal.length ? list : `<p class="empty">${sessions.length ? "没有匹配的会话。试试其他关键词或工作台筛选。" : "还没有会话，创建一件新任务。"}</p>`);
  $("new-session").disabled = !writable();
  $("create").disabled = !writable() || !!connection?.outbox?.items.create;
  $("workspace-path").readOnly = !!connection?.outbox?.items.create;
  $("return-chat").hidden=source === "workbench" && !collaborationPage;
  $("session-menu").hidden=!selected || source !== "workbench";
  document.querySelectorAll("[data-route]").forEach(n=>n.setAttribute("aria-pressed",String(source===n.dataset.route)));
  applyNavigation(settingsController.values().ui);
  renderSessionMenu();
  renderOutbox(); renderThread(); renderComposer(); updateFeedback();
}
function renderWelcomeStarters() {
  const starters=$("thread").querySelector(".welcome-starters");
  if(!starters)return;
  starters.hidden=source!=="workbench" || !!selected || !!$("message").value.trim() || !writable() || $("message").disabled || $("message").readOnly;
}
function renderTakeover() {
  const bar=$("takeover-bar"),view=cliViews.get(cliSelected);
  bar.hidden = source !== "cli" || !cliSelected;
  if (bar.hidden) { bar.replaceChildren(); return; }
  const t=takeover(view,{writable:!!writable(),pending:!!connection?.outbox?.items.create,native:!!window.webkit?.messageHandlers?.workspace});
  bar.dataset.kind=t.kind;
  const actions=t.kind==="elsewhere"?(t.canOpen?`<button type="button" class="primary" data-cli-open-workspace>在该目录的工作台中打开</button>`:"")
    :t.kind==="held"?`<button type="button" class="primary" data-cli-open-session="${h(t.sessionId)}">打开这场会话</button>`
    :`${t.kind==="ready"?`<button type="button" class="primary" data-cli-continue ${t.canContinue?"":"disabled"}>在这里继续</button>`:""}${["conflict","blocked"].includes(t.kind)?`<button type="button" data-cli-retry ${t.canRetry?"":"disabled"}>${view?.state==="loading"?"正在重试…":"重试"}</button>`:""}${t.kind!=="loading"?`<button type="button" data-cli-fork ${t.canFork?"":"disabled"} title="复制到目前为止的历史，另开一场独立会话；原会话不受影响">另开分支</button>`:""}`;
  region(bar,`<span class="takeover-icon">${icon(t.kind==="conflict"?"lock":t.kind==="held"?"check":t.kind==="elsewhere"?"folder":"terminal")}</span><div class="takeover-text"><strong>${h(t.title)}</strong><span>${h(t.detail)}</span>${t.note?`<span class="takeover-note">${h(t.note)}</span>`:""}</div><div class="takeover-actions">${actions}</div>`);
}
function renderComposer() {
  renderTakeover();
  $("composer-area").hidden = source !== "workbench" || !!collaborationPage;
  $("workspace").dataset.welcome=String(source === "workbench" && !session());
  const s = session(), pending = pendingFor(selected).some(([, a]) => ["prompt", "steer", "weave", "contest", "fleet_command"].includes(a.kind));
  const directCollaboration = s?.busy && ["qixiang","douqiao","chenshu"].includes(s.in_flight?.command?.kind);
  $("message").disabled = !connection; $("message").readOnly = pending || !!connection?.outbox?.items.create;
  renderWelcomeStarters();
  const starterDisabled=$("message").disabled || $("message").readOnly;
  for(const id of ["starter-menu"]) {
    $(id).querySelector("summary").setAttribute("aria-disabled",String(starterDisabled));
    $(id).querySelectorAll("button").forEach(n=>n.disabled=starterDisabled || n.dataset.add==="files" && !window.webkit?.messageHandlers?.workspace);
    if(starterDisabled)$(id).open=false;
  }
  $("send").disabled = modelSaves.has(selected) || !!(s?.busy&&s.next_model) || !writable() || !!(selected&&!s) || (s && !sessionActionAllowed(s)) || !!connection?.outbox?.items.create || pending || directCollaboration || !$("message").value.trim();
  const stopPending=pendingFor(selected).some(([,a])=>a.kind==="abort");
  const controls=composerControls({busy:!!s?.busy,hasText:!!$("message").value.trim(),pending:pending || !!connection?.outbox?.items.create,stopPending,directCollaboration:!!directCollaboration});
  $("send").hidden=controls.sendHidden;
  $("send").innerHTML=`${icon(controls.sendSymbol)}<span class="sr-only">${controls.sendLabel}</span>`;
  $("send").dataset.loading=String(controls.sendSymbol==="loading");
  $("send").setAttribute("aria-label",controls.sendLabel);$("send").title=controls.sendLabel;
  $("stop").hidden=controls.stopHidden;
  $("stop").disabled=!writable() || !s?.in_flight?.turn || stopPending;
  $("stop").innerHTML=`${icon(controls.stopSymbol)}<span class="sr-only">${controls.stopLabel}</span>`;
  $("stop").dataset.loading=String(controls.stopSymbol==="loading");
  $("stop").setAttribute("aria-label",controls.stopLabel);$("stop").title=controls.stopLabel;
  const sendHint=settingsController.values().ui?.send_key === "enter" ? "Enter 发送 · Shift / Alt + Enter 换行" : "⌘Enter 发送 · Enter / Shift / Alt + Enter 换行";
  $("composer-hint").textContent = !connection ? "连接本机服务后，即可开始任务" : selected&&!s ? "此历史会话当前不可用 · 草稿已保留，暂不可提交" : !s ? "" : s.writable === false ? s.write_blocked_reason || "当前会话暂不能写入，请核对服务状态" : pending ? "这次提交尚待核对，原文已保留" : directCollaboration ? "协作任务执行中；可保存草稿，调整任务请先停止，再新建任务或按可用条目续跑" : s.busy ? `插话将在安全步骤边界接纳 · ${sendHint}` : "";
  if (s?.busy && fleets.hasUnsettled(selected)) $("composer-hint").textContent += " · 停止本轮不等于停止统筹协作成员";
  if(s?.next_model)$("composer-hint").textContent = `下一条消息使用 ${s.next_model}${s.busy?` · 当前回复仍使用 ${s.model}`:""}`;
  $("composer-hint").hidden = !$("composer-hint").textContent;
  const composerProject=workspaceController.projects().find(p=>p.project_id===(s?.project_id||workspaceController.projectForSession(s?.session_id)||inputProject));
  $("composer-project-label").textContent=composerProject?.name||projectName(s?.workspace||inputWorkspace||connection?.health?.root||"选择项目");
  region($("composer-project-choices"),workspaceController.projects().map(p=>`<button type="button" data-input-project="${h(p.project_id)}" aria-pressed="${p.project_id===inputProject}">${icon("folder")}<span>${h(p.name||projectName(p.workspace))}</span></button>`).join(""));
  fitComposer($("message"));
  const prefs=settingsController.snapshot(),models=prefs?.options?.models||[],modes=prefs?.options?.modes||[];
  const model=$("composer-model"),mode=$("composer-mode");
  const modelValue=(s?.next_model||s?.model)??defaultComposerModel({models,pending:pendingModel,saved:prefs?.values.default_model,effective:prefs?.effective?.model}),modeValue=s?.mode??mode.value??prefs?.values.default_mode??"";
  const modelRows=[...new Set([modelValue,...models.map(m=>m.id)].filter(Boolean))];
  region(model,`${modelRows.length?"":'<option value="">正在读取模型…</option>'}${modelRows.map(id=>`<option value="${h(id)}">${h(id)}</option>`).join("")}`);
  region(mode,`${modes.map(m=>`<option value="${h(m.id)}">${h(executionModePresentation(m).label)}</option>`).join("")}<option value="">使用默认设置</option>`);
  model.value=modelValue;mode.value=modeValue;model.disabled=!!s?.agent||modelSaves.has(selected)||!modelRows.length||!writable();mode.disabled=!!s;
  model.title=mode.title=s?"当前会话配置；新建会话时可选择默认模型和执行模式":"用于即将创建的新会话";
  model.title=s?.agent?"此会话使用固定助手配置":s?.busy?`当前回复：${s.model}；选择的模型用于下一条消息`:"选择下一条消息使用的模型";
  modelPicker.render({models});
  $("work-mode").disabled=!writable() || $("message").readOnly || !!(selected&&!s);
  permissionPicker.render({choices:executionPermissionChoices(modes,prefs?.effective?.mode)});workflowPicker.render({choices:WORKFLOW_CHOICES});
}
function renderOutbox() {
  const items = Object.entries(connection?.outbox?.items || {}).filter(([scope, a]) => !scope.startsWith("side:") && (!a.sid || source === "workbench" && a.sid === selected) && showSubmissionReceipt(a,{owned:ownsAction(a)}));
  region($("outbox"), items.map(([scope, a]) => {
    const labels = {create:"创建会话",prompt:"提交任务",steer:"插话",abort:"停止本轮",approval:"处理审批",answer:"提交答案",weave:"提交批量执行批次",contest:"提交方案比选",selection:"记录采纳方案",fleet_message:"向成员发送消息",fleet_stop:"请求停止成员",fleet_command:"执行统筹协作操作",weave_message:"向批量执行单项发送消息",weave_stop:"请求停止批量执行单项"};
    const states = {ready:"尚未发送",sending:"正在提交",accepted:"已接收",unknown:"回执未知",rejected:"未完成",recorded:"已有提交记录"};
    const retry = a.state === "unknown" && ["create", "prompt", "answer", "abort", "weave", "contest", "selection", "fleet_message", "fleet_stop", "fleet_command", "weave_message", "weave_stop"].includes(a.kind);
    const fleetTarget = fleetActionTarget(a), weaveTarget = weaveActionTarget(a);
    return `<div><strong>${a.kind === "create" && a.body.adopt_from ? "在这里继续终端会话" : a.kind === "create" && a.body.fork_from ? "从终端历史另开分支" : labels[a.kind]} · ${states[a.state] || h(a.state)}</strong><p>${h(a.message || (a.state === "unknown" ? "请求可能已经送达。请先刷新状态；重试将保留原文与原幂等键。" : "请等待服务回执。"))}</p>${!ownsAction(a) ? `<p class="note">此提交来自另一窗口，请回到原窗口核对；关闭后可恢复原窗口继续处理。</p>` : ""}${fleetTarget ? `<p class="note">目标成员 ${h(fleetTarget[1])} · 运行代次 ${h(fleetTarget[2])}</p>` : ""}${weaveTarget ? `<p class="note">批量执行批次 ${h(weaveTarget[0])} · 任务 ${h(weaveTarget[1])} · 执行实例 ${h(weaveTarget[2])}</p>` : ""}${a.body.text ? `<details><summary>查看原文</summary><p>${h(a.body.text)}</p></details>` : ""}${ownsAction(a) && a.state !== "sending" ? `<div class="actions">${retry || a.state === "ready" ? `<button type="button" data-retry="${h(scope)}">使用原请求重试</button>` : ""}<button type="button" data-forget="${h(scope)}">${a.state === "unknown" || a.state === "recorded" ? "已核对，不再重试此请求" : "清除此回执"}</button></div>` : ""}</div>`;
  }).join(""));
}
function openCLI() {
  prepareNavigation();saveDraft(); source = "cli"; cliListMode = true; eventController?.abort();
  $("create-panel").hidden = true; $("thread-scroll").scrollTop = 0;
  notice(); render();
  rememberNavigation();
  if (connection && cliList.state === "initial") loadCLIList(connection);
}
function cliTitle(s) { return s.title || `CLI 会话 · ${s.catalog_id?.slice(-6) || ""}`; }
function renderCLIList() {
  let html = `<h2 class="section-label">Xiaoyu CLI 会话</h2>`;
  if (!connection) html += `<p class="empty">连接服务后，可浏览允许工作目录内的 CLI 会话。</p>`;
  else if (connection.health?.catalog_protocol !== 1) html += `<p class="empty">当前服务不支持 CLI 会话浏览。</p>`;
  else {
    if (cliList.state === "loading") html += `<p class="note sidebar-note" role="status">正在读取会话目录…</p>`;
    if (cliList.error) html += `<p class="note sidebar-note">${h(cliList.error)}${cliList.items.length ? "；保留上次列表。" : ""}</p><button class="quiet" type="button" data-cli-list-retry>重试读取</button>`;
    if (cliList.warning) html += `<p class="note sidebar-note">${h(cliList.warning)}</p>`;
    const query = search.toLowerCase(), matches = cliList.items.filter(s => !query || `${cliTitle(s)} ${s.workspace} ${s.model}`.toLowerCase().includes(query));
    html += matches.map(s => `<button id="cli-${h(s.catalog_id)}" class="session" type="button" data-cli-session="${h(s.catalog_id)}" aria-pressed="${s.catalog_id === cliSelected}" title="${h(cliTitle(s))}"><span class="session-title">${h(cliTitle(s))}</span><span class="session-bottom"><span>${h(projectName(s.workspace))}</span><span>${h(terminalLabel(s))}</span></span></button>`).join("");
    if (!matches.length && cliList.state === "ready") html += `<p class="empty">${search ? "已加载的 CLI 会话中没有匹配项。" : "此工作目录范围内还没有可读取的 CLI 会话。"}</p>`;
    if (cliList.cursor !== null) html += `<button class="quiet" type="button" data-cli-list-more ${cliList.state === "loading" ? "disabled" : ""}>加载更多（${cliList.items.length} / ${h(cliList.total)}）</button>`;
    if (search && cliList.cursor !== null) html += `<p class="note sidebar-note">搜索仅覆盖已加载的会话。</p>`;
  }
  region($("sessions"), html);
}
async function loadCLIList(conn, more = false) {
  if (connection !== conn || conn.health?.catalog_protocol !== 1 || cliList.state === "loading") return;
  const state = cliList, cursor = more ? state.cursor : 0; if (cursor === null) return;
  state.state = "loading"; state.error = ""; if (source === "cli" && cliListMode) renderCLIList();
  try {
    const page = await conn.client.get(`/catalog/sessions?cursor=${cursor}&limit=100`, conn.controller.signal);
    if (connection !== conn || cliList !== state) return;
    if (!Array.isArray(page.sessions) || !Number.isInteger(page.total) || !(page.next_cursor === null || Number.isInteger(page.next_cursor))) throw new Error("CLI 会话目录格式无法识别。");
    const items = new Map((more ? state.items : []).map(s => [s.catalog_id, s]));
    for (const item of page.sessions) items.set(item.catalog_id, item);
    state.items = [...items.values()]; state.cursor = page.next_cursor; state.total = page.total;
    state.warning = page.warning || ""; state.state = "ready";
  } catch (error) {
    if (connection !== conn || cliList !== state || conn.controller.signal.aborted) return;
    state.state = "error"; state.error = error.message;
  } finally { if (connection === conn && cliList === state && ["cli","workbench"].includes(source)) render(); }
}
function chooseCLI(id) {
  const item = cliList.items.find(s => s.catalog_id === id);
  if (!item) return;
  // 已在应用里继续的那一段，直接去它对应的会话
  if (item.session_id && sessions.some(s => s.session_id === item.session_id)) { choose(item.session_id); return; }
  prepareNavigation(); if (source !== "cli") { saveDraft(); eventController?.abort(); $("create-panel").hidden = true; }
  source = "cli"; cliSelected = id; $("thread-scroll").scrollTop = 0; notice(); render();
  rememberNavigation();
  if (connection) loadCLIHistory(connection, id);
}
async function loadCLIHistory(conn, id, more = false) {
  if (connection !== conn || conn.health?.catalog_protocol !== 1) return;
  if (!cliViews.has(id)) cliViews.set(id, {items: [], state: "initial", cursor: null, total: 0});
  const state = cliViews.get(id);
  if (state.state === "loading") { if (!more) state.reload = true; return; }
  const cursor = more ? state.cursor : 0; if (cursor === null) return;
  state.state = "loading"; state.error = ""; if (source === "cli" && cliSelected === id) { renderThread(); renderTakeover(); }
  try {
    const page = await conn.client.get(`/catalog/sessions/${encodeURIComponent(id)}/history?cursor=${cursor}&limit=100`, conn.controller.signal);
    if (connection !== conn || cliViews.get(id) !== state) return;
    if (!Array.isArray(page.messages) || page.revision == null || !Number.isInteger(page.total) || !(page.next_cursor === null || Number.isInteger(page.next_cursor))) throw new Error("CLI 历史快照格式无法识别。");
    if (more && state.revision !== page.revision) state.reload = true;
    else {
      const items = new Map((more ? state.items : []).map(m => [m.id, m]));
      for (const item of page.messages) items.set(item.id, item);
      state.items = [...items.values()]; state.cursor = page.next_cursor; state.total = page.total;
      Object.assign(state, {revision: page.revision, title: page.title, workspace: page.workspace, persisted: page.persisted, warning: page.warning || "", locked: page.locked === true, canFork: page.can_fork === true, forkReason: page.fork_reason || "", canContinue: page.can_continue === true, continueReason: page.continue_reason || "", sessionId: page.session_id || "", inScope: page.in_scope !== false});
    }
    state.state = "ready";
  } catch (error) {
    if (connection !== conn || cliViews.get(id) !== state || conn.controller.signal.aborted) return;
    state.state = "error"; state.error = error.message;
  } finally {
    if (connection === conn && cliViews.get(id) === state) {
      if (state.reload) { state.reload = false; loadCLIHistory(conn, id); }
      if (source === "cli" && cliSelected === id) { renderThread(); renderTakeover(); }
    }
  }
}
function renderCLIThread() {
  const row = cliList.items.find(s => s.catalog_id === cliSelected), view = cliViews.get(cliSelected);
  $("title").textContent = view?.title || (row ? cliTitle(row) : "Xiaoyu CLI 会话");
  $("subtitle").textContent = row || view ? `${view?.workspace || row?.workspace || connection?.health?.root || "未知工作目录"} · 终端会话` : `浏览范围：${connection?.health?.root || "尚未连接"}`;
  $("session-state").innerHTML = `<span class="badge${view?.locked ? " warning" : ""}">${h(view ? terminalLabel({locked:view.locked,session_id:view.sessionId}) : "终端")}</span>`;
  if (!cliSelected) { region($("thread"), `<div class="empty"><img class="empty-mark" src="./logo.png" width="64" height="64" alt=""><h2>把终端里的工作接到羽案。</h2><p>在左侧选择一段终端会话，先查看，再决定在这里继续还是另开分支。</p><p class="note">查看不会占用它；继续期间终端不能同时打开同一场会话。</p></div>`); return; }
  let html = "";
  if (!view || view.state === "initial" || view.state === "loading" && !view.items.length) html += `<p class="loading-line" role="status">正在读取 CLI 历史…</p>`;
  if (view?.error) html += `<div class="callout warning"><p>CLI 历史读取失败：${h(view.error)}${view.items.length ? "；保留上次快照，暂不可继续或另开分支。" : ""}</p><button type="button" data-cli-history-retry>重新读取</button></div>`;
  if (view?.warning) html += `<p class="note history-note">${h(view.warning)}</p>`;
  if (view?.persisted === false) html += `<p class="note history-note">这份历史未确认完整持久化，当前不可用于继续执行。</p>`;
  if (view?.state === "ready" && !view.items.length) html += `<p class="empty">此历史中没有可显示的用户或助手正文。</p>`;
  html += historyConversation(view?.items || []).map(m => `<section class="message ${m.kind}">${m.kind === "user" ? h(m.text) : renderText(m.text)}</section>`).join("");
  if (view?.cursor != null) html += `<div class="history-pagination"><span>已显示 ${view.items.length} / ${h(view.total)} 条历史</span><button type="button" data-cli-history-more ${view.state === "loading" ? "disabled" : ""}>${view.state === "loading" ? "正在加载…" : "加载后续记录"}</button></div>`;
  html += `<p class="note history-note">这是读取时的历史快照。在这里继续或另开分支后，运行配置取自羽案，来源的授权与模型设置不会带过来；两边仍共用同一个工作目录。</p>`;
  region($("thread"), html);
}
function approvalPurpose(request, events) {
  if (request.identity) return request.purpose || "成员未提供本次调用理由；请按成员身份、工具与参数判断。";
  const index = events.findIndex(e => e.kind === "permission.requested" && e.request_id === request.request_id);
  if (index < 0) return "当前事件窗口没有提供这次调用的理由。";
  for (let i = index - 1; i >= 0; i--) {
    const e = events[i];
    if (e.kind === "permission.requested" || e.kind === "run.started") break;
    if (e.kind === "tool.purpose" && e.name === request.tool) return e.purpose;
  }
  return "模型未提供调用理由；请按工具与参数判断。";
}
async function openSearchSource(sid,ref){
  choose(sid);if(!ref?.messageId)return;
  historyTarget={sid,id:ref.messageId};const conn=connection;await loadArchive(conn,sid);
  for(let count=0;count<100&&connection===conn&&selected===sid;count++){
    const state=archives.get(sid)?.history;if(state?.state==="loading"){await new Promise(r=>setTimeout(r,50));continue;}
    const target=[...$("thread").querySelectorAll("[data-message-id]")].find(n=>n.dataset.messageId===ref.messageId);
    if(target){for(let node=target.parentElement;node&&node!==$("thread");node=node.parentElement)if(node.tagName==="DETAILS")node.open=true;target.scrollIntoView({block:"center"});target.classList.add("search-target");notice("已定位搜索命中的历史消息。");return;}
    if(!state?.cursor||state?.state==="error")break;await loadPage(conn,sid,"history",true);
  }
  notice("已打开来源会话；该消息尚未载入或历史已变化，可继续加载较早记录核对。");
}
function archiveFor(sid) {
  if (!archives.has(sid)) archives.set(sid, {
    history: {items: [], state: "initial", cursor: null, total: 0},
    runs: {items: [], state: "initial", cursor: null, total: 0},
  });
  return archives.get(sid);
}
async function loadArchive(conn, sid, force = false) {
  if (connection !== conn || conn.health?.history_protocol !== 1) return;
  const s = sessions.find(s => s.session_id === sid); if (!s) return;
  const archive = archiveFor(sid), revision = `${s.turns}:${s.busy}:${s.model}:${s.lost_turn?.turn || ""}`;
  if (!force && archive.revision === revision) return;
  archive.revision = revision;
  await Promise.allSettled([loadPage(conn, sid, "history"), loadPage(conn, sid, "runs")]);
}
async function loadPage(conn, sid, kind, more = false) {
  if (connection !== conn) return;
  const archive = archiveFor(sid), state = archive[kind];
  if (state.state === "loading") { if (!more) state.reload = true; return; }
  const cursor = more ? state.cursor : 0; if (cursor === null) return;
  state.state = "loading"; state.error = "";
  if (selected === sid) renderThread();
  try {
    const page = await conn.client.get(`${sidPath(sid)}/${kind}?cursor=${cursor}&limit=${kind === "history" ? "100&include_activity=true" : 50}`, conn.controller.signal);
    if (connection !== conn || archives.get(sid) !== archive) return;
    const rows = kind === "history" ? page.messages : page.runs;
    if (!Array.isArray(rows) || !Number.isInteger(page.total) || !(page.next_cursor === null || Number.isInteger(page.next_cursor))) throw new Error("历史读取接口返回了无法识别的数据。");
    if (kind === "history" && !Number.isInteger(page.live_from_seq)) throw new Error("历史快照没有提供实时事件边界。");
    if (more && kind === "history" && page.live_from_seq !== state.liveFrom) {
      state.reload = true;
    } else {
      const merged = new Map((more ? state.items : []).map(item => [item.id, item]));
      for (const item of rows) merged.set(item.id, item);
      state.items = [...merged.values()]; state.cursor = page.next_cursor; state.total = page.total;
      state.warning = page.warning || ""; state.persisted = page.persisted;
      if (kind === "history") state.liveFrom = page.live_from_seq;
    }
    state.state = "ready";
  } catch (error) {
    if (connection !== conn || archives.get(sid) !== archive || conn.controller.signal.aborted) return;
    state.state = error.status === 404 ? "unsupported" : "error"; state.error = error.message;
  } finally {
    if (connection === conn && archives.get(sid) === archive) {
      if (state.reload) { state.reload = false; loadPage(conn, sid, kind); }
      if (selected === sid) renderThread();
    }
  }
}
function historyControls(state, kind) {
  const label = kind === "history" ? "对话历史" : "历轮结算";
  if (!state || state.state === "initial" || state.state === "loading" && !state.items.length) return `<p class="loading-line" role="status">正在读取${label}…</p>`;
  let html = state.error ? `<div class="callout warning"><p>${h(label)}读取失败：${h(state.error)}${state.items.length ? "；下方保留上次读取的数据，可能已过期。" : "；不能确认是否存在更早记录。"}</p><button type="button" data-history-retry="${kind}">重新读取</button></div>` : "";
  if (state.warning) html += `<p class="note history-note">${h(state.warning)}</p>`;
  if (state.persisted === false) html += `<p class="note history-note">这份${label}尚未持久化，重启后不保证保留。</p>`;
  if (state.cursor !== null) html += `<div class="history-pagination"><span>已显示 ${state.items.length} / ${h(state.total)} 条${label}</span><button type="button" data-history-more="${kind}" ${state.state === "loading" ? "disabled" : ""}>${state.state === "loading" ? "正在加载…" : "加载后续记录"}</button></div>`;
  return html;
}
function artifactCard(a, context = {}) {
  const ref = {session_id: selected, artifact_id: a.id, ...context}, key = JSON.stringify(ref);
  if (a.id) artifactRefs.set(key, ref);
  return `<article class="artifact"><div class="artifact-name">${h(a.path)}</div><div class="artifact-meta">${h(a.size)} 字节 · ${a.status === "complete" ? "轮次正常结束" : "轮次未完整结束"}</div>${a.id && (ref.run_id || ref.batch_id && ref.item_id || ref.contest_id && ref.candidate_id) && connection?.health?.artifact_protocol === 1 ? `<button type="button" class="quiet" data-artifact-inspect="${h(key)}">${artifactViews.has(key) ? "重新检查成果" : "检查成果"}</button>` : ""}${renderArtifactInspection(artifactViews.get(key), {canReveal: desktop && !!window.webkit?.messageHandlers?.revealArtifact})}<details><summary>文件校验信息</summary><pre>${h(a.sha256)}</pre></details></article>`;
}
function renderRuns(state) {
  const labels = {running:"执行中",completed:"已完成",interrupted:"已停止",stopped:"未完整结束",failed:"执行失败",interrupted_by_restart:"重启中断"};
  let html = `<section class="run-history" aria-label="历轮成果与结算"><h3>历轮成果与结算</h3>${historyControls(state, "runs")}`;
  for (const run of state?.items || []) {
    const artifacts = run.result?.artifacts || [];
    const stopped = {budget:"预算已用尽",turn_cap:"达到轮数上限",truncated:"输出被截断",output_missing:"未返回要求的结构化结果"}[run.result?.stopped];
    html += `<details class="run" data-key="run-${h(run.id)}"><summary><span>第 ${h(run.turn)} 轮</span><span>${h(stopped || labels[run.status] || run.status)}${artifacts.length ? ` · ${artifacts.length} 个文件` : ""}</span></summary><div class="run-body">${run.error ? `<p class="note">${h(run.error)}</p>` : ""}${run.result?.text ? `<div class="message assistant">${renderText(run.result.text)}</div>` : `<p class="note">${run.status === "running" ? "本轮尚未结算。" : "本轮没有可读取的结算正文。"}</p>`}${artifacts.map(a => artifactCard(a, {run_id: run.id})).join("")}</div></details>`;
  }
  if (state?.state === "ready" && !state.items.length) html += `<p class="note">尚无已记录的轮次。</p>`;
  if (state?.items.some(r => r.result?.artifacts?.length)) html += `<p class="note">路径相对工作目录；元数据反映交付时状态。轮次正常结束不代表文件内容已验收。</p>`;
  return `${html}</section>`;
}
async function loadWeaves(conn, sid, more = false) {
  if (connection !== conn || conn.health?.weave_protocol !== 1) return;
  if (!weaves.has(sid)) weaves.set(sid, {items: [], state: "initial", cursor: null, total: 0});
  const state = weaves.get(sid);
  if (state.state === "loading") { if (!more) state.reload = true; return; }
  const cursor = more ? state.cursor : 0; if (cursor === null) return;
  state.state = "loading"; state.error = "";
  try {
    const limit = more ? 20 : Math.max(20, Math.min(64, state.items.length));
    const page = await conn.client.get(`${sidPath(sid)}/weaves?cursor=${cursor}&limit=${limit}`, conn.controller.signal);
    if (connection !== conn || weaves.get(sid) !== state) return;
    if (!Array.isArray(page.batches) || !Number.isInteger(page.total) || !(page.next_cursor === null || Number.isInteger(page.next_cursor))) throw new Error("批量执行任务快照格式无法识别。");
    const items = new Map((more ? state.items : []).map(b => [b.batch_id, b]));
    for (const batch of page.batches) items.set(batch.batch_id, batch);
    state.items = [...items.values()]; state.total = page.total; state.cursor = page.next_cursor;
    state.warning = page.warning || ""; state.persisted = page.persisted; state.state = "ready";
    await weaveControls.loadReceipts(conn, sid);
  } catch (error) {
    if (connection !== conn || weaves.get(sid) !== state || conn.controller.signal.aborted) return;
    state.state = "error"; state.error = error.message;
  } finally {
    if (connection === conn && weaves.get(sid) === state) {
      if (state.reload) { state.reload = false; loadWeaves(conn, sid); }
      loadWeaveCapabilities(conn, sid);
      if (source === "workbench" && selected === sid) renderThread();
    }
  }
}

function emptyWeaveDraft() { return {mode:"new", spec:"", prompt_template:"请处理以下事项：\n{{item}}", items:[{id:crypto.randomUUID(),text:""},{id:crypto.randomUUID(),text:""}]}; }
const weaveDraftKey = sid => weaveForms.get(sid)?.draftKey || `weave-compose:${preparationDraftIdentity(sid)}`;
function weaveForm(sid) {
  if (!weaveForms.has(sid)) {
    let draft = emptyWeaveDraft(), error = "";
    const saved = connection?.localState?.getDraft(weaveDraftKey(sid));
    if (saved) try {
      const value = JSON.parse(saved);
      if (value.mode === "new" && Array.isArray(value.items) && value.items.length <= 64 && value.items.every(i => typeof i.id === "string" && typeof i.text === "string") && typeof value.prompt_template === "string" && typeof value.spec === "string"
          || value.mode === "resume" && typeof value.source_batch_id === "string" && Array.isArray(value.item_ids) && value.item_ids.every(i => typeof i === "string") && typeof value.instruction === "string") draft = value;
      else throw new Error();
    } catch { error = "已保存的批量任务草稿无法识别，未覆盖原数据。编辑后将保存为新的草稿。"; }
    weaveForms.set(sid, {draftKey:weaveDraftKey(sid),open:false,draft,error,capabilities:null,selections:new Map(),preview:null,loading:false});
  }
  return weaveForms.get(sid);
}
function persistWeaveDraft(sid, state) {
  state.preview = null; state.previewBody = null;
  connection?.localState?.setDraft(weaveDraftKey(sid), JSON.stringify(state.draft));
}
async function loadWeaveCapabilities(conn, sid) {
  if (connection !== conn || conn.health?.weave_command_protocol !== 1) return;
  const state = weaveForm(sid); if (state.capsLoading) return;
  state.capsLoading = true;
  try {
    const caps = await conn.client.get(`${sidPath(sid)}/weaves/capabilities`, conn.controller.signal);
    if (connection !== conn || weaveForms.get(sid) !== state) return;
    if (!Array.isArray(caps.specs) || !Array.isArray(caps.batches)) throw new Error("批量执行能力清单格式无法识别。");
    state.capabilities = caps;
    if (state.error === state.capsError) state.error = "";
    state.capsError = "";
  } catch (error) { if (connection === conn && weaveForms.get(sid) === state) { state.capabilities = null; state.capsError = error.message; state.error = error.message; } }
  finally { state.capsLoading = false; if (connection === conn && source === "workbench" && selected === sid) renderThread(); }
}
function weaveBody(draft) {
  return draft.mode === "resume" ? {source_batch_id:draft.source_batch_id,item_ids:draft.item_ids,instruction:draft.instruction.trim()} : {spec:draft.spec,prompt_template:draft.prompt_template,items:draft.items.map(i => i.text.trim())};
}
async function previewWeave() {
  const conn = connection, sid = selected, state = weaveForm(sid);
  if (!writable() || state.loading) return;
  const body = weaveBody(state.draft), snapshot = JSON.stringify(state.draft), caps = state.capabilities;
  state.error = ""; state.preview = null;
  if (state.draft.mode === "new" && (!body.spec || body.items.length < (caps?.min_items || 2) || body.items.some(i => !i))) { state.error = "请选择织手，并填写至少两项完整任务。"; renderThread(); return; }
  state.loading = true; renderThread();
  try {
    const preview = await conn.client.post(`${sidPath(sid)}/weaves/preview`, body, {signal:conn.controller.signal});
    if (connection !== conn || weaveForms.get(sid) !== state) return;
    if (JSON.stringify(state.draft) !== snapshot) { state.error = "预览期间内容已变化，请重新预览。"; return; }
    if (typeof preview.preview_token !== "string" || !Array.isArray(preview.items)) throw new Error("任务预览格式无法识别，未提交执行。");
    state.preview = preview; state.previewBody = body; state.previewDraft = snapshot;
  } catch (error) { if (connection === conn && weaveForms.get(sid) === state) state.error = error.message; }
  finally { state.loading = false; if (connection === conn && source === "workbench" && selected === sid) renderThread(); }
}
function clearWeaveDraft(conn, action) {
  if (action.kind !== "weave" || action.state !== "accepted" || !action.draftKey || typeof action.draftText !== "string") return;
  if (conn.localState?.getDraft(action.draftKey) === action.draftText) conn.localState.deleteDraft(action.draftKey);
  const state = weaveForms.get(action.sid);
  if (state && JSON.stringify(state.draft) === action.draftText) { state.draft = emptyWeaveDraft(); state.open = false; state.preview = null; }
}
async function inspectArtifact(key) {
  const conn = connection, ref = artifactRefs.get(key); if (!conn || !ref) return;
  const state = artifactViews.get(key) || {key}; if (state.loading) return;
  artifactViews.set(key,state); state.loading = true; state.error = ""; renderThread();
  const query = new URLSearchParams(ref.run_id ? {run_id:ref.run_id} : ref.batch_id ? {batch_id:ref.batch_id,item_id:ref.item_id} : ref.contest_id ? {contest_id:ref.contest_id,candidate_id:ref.candidate_id} : {});
  try {
    const data = await conn.client.get(`${sidPath(ref.session_id)}/artifacts/${encodeURIComponent(ref.artifact_id)}?${query}`,conn.controller.signal);
    if (connection === conn && artifactViews.get(key) === state){state.data=data;
      const node=document.createElement("section");node.innerHTML=renderArtifactInspection(state,{canReveal:desktop});node.addEventListener("click",e=>{const n=e.target.closest("[data-artifact-reveal]");if(n)revealArtifact(n.dataset.artifactReveal);});
      await panes.openContent({id:`artifact:${key}`,kind:"custom",title:data.path||ref.artifact_id,node,replace:true,source:{session_id:ref.session_id}});
    }
  } catch (error) { if (connection === conn && artifactViews.get(key) === state) state.error = error.message; }
  finally { state.loading = false; if (connection === conn && (source === "project" || source === "library" || source === "workbench" && selected === ref.session_id)) renderThread(); }
}
async function revealArtifact(key) {
  const conn = connection, ref = artifactRefs.get(key), state = artifactViews.get(key);
  if (!desktop || !ref || !state?.data?.can_reveal || !window.webkit?.messageHandlers?.revealArtifact) return;
  try {
    const result = await window.webkit.messageHandlers.revealArtifact.postMessage(ref);
    if (connection === conn && (source === "project" || selected === ref.session_id && source === "workbench")) notice(result?.revealed ? "已在 Finder 中显示成果文件。" : "文件定位尚未确认，请重试。");
  } catch (error) { if (connection === conn && (source === "project" || selected === ref.session_id && source === "workbench")) notice(`无法在 Finder 中定位：${error.message || "请重新检查文件状态。"}`); }
}

function renderThread() {
  $("thread").dataset.workflow=source==="workbench"?collaborationPage:"";
  if(source === "archived") {
    $("title").textContent="已归档";$("subtitle").textContent="";$("session-state").textContent="";
    region($("thread"),workspaceController.renderArchived(sessions,{query:archiveQuery,titleOf,date}));return;
  }
  if (["settings","library","spaces","pages","content-project","capabilities","search"].includes(source)) {
    const labels={settings:"设置",library:"作品",spaces:"空间",pages:"页面", "content-project":"项目资料",capabilities:"插件",search:"搜索"};
    $("title").textContent=labels[source];$("subtitle").textContent="";$("session-state").textContent="";
    const c=pageControllers.get(source);
    region($("thread"),source === "settings" ? settingsController.render() : c?.render(c.currentView?.()||source) || `<section class="feature-intro"><h2>${labels[source]}</h2><p>规划中。此入口正在接入真实工作数据。</p><button type="button" data-back-chat>返回工作台</button></section>`);return;
  }
  if (["automations","connections"].includes(source)) { $("title").textContent=source === "automations" ? "定时任务" : "连接器"; $("subtitle").textContent=source === "automations" ? "应用打开期间的任务调度与运行核对" : "配置、会话挂载与授权状态"; $("session-state").textContent=""; region($("thread"),source === "automations" ? automations.render() : connections.render()); return; }
  if (source === "project") { renderProjectThread(); return; }
  if (source === "cli") { renderCLIThread(); return; }
  const s = session();
  const workflow=WORKFLOW_CHOICES.find(w=>w.id===collaborationPage);
  $("title").textContent = workflow?.label || (s ? titleOf(s) : "新任务");
  $("subtitle").textContent = workflow && s ? projectName(s.workspace) : fixture ? "脚本模型示例 · 不调用真实模型" : "";
  $("session-state").innerHTML = s && collaborationPage ? badge(s) : "";
  if (!s) { region($("thread"),selected?`<section class="empty"><h2>会话暂不可用</h2><p>此历史会话当前不在服务列表中。草稿仍保留，请刷新状态或选择其他会话。</p></section>`:welcomeHTML()); renderWelcomeStarters(); return; }
  const view = views.get(s.session_id), events = view?.events || [];
  const archive = archives.get(s.session_id), history = archive?.history, runs = archive?.runs;
  const hasHistory = Number.isInteger(history?.liveFrom);
  const liveEvents = hasHistory ? events.filter(e => e.seq >= history.liveFrom) : events;
  const blocks = liveTimeline(liveEvents), saved = history?.items || [];
  const form = connection?.health?.weave_command_protocol === 1 ? weaveForm(s.session_id) : null;
  let html = desktop ? workspaceController.renderStatus() : "";
  if(collaborationPage==="weave") html += renderWeaveForm(form, form?.capabilities, !writable() || !sessionActionAllowed(s) || s.busy || !!connection?.outbox?.items[scopeFor(s.session_id,"weave")]);
  if (collaborationPage==="fleet" && connection?.health?.fleet_command_protocol === 1) html += fleetCommands.render(s.session_id);
  if (collaborationPage==="contest" && connection?.health?.contest_protocol === 1) html += contests.form(s.session_id);
  if(!collaborationPage) {
    if (connection?.health?.history_protocol === 1) html += historyControls(history, "history");
    else html += `<p class="note history-note">当前服务不提供完整历史与历轮结算接口，仅显示最近事件。升级服务后可读取持久化记录。</p>`;
    if ((view?.gap || view?.clipped) && (!hasHistory || liveEvents.length)) html += `<p class="note">实时事件窗口存在缺口或截断；${hasHistory ? "已保存的对话记录仍显示在下方。" : "当前内容不是完整对话记录。"}</p>`;
  }
  if (s.writable === false) html += `<div class="callout warning"><h3>此会话暂不能继续执行</h3><p>${h(s.write_blocked_reason || "请核对服务状态后继续。")}</p><p>草稿和成果仍保留，可以停止执行或拒绝审批。</p></div>`;
  if (s.error) html += `<div class="callout error"><h3>执行失败</h3><p>${h(s.error)}</p></div>`;
  if (s.lost_turn) html += `<div class="callout warning"><h3>第 ${h(s.lost_turn.turn)} 轮被重启中断</h3><p>请先检查已有文件和成果，再决定下一步。服务不会自动重跑。</p></div>`;
  if (s.budget_reason) html += `<div class="callout warning"><h3>预算已用尽</h3><p>${h(s.budget_reason)}</p></div>`;
  if (s.questionError) html += `<div class="callout warning">${h(s.questionError)}；待答状态未知。</div>`;
  if(!collaborationPage) {
    messageCopies.clear();
    const messageHTML=(m,key)=>{messageCopies.set(key,m.text||"");return conversationMessage(m,key,{copyable:!s.busy || m.kind==="user" || !blocks.length});};
    html += timelineHTML(historyTimeline(saved),messageHTML,{runs:runs?.items||[]});
    if (hasHistory && history.cursor !== null && blocks.length) html += `<p class="note history-note">尚有历史记录未加载；以下为当前轮次。</p>`;
    html += timelineHTML(blocks,messageHTML,{busy:s.busy});
    if (!blocks.length && !saved.length && !s.last_result && history?.state !== "loading") html += `<div class="empty"><h2>${s.turns ? "接着这件事继续。" : "准备好了。"}</h2><p>${s.turns ? "下方可以继续发送新的指令。" : "在下方写下任务，小羽会在这个工作目录里开始。"}</p></div>`;
  }
  if (s.busy && (collaborationPage || !blocks.some(b=>b.kind==="tool" && ["pending","running"].includes(b.status)))) {
    const progress = [...events].reverse().find(e => ["tool.running", "tool.progress", "request.started", "steer.accepted"].includes(e.kind));
    html += `<p class="progress" role="status">${h(stateOf(s).label)}${progress?.name ? ` · ${h(progress.name)}` : ""}${progress?.kind === "steer.accepted" ? " · 插话已被模型接纳" : ""}</p>`;
  }
  if(collaborationPage && s.busy) html+=`<button type="button" data-workflow-stop ${!writable() || !s.in_flight?.turn || pendingFor(selected).some(([,a])=>a.kind==="abort")?"disabled":""}>停止当前执行</button>`;
  for (const request of s.pending_approvals || []) {
    const scope = scopeFor(s.session_id, "approval", request.request_id);
    const disabled = !writable() || !!connection.outbox.items[scope];
    const decisions = request.identity ? request.allowed_decisions || [] : ["allow","deny"];
    html += `<form class="callout warning" data-approval="${h(request.request_id)}" data-sid="${h(s.session_id)}"><h3>${request.identity?.kind === "qixiang" ? "批量执行单项需要批准" : request.identity ? "成员需要批准" : "需要批准"} · ${h(request.tool)}</h3>${renderApprovalIdentity(request.identity)}<p class="note">${h(approvalPurpose(request, events))}</p><details open data-key="approval-${h(request.request_id)}"><summary>查看完整调用参数</summary><pre>${json(request.args)}</pre></details><label>说明（可选）<textarea id="reason-${h(request.request_id)}" name="reason" rows="2" placeholder="拒绝原因或执行时的补充说明" ${disabled ? "disabled" : ""}></textarea></label><div class="actions"><button class="primary" type="submit" value="allow" ${disabled || !sessionActionAllowed(s) || !decisions.includes("allow") ? "disabled" : ""}>单次允许</button><button type="submit" value="deny" ${disabled || !decisions.includes("deny") ? "disabled" : ""}>拒绝</button></div></form>`;
  }
  for (const q of s.questionItems || []) {
    const scope = scopeFor(s.session_id, "answer", q.question_id);
    const editable = ["open", "pending"].includes(q.state), disabled = !writable() || !!connection.outbox.items[scope];
    html += `<form class="callout" data-question="${h(q.question_id)}" data-sid="${h(s.session_id)}"><h3>${{open:"需要你的回答",pending:"需要你的回答",queued:"答案已排队",answered:"模型已接纳答案",cancelled:"问题已取消"}[q.state] || h(q.state)}</h3>`;
    q.items.forEach((item, i) => {
      html += `<p>${h(item.question)}</p>`;
      if (editable) {
        html += (item.options || []).map((o, j) => `<label class="option"><input id="q-${h(q.question_id)}-${i}-${j}" type="${item.multi_select ? "checkbox" : "radio"}" name="choice-${i}" value="${h(o.label)}" ${disabled ? "disabled" : ""}>${h(o.label)}${o.description ? ` — ${h(o.description)}` : ""}</label>`).join("");
        html += `<label>补充或自由回答<textarea id="answer-${h(q.question_id)}-${i}" name="custom-${i}" rows="2" maxlength="4000" ${disabled ? "disabled" : ""}></textarea></label><label class="option"><input id="skip-${h(q.question_id)}-${i}" type="checkbox" name="skip-${i}" ${disabled ? "disabled" : ""}>明确跳过此题</label>`;
      } else {
        const a = q.answers?.find(a => a.item_id === item.item_id);
        if (a) html += `<p class="note">${h(a.skipped ? "已跳过" : [...(a.selected || []), a.custom].filter(Boolean).join("；"))}</p>`;
      }
    });
    if (editable) html += `<div class="actions"><button type="submit" class="primary" ${disabled ? "disabled" : ""}>提交答案</button></div>`;
    if (q.state === "queued") html += `<p class="note">${s.busy ? "将在安全步骤边界接纳。" : "会话空闲，回答不会自动启动模型。"}</p>${!s.busy ? `<button type="button" data-continue="${h(s.session_id)}" ${!writable() || pendingFor(s.session_id).some(([,a]) => a.kind === "prompt") ? "disabled" : ""}>根据答案继续</button>` : ""}`;
    html += `</form>`;
  }
  if(collaborationPage==="weave") html += renderWeaves(weaves.get(s.session_id), {capabilities:form?.capabilities,selections:form?.selections,canSubmit:writable() && !s.busy,itemControls:(batch,item) => weaveControls.render(s.session_id,batch,item),artifactCard:(a,context) => artifactCard(a,context)});
  if (collaborationPage==="contest" && connection?.health?.contest_protocol === 1) html += contests.results(s.session_id);
  if (collaborationPage==="fleet" && connection?.health?.fleet_protocol === 1) html += fleets.render(s.session_id,{hideEmpty:true});
  if(collaborationPage && !html.trim()) html=`<button type="button" data-workflow-page="${h(collaborationPage)}">${icon("plus")}配置${h(workflow.label)}</button>`;
  if(!collaborationPage) {
    const activities=WORKFLOW_CHOICES.filter(w=>w.id==="weave" ? weaves.get(s.session_id)?.items?.length || weaves.get(s.session_id)?.error : w.id==="contest" ? contests.hasRecords(s.session_id) : w.id==="fleet" ? fleets.hasRecords(s.session_id) : false);
    if(activities.length)html+=`<nav class="workflow-results" aria-label="相关任务">${activities.map(w=>`<button type="button" class="quiet" data-workflow-page="${w.id}">${icon(w.symbol)}查看${w.label}</button>`).join("")}</nav>`;
    const result = s.last_result;
    if (!hasHistory && result?.text && (!blocks.some(b => b.kind === "assistant") || view?.gap || view?.clipped)) html += `<section class="message assistant"><h3>${s.busy ? "上一轮结算" : "最近结算"}</h3>${renderText(result.text)}</section>`;
    const seenArtifacts = new Map();
    for (const a of [...events.filter(e => e.kind === "artifacts.ready").flatMap(e => e.artifacts || []), ...(result?.artifacts || [])]) seenArtifacts.set(`${a.path}:${a.sha256}`, a);
    const artifacts = [...seenArtifacts.values()];
    let executionDetails=connection?.health?.history_protocol===1 && (runs?.items?.length || runs?.error)?renderRuns(runs):"";
    if (artifacts.length) html += `<h3>文件成果 · 最近事件与结算</h3>${artifacts.map(a=>{const run=runs?.items?.find(r=>r.result?.artifacts?.some(item=>item.id===a.id));return artifactCard(a,run?{run_id:run.id}:{});}).join("")}<p class="note">路径相对工作目录；仅显示交付时的文件信息，尚未验收文件内容。</p>`;
    if(events.length || s.turns) executionDetails += `<details class="technical" data-key="technical"><summary>会话与执行详情</summary><div class="facts"><span>${h(s.workspace)}</span><span>${h(s.session_id)}</span><span>模式 ${h(s.mode)} · ${h(s.turns)} 轮 · ${h(s.spend?.tokens ?? 0)} tokens</span><span>更新于 ${date(s.updated_at)}</span></div><p class="note">最近最多 200 条实时事件；事件可能含截断字段。历史正文和历轮结算由独立接口读取。</p><div class="events">${events.filter(e => e.kind !== "text.delta").map(e => `<details class="event" data-key="event-${e.seq}"><summary>#${e.seq} ${h(e.kind)} ${h(e.name || "")}</summary><pre>${json(e)}</pre></details>`).join("")}</div></details>`;
    region($("execution-detail-body"),executionDetails || '<p class="note">暂无运行记录。</p>');
  }
  const scroll = $("thread-scroll"), bottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 100;
  region($("thread"), html);
  if (bottom && !collaborationPage) scroll.scrollTop = scroll.scrollHeight;
}

async function refresh(conn) {
  if (connection !== conn || conn.loading) return;
  conn.loading = true; clearTimeout(conn.timer);
  try {
    const health = await conn.client.get("/health", conn.controller.signal);
    const response = await conn.client.get("/session", conn.controller.signal);
    if (connection !== conn) return;
    if (!Array.isArray(response.sessions)) throw new Error("服务没有返回会话列表。");
    const identity = JSON.stringify([conn.client.base, health.root, health.state_dir]);
    if(conn.localState&&conn.outbox&&!conn.outbox.running.size) {
      const synced=await syncViewReceipts(conn.localState,conn.outbox);
      if(connection!==conn)return;
      if(synced)conn.outbox=synced;
    }
    if (!conn.outbox) {
      conn.outbox = new Outbox(conn.localState?.storage || sessionStorage, conn.localState ? "native" : identity);
      conn.restorePending = true;
    }
    if (conn.restorePending) {
      const created = conn.outbox.items.create;
      if (created?.state === "accepted" && ownsAction(created) && created.result?.session_id) {
        selected=created.result.session_id;
        if(created.draftText&&conn.localState){const existing=conn.localState.getDraft(selected);if(!existing||existing===created.draftText){conn.localState.setDraft(selected,created.draftText);drafts.set(selected,created.draftText);if(source==="workbench")$("message").value=created.draftText;await conn.localState.flush();}else notice("新会话已经创建；首条任务原文仍保存在新任务草稿中，请核对后再发送。");}
      }
      for (const [scope, a] of Object.entries(conn.outbox.items)) if (!scope.startsWith("side:") && a.state === "accepted" && ownsAction(a)) {
        if (conn.localState && reconcileAcceptedDraft(conn.localState, a) && drafts.get(a.sid) === a.body.text) drafts.delete(a.sid);
        clearWeaveDraft(conn, a); contests.accepted(conn, a); fleets.accepted(conn, a); fleetCommands.accepted(conn, a); weaveControls.accepted(conn, a);
        conn.outbox.forget(scope);
      }
      await conn.localState?.flush();
      if (connection !== conn) return;
      conn.restorePending = false;
      rememberNavigation(true);
    }
    if (conn.identity && conn.identity !== identity) throw new Error("服务工作区已变化，请断开后重新连接。未发送任何操作。");
    if (conn.health && conn.health.instance_id !== health.instance_id) {
      views.clear(); archives.clear(); weaves.clear(); weaveForms.clear(); contests.reset(); fleets.reset(); fleetCommands.reset(); weaveControls.reset(); workspaceController.reset(); automations.reset(); connections.reset(); artifactViews.clear(); artifactRefs.clear(); cliViews.clear(); cliList = {items: [], state: "initial", cursor: null, total: 0}; eventController?.abort();
      notice("服务已重启。已重新读取会话和问答状态；未自动重试任何提交。");
    }
    conn.identity = identity; conn.health = health;
    for (const s of response.sessions) {
      s.questionItems = [];
      if (s.questions != null) {
        try {
          const response = await conn.client.get(`${sidPath(s.session_id)}/questions?include_terminal=true`, conn.controller.signal);
          if (!Array.isArray(response.questions)) throw new Error("问题快照格式不正确");
          s.questionItems = response.questions;
        } catch (error) {
          if (conn.controller.signal.aborted || [401,403].includes(error.status)) throw error;
          s.questionError = error.message;
        }
      }
    }
    if (connection !== conn) return;
    if(desktop)await automations.load(conn);
    notifyChanges(response.sessions);
    sessions = response.sessions; conn.healthy = true;
    if (desktop) await workspaceController.load(conn);
    if(desktop && !conn.preferencesLoaded){
      conn.preferencesLoaded=true;await settingsController.load(conn);
      const restored=viewState(native.session_id,native.page);
      if(restored.session_id) {
        if(sessions.some(s=>s.session_id===restored.session_id))choose(restored.session_id,{record:false});
        else {selected=restored.session_id;navigationUnavailable=selected;newTask=false;notice("上次的会话当前不可用。草稿已保留；不会创建替代任务。");}
      }
      inputProject=session()?.project_id||workspaceController.projectForSession(selected)||conn.localState?.getDraft("new-input-project")||workspaceController.projectForWorkspace(health.root)||"";
      const inputEntry=workspaceController.projects().find(p=>p.project_id===inputProject);
      inputWorkspace=session()?.workspace??inputEntry?.workspace??conn.localState?.getDraft("new-input-workspace")??health.root??"";
      if(!selected)$("workspace-path").value=inputWorkspace;
      // Adopt the old single input once, only into this service's root folder.
      const legacy=conn.localState?.getDraft("new-task");
      if(legacy && !newInput(health.root)){newInput(health.root,legacy);conn.localState.deleteDraft("new-task");}
      $("message").value=selected?(drafts.get(selected)??conn.localState?.getDraft(selected)??""):newInput();
      if(restored.page!=="workbench")openPage(restored.page);
      rememberNavigation(true);
    }
    if (connection !== conn) return;
    if(navigationUnavailable&&sessions.some(s=>s.session_id===navigationUnavailable))navigationUnavailable="";
    if (selected && selected!==navigationUnavailable && !sessions.some(s => s.session_id === selected)) {
      if (source === "workbench") choose("",{record:false}); else selected = "";
      rememberNavigation(true);
    }
    if (source === "workbench" && !selected && !newTask && sessions.length) { const candidate = workspaceController.filterSessions(sessions.filter(hasRecentActivity))[0]; if (candidate) {choose(candidate.session_id,{record:false});rememberNavigation(true);} }
    if(desktop && conn.capabilitiesInstance!==health.instance_id){conn.capabilitiesInstance=health.instance_id;capabilitiesController.load();}
    $("workspace-hint").textContent = `允许范围：${health.root || "未知"}。目录须已存在；终端会话可在清单里选择后明确继续。`;
    if (!$("workspace-path").value) $("workspace-path").value = health.root || "";
    if (!conn.didConnect) { $("connection-panel").hidden = true; conn.didConnect = true; }
    $("disconnect").hidden = desktop;
    region($("connection-summary"), `<span>服务地址：${h(conn.client.base)}</span><span>工作区：${h(health.root || "未知")}</span><span>持久化：${health.persist ? "已启用" : "未启用"} · 完整历史：${health.history_protocol === 1 ? "可用" : "当前服务不支持"}</span>`);
    status("已连接", "online");
    connectionBanner(conn);
    render();
    if (source === "workbench" && selected && !navigationUnavailable) { loadArchive(conn, selected); loadWeaves(conn, selected); contests.load(conn, selected); fleets.load(conn, selected); fleetCommands.load(conn, selected); }
    resumePendingWorkflow(conn);
    if (source === "workbench" && selected && !navigationUnavailable && (!eventController || eventController.signal.aborted)) watch(conn, selected);
    if (source === "automations") automations.load(conn);
    if (source === "connections") connections.load(conn);
    if (cliList.state === "initial") loadCLIList(conn);
    if (source === "cli" && cliSelected && !cliViews.has(cliSelected)) loadCLIHistory(conn, cliSelected);
  } catch (error) {
    if (connection !== conn || conn.controller.signal.aborted) return;
    conn.healthy = false; status("连接异常", "offline");
    banner(`${error.message} 当前显示的数据可能已过期；操作已暂停。${conn.storageError ? `\n草稿保存仍未成功：${conn.storageError}` : ""}`); render();
  } finally {
    if (connection === conn) { conn.loading = false; $("refresh").disabled = false; conn.timer = setTimeout(() => refresh(conn), 3000); }
  }
}
async function watch(conn, sid) {
  eventController?.abort();
  const controller = new AbortController(); eventController = controller;
  const stop = () => controller.abort(); conn.controller.signal.addEventListener("abort", stop, {once:true});
  try {
    while (connection === conn && source === "workbench" && selected === sid && !controller.signal.aborted) {
      const s = sessions.find(s => s.session_id === sid);
      if (!s) return;
      const old = views.get(sid) || eventWindow(s);
      const page = await conn.client.get(`${sidPath(sid)}/events?from=${old.cursor}&limit=200&wait=20`, controller.signal);
      if (controller.signal.aborted || connection !== conn || source !== "workbench" || selected !== sid) return;
      views.set(sid, mergeEvents(old, page));
      const first = page.events.find(e => e.kind === "run.started");
      if (first && !titles.has(sid)) titles.set(sid, first.prompt.slice(0,60));
      render();
      if (page.events.some(e => e.kind !== "text.delta")) refresh(conn);
    }
  } catch (error) {
    if (!controller.signal.aborted && connection === conn) {
      conn.healthy = false; banner(`进度连接中断：${error.message} 将重新读取状态。`); render();
    }
  } finally {
    controller.abort(); conn.controller.signal.removeEventListener("abort", stop);
  }
}
async function sendAction(scope) {
  const conn = connection;
  if (!writable()) return;
  const a = conn.outbox.items[scope];
  if (!a || conn.outbox.running.has(scope)) return;
  if (!ownsAction(a)) { notice("此提交来自另一窗口，请回到原窗口核对或恢复该窗口后重试。"); return; }
  try {
    const promise = conn.outbox.send(scope, conn.client, conn.controller.signal); render();
    const result = await promise;
    if (connection !== conn || !result) return;
    if (a.kind === "create") {
      $("create-panel").hidden = true;
      if (!sessions.some(s => s.session_id === result.session_id)) sessions.push(result);
      project = ""; filter = "all"; search = ""; $("search").value = "";
      choose(result.session_id);
      if(a.workflow)pendingWorkflow={mode:a.workflow,sid:result.session_id};
      if(a.draftText){$("message").value=a.draftText;saveDraft();await conn.localState?.flush();}
      if(a.body.adopt_from||a.body.fork_from){cliViews.delete((a.body.adopt_from||a.body.fork_from).catalog_id);loadCLIList(conn);}
    }
    if (["prompt","steer"].includes(a.kind)) {
      if (a.kind === "prompt" && !titles.has(a.sid)) titles.set(a.sid, a.body.text.slice(0,60));
      if (drafts.get(a.sid) === a.body.text) drafts.delete(a.sid);
      if (conn.localState) reconcileAcceptedDraft(conn.localState, a);
      const folder=sessions.find(s=>s.session_id===a.sid)?.workspace;
      const pid=sessions.find(s=>s.session_id===a.sid)?.project_id||workspaceController.projectForSession(a.sid);
      if(folder && newInput(folder,undefined,pid)===a.body.text)newInput(folder,"",pid);
      if (selected === a.sid && $("message").value === a.body.text) $("message").value = "";
    }
    clearWeaveDraft(conn, a); contests.accepted(conn, a); fleets.accepted(conn, a); fleetCommands.accepted(conn, a); weaveControls.accepted(conn, a);
    await refreshAfterAction(conn);
    conn.outbox.forget(scope);
    resumePendingWorkflow(conn);
    if(a.kind==="create" && startingDraft && a.draftText===startingDraft){
      const text=startingDraft,sid=result.session_id;startingDraft="";
      drafts.set(sid,text);conn.localState?.setDraft(sid,text);await conn.localState?.flush();
      submit(scopeFor(sid,"prompt"),{kind:"prompt",sid,path:`${sidPath(sid)}/prompt_async`,body:{text}});

    }
    const fleetFailure = ["fleet_message","fleet_stop","weave_message","weave_stop"].includes(a.kind) && ["failed","unknown","not_consumed"].includes(result.operation?.status);
    notice((fleetFailure ? `${result.operation.status === "failed" ? "执行请求失败" : result.operation.status === "not_consumed" ? "原执行已结束，消息未被消费" : "执行请求结果未知"}：${result.operation.reason || result.operation.error || "请查看操作记录并核对成员状态。"}${["fleet_message","weave_message"].includes(a.kind) ? " 原草稿已保留。" : ""}` : a.kind === "answer" ? result.question.state === "answered" ? "模型已接纳答案。" : "答案已排队，尚未被模型接纳。空闲时请明确点击继续。"
      : a.kind === "steer" ? "插话已排队，等待模型在安全步骤边界接纳。"
      : a.kind === "abort" ? result.aborted ? "已请求停止本轮。" : "目标轮次已结束，未影响其他轮次。"
      : a.kind === "approval" ? a.body.decision === "allow" ? "单次允许已提交。" : "已拒绝这次调用。"
      : a.kind === "weave" ? "批量执行任务已接收；新批次的执行状态会显示在任务表中。"
      : a.kind === "contest" ? "方案比选已接收；候选与判官状态会显示在下方。"
      : a.kind === "selection" ? "采纳记录已保存；当前选择以最新记录为准，未自动应用文件改动。"
      : a.kind === "weave_message" ? "单项消息已收到提交回执；是否进入对应执行实例上下文，请查看本项操作记录。"
      : a.kind === "weave_stop" ? "单项停止请求已收到回执；请核对此执行实例是否退出，兄弟任务不受影响。"
      : a.kind === "fleet_command" ? "统筹协作操作已接收；请查看工具审批、执行记录与协作看板。接收不代表操作已完成。"
      : a.kind === "fleet_message" ? "成员消息提交已收到回执；是否已进入成员上下文，请查看成员消息记录。"
      : a.kind === "fleet_stop" ? "成员停止请求已收到回执；请查看该成员是否已结束，工作树与成果不会因此清理。"
      : a.kind === "create" ? a.body.adopt_from ? "已在这里继续这场终端会话。关闭它之前，终端不能同时打开同一场会话。" : a.body.fork_from ? "已从终端历史创建新分支。原历史未修改；新会话仍使用原工作目录。" : "" : "") + (a.kind === "weave_stop" && result.operation?.persisted === false ? ` 停止回执未可靠持久化，不承诺重启后可按原键核对。${result.operation.warning || ""}` : ""));
    if (a.kind === "answer") feedback = {kind:"answer",sid:a.sid,qid:a.qid};
    if (a.kind === "steer") feedback = {kind:"steer",sid:a.sid,text:a.body.text,seq:a.seq};
  } catch (error) {
    if (connection === conn) {
      contests.rejected(a,error); fleets.rejected(a,error); fleetCommands.rejected(a,error); weaveControls.rejected(a,error);
      if (a.kind === "weave" && error.status === 409 && !error.recorded) {
        const form = weaveForms.get(a.sid);
        if (form) { form.preview = null; form.previewBody = null; form.error = "预览已过期或执行状态已变化。核对并清除此回执后，请重新预览再提交。"; }
      }
      notice(error.recorded ? "这次任务已有提交记录，请核对当前会话和文件成果。" : error.message); await refreshAfterAction(conn);
      const origin=a.kind==="create"&&(a.body.adopt_from||a.body.fork_from)?.catalog_id;
      if(origin){conn.outbox.forget(scope);loadCLIList(conn);loadCLIHistory(conn,origin);}
    }
  } finally { if (connection === conn) render(); }
}
async function refreshAfterAction(conn) {
  // Do not race a snapshot that was read before the mutation.
  while (conn.loading && connection === conn) await new Promise(resolve => setTimeout(resolve,20));
  if (connection === conn) await refresh(conn);
}
function submit(scope, action) {
  if (!writable()) return;
  const target = sessions.find(s => s.session_id === action.sid);
  if (!sessionActionAllowed(target, action)) { notice(target.write_blocked_reason || "会话暂不能写入，请核对服务状态。"); return; }
  if (connection.outbox.items[scope] || ["prompt","steer","weave","contest"].includes(action.kind) && pendingFor(action.sid).some(([,a]) => a.kind === "fleet_command")) { renderOutbox(); return; }
  try { connection.outbox.prepare(scope, {...action, ...(viewID ? {view_id:viewID} : {}), instance: connection.health.instance_id, seq: target?.next_seq || 1}); sendAction(scope); }
  catch { banner("无法保存待提交回执，操作没有发送。请检查浏览器会话存储。"); }
}
function submitText(text) {
  if (source !== "workbench") return;
  if(selected&&!session()){notice("此会话当前不可用。草稿已保留，请刷新状态后再提交。");return;}
  if(!session() && text.trim() && writable() && !connection.outbox.items.create){
    startingDraft=text;saveDraft();
    const values=settingsController.values(),model=$("composer-model").value||values.default_model,mode=$("composer-mode").value||values.default_mode;
    submit("create",{kind:"create",path:"/session",body:{...inputProjectOptions(),...(model?{model}:{}),...(mode?{mode}:{}),questions:{foreground_timeout_seconds:60}},draftText:text});return;
  }
  const s = session(); if (!s || !text.trim() || modelSaves.has(s.session_id)) return;
  if(s.busy && s.next_model){notice("新模型将在下一轮使用。请等待当前回复结束，或先停止生成后发送。");return;}
  if (s.busy && ["qixiang","douqiao","chenshu"].includes(s.in_flight?.command?.kind)) return;
  if (pendingFor(s.session_id).some(([,a]) => ["prompt","steer","weave","contest","fleet_command"].includes(a.kind))) return;
  const kind = s.busy ? "steer" : "prompt";
  if (s.busy && !s.in_flight?.turn) return;
  submit(scopeFor(s.session_id,kind), {kind,sid:s.session_id,path:`${sidPath(s.session_id)}/${s.busy ? "steer" : "prompt_async"}`,body:{text,...(s.busy ? {turn:s.in_flight.turn} : {})}});
}
function disconnect() {
  saveDraft();
  if (connection) { clearTimeout(connection.timer); connection.controller.abort(); }
  eventController?.abort(); connection = null;pendingModel="";navigationUnavailable="";startingDraft="";pendingWorkflow="";collaborationPage="";
  settingsController.reset();providersController.reset();dataController.reset();notificationStates.clear();automationNoticeIds=null;automationSessions.clear();for(const c of new Set(pageControllers.values()))c.reset?.();
  inputWorkspace="";inputProject="";memoryInputs.clear();
  sessions = []; selected = ""; project = ""; projectSelected = ""; source = "workbench"; cliSelected = "";
  views.clear(); archives.clear(); drafts.clear(); titles.clear(); cliViews.clear(); weaves.clear(); weaveForms.clear(); contests.reset(); fleets.reset(); fleetCommands.reset(); weaveControls.reset(); workspaceController.reset(); automations.reset(); connections.reset(); artifactViews.clear(); artifactRefs.clear(); cliList = {items: [], state: "initial", cursor: null, total: 0};
  $("message").value = ""; $("token").value = ""; $("workspace-path").value = "";
  $("disconnect").hidden = true; $("connection-panel").hidden = desktop; $("create-panel").hidden = true;
  $("refresh").disabled = true; status("尚未连接"); banner(); notice(); render();
  navigationHistory?.reset(viewSnapshot());
}
$("connect-form").addEventListener("submit", async event => {
  event.preventDefault(); let client;
  try { client = new ReadClient($("endpoint").value.trim(), $("token").value); } catch(error) { banner(error.message); return; }
  disconnect(); const conn = {client,controller:new AbortController(),healthy:false,loading:false}; connection = conn;
  status("正在连接…");
  try {
    if (desktop) {
      conn.localState = scopeViewDrafts(await claimLegacyDrafts(await LocalState.load(client, conn.controller.signal),viewID),viewID);

      conn.localState.onError = error => { if (connection === conn) { conn.storageError = error.message; connectionBanner(conn); } };
      conn.localState.onSaved = () => { if (connection === conn && conn.storageError) { conn.storageError = ""; if (conn.healthy) connectionBanner(conn); } };
    }
    if (connection === conn) refresh(conn);
  } catch (error) { if (connection === conn) { status("恢复失败", "offline"); banner(`无法读取本机草稿与提交回执：${error.message}。未提交任务，请重新打开应用重试。`); } }
});
$("disconnect").addEventListener("click",disconnect);
$("state-conflicts").addEventListener("click",async e=>{const n=e.target.closest("[data-state-conflict]");if(!n||!connection?.localState)return;const conn=connection;try{const result=await resolveLocalConflict(conn.localState,n.dataset.stateGroup,n.dataset.stateKey,n.dataset.stateConflict);if(result.rebuildOutbox)conn.outbox=new Outbox(conn.localState.storage,"native");if(result.reloadDraft){if(result.key===selected){$("message").value=conn.localState.getDraft(selected)||"";drafts.set(selected,$("message").value);}else if(result.key==="new-conversation-inputs"&&!selected)$("message").value=newInput();else{contentController.reset();await contentController.load();}}connectionBanner(conn);await refreshAfterAction(conn);}catch(error){banner(error.message);}});

$("composer-model").addEventListener("change", async () => {
  if (!writable()) return;
  const value = $("composer-model").value, conn = connection, current=session();
  if(current){
    const sid=current.session_id,previous=current.next_model||current.model;
    if(modelSaves.has(sid))return;
    modelSaves.add(sid);renderComposer();
    try{
      const result=await conn.client.post(`/desktop/sessions/${encodeURIComponent(sid)}/model`,{model:value,previous},{instance:conn.health?.instance_id,signal:conn.controller.signal});
      if(connection!==conn)return;
      current.next_model=result.next_model;
      await refreshAfterAction(conn);
      await settingsController.saveDefaultModel(value);
    }catch(error){if(connection===conn)notice(`模型选择尚未确认：${error.message}。请核对输入框中的模型。`);}
    finally{modelSaves.delete(sid);if(connection===conn)renderComposer();}
    return;
  }
  pendingModel = value;
  renderComposer();
  rememberNavigation();
  if (!desktop) return;
  try {
    await settingsController.saveDefaultModel(value);
    if (connection === conn && pendingModel === value) { pendingModel = ""; renderComposer(); }
  } catch (error) {
    if (connection === conn) notice(`本次选择已保留，默认模型尚未保存：${error.message}`);
  }
});

$("settings").addEventListener("click",() => openPage("settings"));
$("return-chat").addEventListener("click",()=>choose(selected));
document.querySelector(".shell").addEventListener("click",e=>{const n=e.target.closest("[data-route]");if(n)openPage(n.dataset.route);});
$("composer-project").addEventListener("click",()=>{$("create-panel").hidden=false;$("workspace-path").value=session()?.workspace||$("workspace-path").value||connection?.health?.root||"";$("workspace-path").focus();});
const composerAdditions=createComposerAdditions({
  root:$("starter-menu"),boundary:$("workspace"),
  context:()=>({conn:connection,sid:selected,workspace:session()?.workspace||$("workspace-path").value||connection?.health?.root,enabled:source==="workbench" && !!writable() && !$("message").disabled && !$("message").readOnly}),
  get:()=>$("message").value,put:setComposer,notice,focus:()=>$("message").focus(),
  pickFiles:window.webkit?.messageHandlers?.workspace ? ()=>window.webkit.messageHandlers.workspace.postMessage({operation:"choose_references"}) : null
});
$("work-mode").addEventListener("change",e=>activateWorkflow(e.target.value));
function resumePendingWorkflow(conn){
  if(connection===conn && pendingWorkflow?.sid===selected && selected && source==="workbench" && !conn.outbox?.items.create){
    const mode=pendingWorkflow.mode;pendingWorkflow="";activateWorkflow(mode);
  }
}
function activateWorkflow(mode){
  if(!["weave","contest","fleet"].includes(mode)){choose(selected);return;}
  if(!selected){
    pendingWorkflow={mode,sid:""};
    const model=$("composer-model").value,permission=$("composer-mode").value;
    submit("create",{kind:"create",workflow:mode,path:"/session",body:{...inputProjectOptions(),...(model?{model}:{}),...(permission?{mode:permission}:{}),questions:{foreground_timeout_seconds:60}}});
    return;
  }
  prepareNavigation();saveDraft();source="workbench";collaborationPage=mode;
  $("work-mode").value=mode;
  if(mode==="weave"){weaveForm(selected).open=true;if(connection)loadWeaveCapabilities(connection,selected);}
  else if(mode==="contest")contests.open();
  else fleetCommands.open();
  render();rememberNavigation();$("thread-scroll").scrollTop=0;
}

$("toggle-panes").addEventListener("click",()=>{if(panes)panes.toggle();else notice("工具工作区正在接入。");});
$("session-menu-items").addEventListener("click",e=>{if(workspaceController.click(e))return;const n=e.target.closest("[data-session-command]");if(n)sessionCommand(n.dataset.sessionCommand);});
$("session-command-form")?.addEventListener("submit",()=>{});
$("session-menu-items").addEventListener("submit",async e=>{e.preventDefault();const command=e.target.dataset.command,value=$("session-command-value").value.trim(),sid=selected,conn=connection;if(!sid||!conn)return;try{if(command==="group"){const meta={...(settingsController.values().ui?.session_meta||{})};meta[sid]={group:value,read_seq:meta[sid]?.read_seq||0};await settingsController.saveUI({session_meta:meta});$("session-menu").open=false;render();}else if(command==="rename"){if(!value)throw new Error("请输入会话标题");await conn.client.post(`/desktop/sessions/${encodeURIComponent(sid)}/title`,{title:value},{signal:conn.controller.signal});$("session-menu").open=false;await refreshAfterAction(conn);}else if(command==="fork"){if(value&&(!/^\d+$/.test(value)||Number(value)<1))throw new Error("轮次数须为正整数");$("session-menu").open=false;submit("create",{kind:"create",path:"/session",body:{fork_from:{session_id:sid,...(value?{turns:Number(value)}:{})},questions:{foreground_timeout_seconds:60}}});}}catch(error){notice(error.message);}});

$("close-settings").addEventListener("click",() => { $("connection-panel").hidden = true; $("settings").focus(); });
$("refresh").addEventListener("click",() => {
  if (!connection) return;
  refresh(connection);
  loadCLIList(connection);
  if (source === "cli") { if (cliSelected) loadCLIHistory(connection, cliSelected); }
  else if (source === "automations") automations.load(connection);
  else if (source === "connections") connections.load(connection);
  else if(source==="settings"){settingsController.load();providersController.load();dataController.load();}
  else if(pageControllers.has(source)){pageControllers.get(source).load(connection);if(source==="library")contentController.loadLibrary();}
  else if (source === "project") { workspaceController.load(connection); if (projectSelected) workspaceController.loadArtifacts(projectSelected); }
  else if (source === "archived") workspaceController.load(connection);
  else if (selected) { loadArchive(connection, selected, true); loadWeaves(connection, selected); contests.load(connection, selected); fleets.load(connection, selected); fleetCommands.load(connection, selected); }
});
$("new-session").addEventListener("click",()=>newConversation());
$("cancel-create").addEventListener("click",() => { $("create-panel").hidden = true; });
$("composer-project-choices").addEventListener("click",event=>{const node=event.target.closest("[data-input-project]");if(node)newConversation({projectId:node.dataset.inputProject});});
$("create-form").addEventListener("submit", event => {event.preventDefault();const workspace=$("workspace-path").value.trim();if(workspace)newConversation({workspace});});
document.querySelector(".shell").addEventListener("click",e => {const n=e.target.closest("[data-filter]");if(n){prepareNavigation();filter=n.dataset.filter;$("sidebar-activity").open=false;if(source !== "workbench") choose(selected,{record:false});render();rememberNavigation();}});
$("cli-sessions").addEventListener("click",openCLI);
$("takeover-bar").addEventListener("click",e=>{
  if (source !== "cli" || !connection || !cliSelected) return;
  const open=e.target.closest("[data-cli-open-session]"),view=cliViews.get(cliSelected);
  if (open) { choose(open.dataset.cliOpenSession); render(); return; }
  if (e.target.closest("[data-cli-open-workspace]")) { window.webkit?.messageHandlers?.workspace?.postMessage({operation:"open_catalog_workspace",catalog_id:cliSelected}).catch(error=>notice(`未能打开那个目录的工作台：${error.message||error}`)); return; }
  if (e.target.closest("[data-cli-retry]")) { notice(); loadCLIList(connection); loadCLIHistory(connection, cliSelected); return; }
  const state=takeover(view,{writable:!!writable(),pending:!!connection.outbox?.items.create}),reference={catalog_id:cliSelected,revision:view?.revision};
  if (e.target.closest("[data-cli-continue]") && state.canContinue) submit("create",{kind:"create",path:"/session",body:{adopt_from:reference,questions:{foreground_timeout_seconds:60}}});
  else if (e.target.closest("[data-cli-fork]") && state.canFork) submit("create",{kind:"create",path:"/session",body:{fork_from:reference,questions:{foreground_timeout_seconds:60}}});
});
$("automations").addEventListener("click",() => openManagement("automations"));
$("projects").addEventListener("click",() => openProject(workspaceController.projectForSession(selected) || projectSelected || workspaceController.projects()[0]?.project_id || ""));
$("new-contest").addEventListener("click",()=>activateWorkflow("contest"));
$("new-weave").addEventListener("click",()=>activateWorkflow("weave"));
$("new-fleet").addEventListener("click",()=>activateWorkflow("fleet"));
$("thread").addEventListener("click",e=>{if(e.target.closest("[data-workflow-stop]"))$("stop").click();const link=e.target.closest("[data-workflow-page]");if(link)activateWorkflow(link.dataset.workflowPage);});

function openSearch() {
  sidebarCollapsed(false, false);
  if(desktop){openPage("search");$("thread").querySelector('[data-content-field="query"]')?.focus();return;}
  $("sidebar-search").hidden = false;
  $("search-toggle").setAttribute("aria-expanded", "true");
  $("search").focus();
}
$("search-toggle").addEventListener("click", openSearch);
$("search").addEventListener("keydown", e => {
  if (e.key !== "Escape" || e.isComposing) return;
  e.preventDefault(); search = ""; e.target.value = "";
  $("sidebar-search").hidden = true;
  $("search-toggle").setAttribute("aria-expanded", "false");
  $("search-toggle").focus(); render();
});
$("search").addEventListener("input",e => {search=e.target.value;render();});
$("thread").addEventListener("input",e=>{if(e.target.id==="archive-search"){archiveQuery=e.target.value;renderThread();}});
$("sessions").addEventListener("click",e => {
  const create=e.target.closest("[data-project-new-chat]");
  if(create){
    e.preventDefault();e.stopPropagation();
    const entry=workspaceController.projects().find(p=>p.project_id===create.dataset.projectNewChat);
    if(entry && writable())newConversation({projectId:entry.project_id});
    return;
  }
  if(e.target.closest('[data-workspace-action="pin"]')){const group=$("sessions").querySelector('[data-key="section-pinned"]');if(group)group.open=true;}
  if(workspaceController.click(e))return;
  if(e.target.closest("[data-sidebar-projects]")){e.preventDefault();$("projects").click();return;}
  if(e.target.closest("[data-sidebar-clear]")){search="";$("search").value="";}
  const p=e.target.closest("[data-project]"); if (p) { e.preventDefault();openProject(p.dataset.project); return; }
  const n=e.target.closest("[data-session]"),cli=e.target.closest("[data-cli-session]");
  if(n){choose(n.dataset.session);render();}
  if(cli)chooseCLI(cli.dataset.cliSession);
  if(connection && e.target.closest("[data-cli-list-retry],[data-cli-list-more]")) loadCLIList(connection,!!e.target.closest("[data-cli-list-more]"));
});
$("message").addEventListener("input",() => { saveDraft(); renderComposer(); });
$("composer").addEventListener("submit",e => {e.preventDefault();submitText($("message").value);});
$("message").addEventListener("keydown",e=>{
  const action=composerEnterAction(e,settingsController.values().ui?.send_key || "enter");
  if(!action || $("message").disabled || $("message").readOnly)return;
  e.preventDefault();
  if(action==="send")$("composer").requestSubmit();
  else {
    const input=$("message");input.setRangeText("\n",input.selectionStart,input.selectionEnd,"end");
    input.dispatchEvent(new Event("input",{bubbles:true}));
  }
});
$("stop").addEventListener("click",() => {const s=session();if(s?.in_flight?.turn)submit(scopeFor(s.session_id,"abort"),{kind:"abort",sid:s.session_id,path:`${sidPath(s.session_id)}/abort`,body:{turn:s.in_flight.turn}});});
$("thread").addEventListener("submit",e => {
  if (dataController.onSubmit(e) || settingsController.onSubmit(e) || providersController.onSubmit(e) || [...new Set(pageControllers.values())].some(c=>c.onSubmit?.(e)) || connections.onSubmit(e) || automations.onSubmit(e) || workspaceController.onSubmit(e) || weaveControls.onSubmit(e) || fleetCommands.onSubmit(e) || fleets.onSubmit(e) || contests.onSubmit(e)) return;
  if (e.target.id === "weave-form") { e.preventDefault(); previewWeave(); return; }
  e.preventDefault();const form=e.target,sid=form.dataset.sid;
  const s=sessions.find(s=>s.session_id===sid);if(!s)return;
  if(form.dataset.approval){const id=form.dataset.approval,request=s.pending_approvals?.find(r=>r.request_id===id);if(!request || request.identity && !(request.allowed_decisions || []).includes(e.submitter?.value))return;submit(scopeFor(sid,"approval",id),{kind:"approval",sid,path:`${sidPath(sid)}/permissions`,body:{request_id:id,decision:e.submitter?.value,reason:form.elements.reason.value}});}
  if(form.dataset.question){
    const q=s.questionItems.find(q=>q.question_id===form.dataset.question);if(!q)return;
    const answers=q.items.map((item,i)=>({item_id:item.item_id,selected:[...form.querySelectorAll(`[name="choice-${i}"]:checked`)].map(n=>n.value),custom:form.elements[`custom-${i}`].value.trim(),skipped:form.elements[`skip-${i}`].checked}));
    if(answers.some(a=>a.skipped?(a.selected.length||a.custom):(!a.selected.length&&!a.custom))){notice("请明确回答每一题，或勾选跳过；跳过时请先清空该题的选择和文字。");return;}
    submit(scopeFor(sid,"answer",q.question_id),{kind:"answer",sid,qid:q.question_id,path:`${sidPath(sid)}/questions/${encodeURIComponent(q.question_id)}/answers`,body:{answers}});
  }
});
$("thread").addEventListener("input",e => {
  if (shortcutsController.input(e) || dataController.input(e) || settingsController.input(e) || providersController.input(e) || [...new Set(pageControllers.values())].some(c=>c.input?.(e)) || connections.input(e) || automations.input(e) || workspaceController.input(e) || weaveControls.input(e) || fleetCommands.input(e) || fleets.input(e) || contests.input(e)) return;
  if (!selected || source !== "workbench" || !e.target.closest("#weave-form")) return;
  const state = weaveForm(selected), node = e.target;
  if (node.dataset.weaveItem) { const item = state.draft.items?.find(i => i.id === node.dataset.weaveItem); if (item) item.text = node.value; }
  else if (["prompt_template","instruction","spec"].includes(node.name)) state.draft[node.name] = node.value;
  state.error = ""; persistWeaveDraft(selected,state);
  if (node.tagName === "SELECT") renderThread();
  else if ($("thread").querySelector("[data-weave-submit]")) renderThread();
});
$("thread").addEventListener("change",e => {
  if (e.target.id === "project-choice") { openProject(e.target.value); return; }
  if (shortcutsController.input(e) || dataController.input(e) || settingsController.input(e) || providersController.input(e) || [...new Set(pageControllers.values())].some(c=>c.input?.(e)) || connections.input(e) || automations.input(e) || workspaceController.input(e) || weaveControls.input(e) || fleetCommands.input(e) || fleets.input(e) || contests.input(e)) return;
  const node = e.target;
  if (!selected || source !== "workbench" || !node.dataset.resumeItem) return;
  const state = weaveForm(selected), batch = node.dataset.resumeBatch;
  if (!state.selections.has(batch)) state.selections.set(batch,new Set());
  if (node.checked) state.selections.get(batch).add(node.dataset.resumeItem); else state.selections.get(batch).delete(node.dataset.resumeItem);
  renderThread();
});
$("home").addEventListener("click",()=>choose(selected));
$("execution-local").addEventListener("click",()=>{$("execution-menu").open=false;$("execution-menu").querySelector("summary").focus();});
$("sidebar-more").addEventListener("click",e=>{if(e.target.closest("button"))$("sidebar-more").open=false;});
$("starter-menu").addEventListener("click",e=>{if($("message").disabled || $("message").readOnly)e.preventDefault();});
$("thread").addEventListener("click",e=>{
  const button=e.target.closest(".welcome-starters [data-starter]");
  if(!button || source!=="workbench" || selected || !writable() || $("message").disabled || $("message").readOnly || $("message").value.trim())return;
  const item=taskStarters[Number(button.dataset.starter)];
  if(!item)return;
  $("work-mode").value="auto";
  setComposer(item.draft);
  rememberNavigation();
  $("message").focus();
});
$("thread").addEventListener("click",e => {
  const copy=e.target.closest("[data-copy-message]");
  if(copy){const text=messageCopies.get(copy.dataset.copyMessage);if(text!==undefined)navigator.clipboard.writeText(text).then(()=>{copy.innerHTML=icon("check");copy.setAttribute("aria-label","已复制");copy.title="已复制";}).catch(()=>notice("复制未成功，请选中回答文字后复制。"));return;}
  const materials=e.target.closest("[data-project-materials]");if(materials){openPage("content-project",{projectId:materials.dataset.projectMaterials});return;}
  if(e.target.closest("[data-back-chat]")){choose(selected);return;}
  if (shortcutsController.click(e) || dataController.click(e) || settingsController.click(e) || providersController.click(e) || [...new Set(pageControllers.values())].some(c=>c.click?.(e)) || connections.click(e) || automations.click(e) || workspaceController.click(e)) return;
  const projectLink=e.target.closest("[data-open-project]"), projectReturn=e.target.closest("[data-project-return]"), projectCreate=e.target.closest("[data-project-create]");
  if (projectLink) { openProject(projectLink.dataset.openProject); return; }
  if (projectReturn) { choose(projectReturn.dataset.projectReturn); return; }
  if (projectCreate) { const entry=workspaceController.projects().find(p => p.project_id === projectCreate.dataset.projectCreate); if (entry && writable()) newConversation({projectId:entry.project_id,workspace:entry.workspace}); return; }
  if (source === "cli") {
    if (connection && cliSelected && e.target.closest("[data-cli-history-retry],[data-cli-history-more]")) loadCLIHistory(connection, cliSelected, !!e.target.closest("[data-cli-history-more]"));
    return;
  }
  if(collaborationPage && e.target.closest('[data-weave-close],[data-contest-action="close"],[data-fc-action="close"]')){choose(selected);return;}
  if (weaveControls.click(e) || fleetCommands.click(e) || fleets.click(e) || contests.click(e)) return;
  const action = e.target.closest("[data-weave-close],[data-weave-reset],[data-weave-add],[data-weave-remove],[data-weave-submit],[data-resume-weave],[data-weave-capabilities]");
  if (action && selected) {
    const state = weaveForm(selected);
    if (action.hasAttribute("data-weave-close")) state.open = false;
    if (action.hasAttribute("data-weave-reset")) { state.draft = emptyWeaveDraft(); state.error = ""; persistWeaveDraft(selected,state); }
    if (action.hasAttribute("data-weave-capabilities") && connection) loadWeaveCapabilities(connection, selected);
    if (action.hasAttribute("data-weave-add") && state.draft.items.length < Math.min(64,state.capabilities?.max_items || 64)) { state.draft.items.push({id:crypto.randomUUID(),text:""}); persistWeaveDraft(selected,state); }
    if (action.dataset.weaveRemove) { state.draft.items = state.draft.items.filter(i => i.id !== action.dataset.weaveRemove); persistWeaveDraft(selected,state); }
    if (action.dataset.resumeWeave) {
      const batch = action.dataset.resumeWeave, eligible = state.capabilities?.batches?.find(b => b.batch_id === batch)?.items || [];
      const ids = [...(state.selections.get(batch) || [])].filter(id => eligible.some(i => i.id === id && i.can_resume));
      if (ids.length) { state.draft = {mode:"resume",source_batch_id:batch,item_ids:ids,instruction:""}; state.open = true; state.error = ""; persistWeaveDraft(selected,state); $("thread-scroll").scrollTop = 0; }
    }
    if (action.hasAttribute("data-weave-submit") && state.preview && state.previewDraft === JSON.stringify(state.draft)) submit(scopeFor(selected,"weave"), {kind:"weave",sid:selected,path:`${sidPath(selected)}/weaves`,body:{...state.previewBody,preview_token:state.preview.preview_token},draftKey:weaveDraftKey(selected),draftText:state.previewDraft});
    renderThread(); return;
  }
  const inspect = e.target.closest("[data-artifact-inspect]"), reveal = e.target.closest("[data-artifact-reveal]");
  if (inspect) { inspectArtifact(inspect.dataset.artifactInspect); return; }
  if (reveal) { revealArtifact(reveal.dataset.artifactReveal); return; }
  const n=e.target.closest("[data-continue]");if(n&&n.dataset.continue===selected&&!session()?.busy)submitText("根据我提交的答案继续。");
  const retry=e.target.closest("[data-history-retry]"),more=e.target.closest("[data-history-more]");
  if (connection && selected && (retry || more)) loadPage(connection, selected, (retry || more).dataset[retry ? "historyRetry" : "historyMore"], !!more);
  if (connection && selected && e.target.closest("[data-weaves-retry],[data-weaves-more]")) loadWeaves(connection, selected, !!e.target.closest("[data-weaves-more]"));
});
$("outbox").addEventListener("click",e => {
  const retry=e.target.closest("[data-retry]"),forget=e.target.closest("[data-forget]");
  if(retry)sendAction(retry.dataset.retry);
  if(forget){if(!ownsAction(connection.outbox.items[forget.dataset.forget])){notice("此提交来自另一窗口，请回到原窗口核对。");return;}try{connection.outbox.forget(forget.dataset.forget);render();}catch(error){banner(error.message);}}
});
function sidebarCollapsed(collapsed, focus = true, persist=true) {
  if(persist)sidebarVisibilityOverride=collapsed;
  document.querySelector(".shell").classList.toggle("sidebar-collapsed", collapsed);
  $("sidebar").hidden = collapsed; $("expand-sidebar").hidden = !collapsed;
  $("expand-sidebar").setAttribute("aria-expanded", String(!collapsed));
  if(persist)settingsController.saveUI({sidebar_hidden:collapsed}).catch(e=>notice(e.message));
  if (focus && !nativeTitlebar) (collapsed ? $("expand-sidebar") : $("collapse-sidebar")).focus();
}
function toggleCurrentSidebar() {
  if(source === "settings") {
    if(nativeTitlebar)$("workspace").classList.toggle("settings-navigation-collapsed");
    return;
  }
  const sidebarHadFocus=$("sidebar").contains(document.activeElement);
  sidebarCollapsed(!$("sidebar").hidden,!nativeTitlebar);
  if(nativeTitlebar && sidebarHadFocus)$("workspace").focus({preventScroll:true});
}
window.addEventListener("xiaoyu-toggle-sidebar",()=>{if(nativeTitlebar)toggleCurrentSidebar();});
window.addEventListener("xiaoyu-toggle-panel",()=>{if(nativePanelTitlebar)panes?.toggle();});
$("collapse-sidebar").addEventListener("click", () => sidebarCollapsed(true));
$("expand-sidebar").addEventListener("click", () => sidebarCollapsed(false));
document.addEventListener("keydown",e=>{const action=shortcutAction(e,settingsController.values().ui?.shortcuts);if(!action)return;e.preventDefault();if(action==="settings")openPage("settings");if(action==="new_task"&&writable())newConversation();if(action==="search")openSearch();if(action==="sidebar")toggleCurrentSidebar();if(action==="panel")panes?.toggle();});

window.addEventListener("pagehide",()=>{for(const side of sideConversations)side.dispose();panes?.destroy();saveDraft();connection?.localState?.flush().catch(()=>{});if(connection){clearTimeout(connection.timer);connection.controller.abort();}eventController?.abort();});
$("setup-command").textContent=`xiaoyu serve --workspace <项目父目录> --environment host --cors-origin ${location.origin}`;
panes=createPanes({host:$("workspace-panes"),context:()=>({client:connection?.client,session_id:selected,instance_id:connection?.health?.instance_id}),
  onReference:ref=>putReference(`${ref.text}\n\n来源：${ref.source?.url||ref.source?.path||ref.source?.session_id||"工作区"} · ${ref.captured_at||new Date().toISOString()}`),
  onSource:ref=>{if(ref?.session_id)choose(ref.session_id);},
  onOpenTool:kind=>kind==="collaboration"?openCollaborationPanel():openContentPanel(kind==="files"?"project":"pages"),
  loadLayout:()=>({width:settingsController.values().ui?.panel_width||420}),
  saveLayout:async layout=>{await settingsController.saveUI({panel_width:Math.max(280,Math.min(1000,Math.round(layout.width)))});},
  onLayout:layout=>document.querySelector(".work-area").classList.toggle("panes-expanded",layout.open&&layout.expanded),
});
navigationHistory=createViewHistory({initial:viewSnapshot(),key:viewHistoryKey,onChange:navigationStateChanged});
window.addEventListener("xiaoyu-navigate-back",()=>traverseNavigation("back"));
window.addEventListener("xiaoyu-navigate-forward",()=>traverseNavigation("forward"));
if(fixture)$("endpoint").value="http://127.0.0.1:18420";
if(desktop){
  window.__XIAOYU_VIEW_STATE__ = () => viewState(session() && !hasRecentActivity(session()) ? "" : selected,source);
  window.__XIAOYU_WORK_STATUS__ = () => workStatus(sessions,connection?.healthy);
  window.__XIAOYU_FLUSH__ = async () => { saveDraft(); await connection?.localState?.flush(); return true; };
  $("connection-panel").hidden=true; $("connect-form").hidden=true; $("setup-command").closest("details").hidden=true;
  $("connect-title").textContent="本机连接"; $("connection-description").textContent="此服务由羽案管理，连接凭据仅保留在内存中。"; $("connection-summary").hidden=false;
  $("endpoint").value=native.origin;$("token").value=native.token;
  $("connect-form").requestSubmit();native.token="";
}else {$("connection-panel").hidden=false;render();}

// Measure only presentation when the composer column changes width.
new ResizeObserver(() => fitComposer($("message"))).observe($("composer"));
document.addEventListener("click", e => {
  for (const id of ["session-menu","collaboration-menu","starter-menu","execution-menu","sidebar-more","sidebar-activity"]) { const menu=$(id); if(menu.open && !e.composedPath().includes(menu)) menu.open=false; }
});
document.addEventListener("keydown", e => {
  if(e.key !== "Escape" || e.isComposing) return;
  for (const id of ["session-menu","collaboration-menu","starter-menu","execution-menu","sidebar-more","sidebar-activity"]) { const menu=$(id); if(menu.open){menu.open=false;menu.querySelector("summary").focus();break;} }
});

$("collaboration-menu").addEventListener("click", e => {
  if (!e.target.closest("button")) return;
  $("collaboration-menu").open=false;
  const section=e.target.closest("#new-fleet")?".fleet-command-section":e.target.closest("#new-weave")?".weave-compose":".contest-compose";
  const heading=$("thread").querySelector(`${section} h2,${section} h3`);
  if(heading){heading.tabIndex=-1;heading.focus({preventScroll:true});}
  else $("collaboration-menu").querySelector("summary").focus();
});
for (const id of ["session-menu","collaboration-menu","starter-menu","execution-menu","sidebar-more","sidebar-activity"]) {
  $(id).addEventListener("toggle",()=>!["sidebar-more","sidebar-activity"].includes(id)&&positionMenu($(id),$("workspace")));
}
new ResizeObserver(()=>{
  for (const id of ["session-menu","collaboration-menu","starter-menu","execution-menu","sidebar-more","sidebar-activity"]) !["sidebar-more","sidebar-activity"].includes(id)&&positionMenu($(id),$("workspace"));
}).observe($("workspace"));

$("close-execution-details").addEventListener("click",()=>$('execution-dialog').close());
$("execution-dialog").addEventListener("click",e=>{
  const inspect=e.target.closest('[data-artifact-inspect]'),reveal=e.target.closest('[data-artifact-reveal]');
  if(inspect){$('execution-dialog').close();inspectArtifact(inspect.dataset.artifactInspect);return;}
  if(reveal){revealArtifact(reveal.dataset.artifactReveal);return;}

  const retry=e.target.closest('[data-history-retry]'),more=e.target.closest('[data-history-more]');
  if(connection&&selected&&(retry||more))loadPage(connection,selected,(retry||more).dataset[retry?'historyRetry':'historyMore'],!!more);
});
