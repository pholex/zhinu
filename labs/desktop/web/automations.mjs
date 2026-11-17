import {escapeHTML as h} from "./client.mjs";
import {icon} from "./ui.mjs";

const ACTIVE=new Set(["reserved","creating","submitting","running","unknown"]);
const LABELS={reserved:"已登记触发",creating:"正在创建会话",submitting:"正在提交任务",running:"会话尚未收束",unknown:"结果未知 · 待人工核对",completed:"已完成",failed:"执行失败",interrupted:"执行中断",acknowledged:"人工已核对（不代表成功）"};
const DRAFT="automation-definition",PENDING="automation-pending",NOTE=id=>`automation-note:${id}`;
const obj=v=>!!v && typeof v==="object" && !Array.isArray(v);
const validDefinition=d=>obj(d) && ["name","workspace","model","prompt"].every(k=>typeof d[k]==="string") && obj(d.schedule) && (d.schedule.kind==="once" ? Number.isFinite(d.schedule.at) && d.schedule.at>=0 : (d.schedule.kind==="interval" && Number.isFinite(d.schedule.seconds) && d.schedule.seconds>=60 || d.schedule.kind==="calendar" && typeof d.schedule.timezone==="string" && /^([01]\d|2[0-3]):[0-5]\d$/.test(d.schedule.time) && Array.isArray(d.schedule.weekdays) && d.schedule.weekdays.length>0));
const at=t=>typeof t==="number" && t>0 ? new Date(t*1000).toLocaleString("zh-CN") : "未记录";
const localTime=t=>{const d=new Date(t);return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16);};
export function validateAutomationResponse(value){
  const s=value?.state;
  if(!s || !Number.isInteger(s.revision) || s.revision<0 || !obj(s.tasks) || !obj(s.runs) || typeof value.runner_active!=="boolean" || typeof value.warning!=="string" || Object.values(s.tasks).some(t=>!validDefinition(t?.definition) || typeof t.enabled!=="boolean" || !Number.isFinite(t.next_due) || typeof t.warning!=="string") || Object.values(s.runs).some(r=>!obj(r) || !Object.hasOwn(LABELS,r.state) || typeof r.task_id!=="string" || typeof r.session_id!=="string" || typeof r.run_id!=="string" || typeof r.message!=="string"))throw new Error("自动化响应格式不正确，保留已有快照。");
  return value;
}
export function automationDefinition(d){
  if(["name","workspace","model","prompt"].some(k=>typeof d[k]!=="string" || !d[k].trim() && !(k==="workspace" && d.project_id)))throw new Error("请填写名称、项目目录、模型与完整任务指令。");
  if(d.name.length>160 || d.workspace.length>4096 || d.model.length>300 || d.prompt.length>40000)throw new Error("名称、目录或模型过长；任务指令最多 40000 字符。");
  let schedule;
  if(d.kind==="once") {const ms=new Date(d.at).getTime();if(!Number.isFinite(ms) || ms<=Date.now())throw new Error("请选择未来的单次时间（本机时区）。");schedule={kind:"once",at:ms/1000};}
  else if(d.kind==="interval") {const seconds=Number(d.seconds);if(!Number.isFinite(seconds) || seconds<60)throw new Error("固定间隔至少为 60 秒。");schedule={kind:"interval",seconds};}
  else if(d.kind==="calendar") {const timezone=d.timezone||"UTC";try{new Intl.DateTimeFormat("en",{timeZone:timezone});}catch{throw new Error("请选择有效的 IANA 时区。");}const weekdays=String(d.weekdays||"").split(",").map(v=>Number(v.trim()));if(!/^([01]\d|2[0-3]):[0-5]\d$/.test(d.time||"")||!weekdays.length||weekdays.some(n=>!Number.isInteger(n)||n<0||n>6)||new Set(weekdays).size!==weekdays.length)throw new Error("请填写时间与星期：周一 0 至周日 6，逗号分隔。");schedule={kind:"calendar",timezone,time:d.time,weekdays};}
  else throw new Error("只支持单次、固定间隔或日历调度。");
  return {name:d.name.trim(),workspace:d.workspace.trim(),model:d.model.trim(),prompt:d.prompt,schedule,...(d.project_id?{project_id:d.project_id}:{})};
}
export function automationEligibility(taskId,state){
  const runs=Object.values(state?.runs || {}).filter(r=>r.task_id===taskId);
  return {canEdit:!runs.some(r=>ACTIVE.has(r.state)),canEnable:!runs.some(r=>r.state==="unknown")};
}
function validatePending(p){
  if(!obj(p) || !obj(p.body) || !Number.isInteger(p.body.revision) || p.body.revision<0)throw new Error();
  if(p.kind==="configure" && p.path==="/desktop/automations/configure" && typeof p.body.task_id==="string" && typeof p.body.create==="boolean" && obj(p.body.definition))return p;
  if(p.kind==="enabled" && /^\/desktop\/automations\/[A-Za-z0-9_-]+\/enabled$/.test(p.path) && typeof p.body.enabled==="boolean")return p;
  if(p.kind==="acknowledge" && /^\/desktop\/automation-runs\/[A-Za-z0-9_-]+\/acknowledge$/.test(p.path) && typeof p.body.note==="string")return p;
  throw new Error();
}
export function createAutomationController(ui){
  let data=null,error="",loading=false,generation=0,draft=null,draftError="",formError="",pending=null,pendingError="",sending=false,restored=false,open=false;
  const notes=new Map(),ctx=()=>ui.context(),paint=()=>ui.render(),store=()=>ctx().conn?.localState;
  const writable=()=>!!(ctx().writable && ctx().conn && store() && data && !error && !draftError && !pendingError);
  const persist=()=>{if(draft)store()?.setDraft(DRAFT,JSON.stringify(draft));};
  function restore(){if(restored || !store())return;restored=true;
    try{const raw=store()?.getDraft(DRAFT);if(raw){const d=JSON.parse(raw);if(!obj(d) || typeof d.task_id!=="string" || typeof d.create!=="boolean" || ["name","workspace","model","prompt","kind","at","seconds"].some(k=>typeof d[k]!=="string"))throw new Error();draft=d;}}
    catch{draftError="已保存的自动化草稿无法读取，原文保留，未覆盖。";}
    try{const raw=store()?.getDraft(PENDING);if(raw)pending={...validatePending(JSON.parse(raw)),status:"unknown",message:"发现待核对的管理请求；未自动重发，也未自动启用。"};}
    catch{pendingError="已保存的自动化管理请求无法读取，原数据保留，写操作已暂停。";}
  }
  async function load(conn=ctx().conn){if(!conn || conn!==ctx().conn || loading)return;const epoch=generation;loading=true;restore();paint();
    try{const next=validateAutomationResponse(await conn.client.get("/desktop/automations",conn.controller.signal));if(conn!==ctx().conn || epoch!==generation)return;if(!data || next.state.revision>=data.state.revision)data=next;ui.onSnapshot?.(data);error="";}
    catch(e){if(conn===ctx().conn && epoch===generation)error=e.status===404 ? "当前服务不支持自动化管理。" : e.message;}
    finally{if(conn===ctx().conn && epoch===generation){loading=false;paint();}}
  }
  function edit(id="") {restore();if(draftError)return;if(draft && (!id || draft.task_id===id)){open=true;paint();return;}const task=data?.state.tasks[id];if(id && (!task || !automationEligibility(id,data.state).canEdit))return;
    if(draft && draft.task_id!==id && (draft.name || draft.prompt)){formError="当前还有配置草稿。先保存，或明确放弃草稿后再编辑其他任务。";open=true;paint();return;}
    const d=task?.definition;draft={task_id:id || crypto.randomUUID(),create:!id,project_id:d?.project_id||"",name:d?.name || "",workspace:d?.workspace || ctx().projects?.[0]?.workspace || "",model:d?.model || "",prompt:d?.prompt || "",kind:d?.schedule.kind || "interval",at:d?.schedule.kind==="once" ? localTime(d.schedule.at*1000) : localTime(Date.now()+3600000),seconds:String(d?.schedule.seconds || 3600),timezone:d?.schedule.timezone || Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",time:d?.schedule.time || "09:00",weekdays:d?.schedule.weekdays?.join(",") || "0,1,2,3,4"};open=true;formError="";persist();paint();
  }
  function request(kind,path,body){if(!writable() || pending || sending)return;pending={kind,path,body,status:"ready",message:""};dispatch();}
  async function dispatch(){const conn=ctx().conn,epoch=generation,p=pending;if(!conn || !p || sending || !writable() || p.status==="conflict")return;sending=true;p.status="sending";paint();
    try{store().setDraft(PENDING,JSON.stringify({kind:p.kind,path:p.path,body:p.body}));await store().flush();await conn.client.post(p.path,p.body,{signal:conn.controller.signal});if(conn!==ctx().conn || epoch!==generation)return;
      pending=null;store().deleteDraft(PENDING);await store().flush();await load(conn);
      if(p.kind==="configure" && draft?.task_id===p.body.task_id){draft.create=false;persist();formError="配置保存请求已确认。请以任务卡片的当前启用状态为准；原配置草稿保留。";}
    }catch(e){if(conn===ctx().conn && epoch===generation){pending=p;p.status=e.status===409 ? "conflict" : "unknown";p.message=e.status===409 ? "配置版本已变化，原请求没有覆盖新状态。请核对当前值及本地草稿，再决定是否重新保存。" : `管理请求结果待核对：${e.message}。原请求保留；没有自动重试。`;await load(conn);}}
    finally{if(conn===ctx().conn && epoch===generation){sending=false;paint();}}
  }
  function note(id){if(!notes.has(id))notes.set(id,store()?.getDraft(NOTE(id)) || "");return notes.get(id);}
  function render(){restore();let html=`<section class="automation-page"><div class="section-heading"><h2>定时任务</h2><div class="actions"><button type="button" data-auto-action="new" ${!writable() ? "disabled" : ""}>${icon("plus")}<span>新建定时任务</span></button><button type="button" data-auto-action="reload" ${loading ? "disabled" : ""}>${icon("refresh")}<span>刷新</span></button></div></div><p class="note">仅在羽案应用打开期间调度，不常驻后台。关闭期间错过的周期不会补跑；保存配置不会启用任务。审批和问答仍需在关联会话处理。</p>`;
    if(loading && !data)html+=`<p class="note">正在读取自动化记录…</p>`;
    if(error || pendingError || draftError)html+=`<p class="form-error">${h(error || pendingError || draftError)}${data ? " 以下保留上次快照，可能过期。" : " 不能判断是否已有任务。"}</p>`;
    if(data)html+=`<p class="note">调度器：${data.runner_active ? "本次应用服务已运行" : "当前未运行"}</p>${data.warning ? `<p class="form-error">${h(data.warning)}</p>` : ""}`;
    if(pending)html+=`<div class="callout warning"><p>${h(pending.message || "正在保存管理请求…")}</p><p class="note">${h(pending.kind)} · 原版本 ${h(pending.body.revision)}。可先检查当前配置；核对后清除回执不会撤销已生效的更新。</p><button type="button" data-auto-action="retry" ${sending || pending.status==="conflict" || !writable() ? "disabled" : ""}>使用原请求重试</button><button type="button" data-auto-action="forget" ${sending ? "disabled" : ""}>已核对，清除管理回执</button></div>`;
    if(formError)html+=`<p class="note">${h(formError)}</p>`;
    if(data?.calendar?.some(row=>row.enabled)){html+=`<details><summary>未来执行日历</summary><p class="note">${h(data.schedule_policy||"")}</p>${data.calendar.filter(row=>row.enabled).map(row=>`<p><strong>${h(data.state.tasks[row.task_id]?.definition.name||row.task_id)}</strong> · ${h(row.timezone)}<br>${row.occurrences.map(stamp=>h(new Date(stamp*1000).toLocaleString("zh-CN",{timeZone:row.timezone}))).join(" · ")}</p>`).join("")}</details>`;}
    if(open && draft){const d=draft;html+=`<form data-auto-form="configure" class="automation-form"><h3>${d.create ? "新建配置（默认禁用）" : "编辑配置（保存后停用调度）"}</h3><p class="note">配置 ID：${h(d.task_id)}；模型不能为空，每次触发会新建关联会话。</p><label>名称<input id="auto-name" data-auto-field="name" value="${h(d.name)}" maxlength="160" required></label><label>项目<select data-auto-field="project_id"><option value="">使用指定工作目录</option>${(ctx().projects||[]).map(p=>`<option value="${h(p.project_id)}" ${d.project_id===p.project_id?"selected":""}>${h(p.name||p.workspace)}</option>`).join("")}</select></label>${d.project_id?'<p class="note">每次触发使用项目当时的主文件夹；修改项目文件夹会影响后续任务。</p>':`<label>工作目录<input id="auto-workspace" data-auto-field="workspace" value="${h(d.workspace)}" ${d.project_id?"disabled":""} list="auto-projects" maxlength="4096" required><datalist id="auto-projects">${(ctx().projects || []).map(p=>`<option value="${h(p.workspace)}"></option>`).join("")}</datalist></label>`}<label>模型<input id="auto-model" data-auto-field="model" value="${h(d.model)}" maxlength="300" required></label><label>任务指令<textarea id="auto-prompt" data-auto-field="prompt" rows="6" maxlength="40000" required>${h(d.prompt)}</textarea></label><label>调度<select id="auto-kind" data-auto-field="kind"><option value="interval" ${d.kind==="interval" ? "selected" : ""}>固定间隔</option><option value="once" ${d.kind==="once" ? "selected" : ""}>单次执行</option><option value="calendar" ${d.kind==="calendar" ? "selected" : ""}>每周日历</option></select></label>${d.kind==="once" ? `<label>本机时区时间<input id="auto-at" data-auto-field="at" type="datetime-local" value="${h(d.at)}" required></label>` : d.kind==="calendar" ? `<label>IANA 时区<input data-auto-field="timezone" value="${h(d.timezone||"UTC")}" required></label><label>当地时间<input type="time" data-auto-field="time" value="${h(d.time||"09:00")}" required></label><label>星期（周一 0 至周日 6，逗号分隔）<input data-auto-field="weekdays" value="${h(d.weekdays||"0,1,2,3,4")}" required></label><p class="note">夏令时不存在的时间跳过，重复时间只执行第一次；停机不补跑。</p>` : `<label>间隔（秒，至少 60）<input id="auto-seconds" data-auto-field="seconds" type="number" min="60" value="${h(d.seconds)}" required></label>`}<div class="actions"><button type="submit" ${!writable() || pending ? "disabled" : ""}>保存禁用配置</button><button type="button" data-auto-action="close">收起（保留草稿）</button><button type="button" data-auto-action="discard" ${pending ? "disabled" : ""}>明确放弃此配置草稿</button></div></form>`;}
    if(data){const entries=Object.entries(data.state.tasks);if(!entries.length)html+=`<p class="empty">还没有自动化任务。先保存一份配置，再决定何时启用。</p>`;
      for(const [id,t] of entries){const eligible=automationEligibility(id,data.state),runs=Object.entries(data.state.runs).filter(([,r])=>r.task_id===id).sort((a,b)=>b[1].created_at-a[1].created_at),disabled=!writable() || pending;html+=`<article class="automation-task"><h3>${h(t.definition.name)} <span class="badge">${t.enabled ? "已启用" : "已停用"}</span></h3><p class="note">${t.definition.project_id ? `项目：${h((ctx().projects||[]).find(p=>p.project_id===t.definition.project_id)?.name||t.definition.project_id)} · 使用触发时的主文件夹` : h(t.definition.workspace)} · ${h(t.definition.model)} · ${t.definition.schedule.kind==="once" ? `单次 ${h(at(t.definition.schedule.at))}` : t.definition.schedule.kind==="calendar" ? `${h(t.definition.schedule.timezone)} ${h(t.definition.schedule.time)} · 星期 ${h(t.definition.schedule.weekdays.map(n=>n+1).join("、"))}` : `每 ${h(t.definition.schedule.seconds)} 秒`} · ${t.enabled ? `下次 ${h(at(t.next_due))}` : "当前不会触发"}</p>${t.warning ? `<p class="form-error">${h(t.warning)}</p>` : ""}<details data-key="auto-definition-${h(id)}"><summary>已保存指令</summary><pre>${h(t.definition.prompt)}</pre></details><div class="actions"><button type="button" data-auto-action="edit" data-auto-id="${h(id)}" ${disabled || !eligible.canEdit ? "disabled" : ""}>${icon("edit")}<span>编辑配置</span></button><button type="button" data-auto-action="${t.enabled ? "disable" : "enable"}" data-auto-id="${h(id)}" ${disabled || !t.enabled && !eligible.canEnable ? "disabled" : ""}>${t.enabled ? "停用后续调度" : "明确启用"}</button></div><p class="note">停用只阻止未来触发，不停止已创建的会话。${!eligible.canEdit ? "存在尚未收束或未知运行，暂不能修改配置。" : ""}${!eligible.canEnable ? "先人工核对未知运行，再另行启用。" : ""}</p><h4>运行记录（${runs.length}）</h4>`;
        if(!runs.length)html+=`<p class="note">尚未触发。</p>`;
        for(const [rid,r] of runs){html+=`<section class="automation-run"><strong>${h(LABELS[r.state])}</strong><p class="note">计划 ${h(at(r.scheduled_at))} · 登记 ${h(at(r.created_at))} · 实例 ${h(rid)}${r.run_id ? ` · 会话轮次 ${h(r.run_id)}` : ""}</p><p>${h(r.message)}</p>${r.session_id ? `<button type="button" data-auto-action="session" data-auto-session="${h(r.session_id)}">${icon("message")}<span>查看关联会话与审批问答</span></button>` : `<p class="note">尚未记录关联会话；未知状态需自行核对工作台，不会重放。</p>`}${r.state==="unknown" ? `<form data-auto-form="acknowledge" data-auto-run="${h(rid)}"><label>人工核对说明<textarea id="auto-note-${h(rid)}" data-auto-note="${h(rid)}" maxlength="1000" rows="2" required>${h(note(rid))}</textarea></label><button type="submit" ${disabled ? "disabled" : ""}>记录已核对（不自动启用）</button></form>` : ""}</section>`;}
        html+="</article>";
      }
    }
    return `${html}</section>`;
  }
  function fromSession({session_id="",workspace="",project_id="",text="",model=""}={}){restore();if(pending||draftError){formError="请先核对当前管理请求或草稿。";open=true;paint();return false;}if(draft&&(draft.name||draft.prompt)){formError="已保留现有草稿，请先保存或明确放弃，再从会话创建。";open=true;paint();return false;}edit();draft.project_id=project_id;draft.workspace=workspace;draft.model=model||ctx().defaultModel||ctx().conn?.health?.model||"";draft.prompt=text;draft.name=session_id?`会话 ${session_id.slice(0,8)} 的自动化`:"新自动化";persist();paint();return true;}
  return {load,render,dispatch,open:edit,fromSession,
    reset(){generation++;data=null;error="";loading=false;draft=null;draftError="";formError="";pending=null;pendingError="";sending=false;restored=false;open=false;notes.clear();},
    input(e){const n=e.target;if(n.dataset.autoField && draft && !draftError){if(["name","workspace","project_id","model","prompt","kind","at","seconds","timezone","time","weekdays"].includes(n.dataset.autoField)){draft[n.dataset.autoField]=n.value;if(n.dataset.autoField==="project_id")draft.workspace=ctx().projects.find(p=>p.project_id===n.value)?.workspace||"";formError="";persist();if(["kind","project_id"].includes(n.dataset.autoField))paint();}return true;}if(n.dataset.autoNote){notes.set(n.dataset.autoNote,n.value);store()?.setDraft(NOTE(n.dataset.autoNote),n.value);return true;}return false;},
    onSubmit(e){const kind=e.target.dataset.autoForm;if(!kind)return false;e.preventDefault();if(!writable() || pending)return true;
      if(kind==="configure" && draft){try{const definition=automationDefinition(draft);if(!draft.create && !automationEligibility(draft.task_id,data.state).canEdit)throw new Error("存在尚未收束的运行，不能改写配置。");request("configure","/desktop/automations/configure",{revision:data.state.revision,task_id:draft.task_id,definition,create:draft.create});}catch(e){formError=e.message;paint();}}
      if(kind==="acknowledge"){const id=e.target.dataset.autoRun,text=note(id).trim();if(data.state.runs[id]?.state!=="unknown" || !text || text.length>1000){formError="请先核对未知运行，并填写不超过 1000 字的说明。";paint();}else request("acknowledge",`/desktop/automation-runs/${encodeURIComponent(id)}/acknowledge`,{revision:data.state.revision,note:text});}return true;},
    click(e){const n=e.target.closest("[data-auto-action]");if(!n)return false;const a=n.dataset.autoAction,id=n.dataset.autoId;
      if(a==="reload")load();if(a==="new")edit();if(a==="edit")edit(id);if(a==="close"){open=false;paint();}if(a==="discard" && !pending){draft=null;open=false;formError="";store()?.deleteDraft(DRAFT);paint();}
      if(a==="retry")dispatch();if(a==="forget" && !sending){if(pending?.kind==="configure" && draft?.task_id===pending.body.task_id && JSON.stringify(data?.state.tasks[draft.task_id]?.definition)===JSON.stringify(pending.body.definition)){draft.create=false;persist();}pending=null;store()?.deleteDraft(PENDING);paint();}
      if(["enable","disable"].includes(a) && data?.state.tasks[id] && (a==="disable" || automationEligibility(id,data.state).canEnable))request("enabled",`/desktop/automations/${encodeURIComponent(id)}/enabled`,{revision:data.state.revision,enabled:a==="enable"});
      if(a==="session" && n.dataset.autoSession)ui.openSession(n.dataset.autoSession);return true;},
  };
}
