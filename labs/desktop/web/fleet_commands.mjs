import {workflowStatusText, escapeHTML as h} from "./client.mjs";

const labels = {new:"新建协作",adopt:"采用已有状态",plan:"规划任务",spawn:"启动成员",resume:"续跑成员",merge:"合并任务",teardown:"清理工作树并结束协作"};
const lifecycle = mode => ["merge","teardown"].includes(mode);
export const fleetCommandDraftKey = (sid, mode, board = "") => `fleet-command:${sid}:${mode}:${["new","adopt"].includes(mode) ? "init" : board || "uninitialized"}`;
const freshDraft = () => ({missions:[{id:crypto.randomUUID(),title:"",kind:"build",scope:"",deps:[]}],name:"",kind:"worker",target:"",model:"",effort:"",instructions:"",member:""});
const memberKey = m => JSON.stringify([m.name,m.run_id]);
const lines = text => text.split(/\r?\n/).map(s => s.trim()).filter(Boolean);
const allowed = (mode,caps,draft) => !!caps?.available && (mode === "new" ? caps.can_init : mode === "adopt" ? caps.can_adopt : mode === "plan" ? caps.can_plan : mode === "spawn" ? caps.can_spawn : lifecycle(mode) ? !!caps.board_id && Object.hasOwn(caps,`can_${mode}`) && (mode !== "merge" || caps.missions?.some(m => m.id === draft.target)) : caps.members?.some(m => memberKey(m) === draft.member && m.can_resume));
export function fleetLifecycleExecutable(mode,preview) {
  if (!lifecycle(mode)) return true;
  if (preview?.can_execute !== true || typeof preview.preview_token !== "string" || !preview.preview_token || !Array.isArray(preview.blockers) || preview.blockers.length) return false;
  return mode !== "teardown" || preview.teardown?.archives_retained === true && Array.isArray(preview.teardown.worktrees) && preview.teardown.worktrees.every(t => t.can_remove === true && t.dirty === false && t.live === false);
}
export function fleetCommandRequest(mode,draft,caps) {
  if (["new","adopt"].includes(mode)) return {action:"init",mode:mode === "new" ? "new" : "adopt"};
  if (!caps?.board_id) throw new Error("没有当前协作实例；历史记录须先明确采用。");
  if (mode === "teardown") return {action:"teardown",board_id:caps.board_id};
  if (mode === "merge") {
    if (!draft.target || !(caps.missions || []).some(m => m.id === draft.target)) throw new Error("请选择当前协作中的任务。");
    return {action:"merge",board_id:caps.board_id,mission_id:draft.target};
  }
  if (mode === "plan") {
    if (!draft.missions.length || draft.missions.some(m => !m.title.trim())) throw new Error("请填写每项任务的标题。");
    const known = new Set((caps.missions || []).map(m => m.id));
    if (draft.missions.some(m => m.deps.some(id => !known.has(id)))) throw new Error("依赖任务已变化，请重新选择。");
    return {action:"plan",board_id:caps.board_id,missions:draft.missions.map(m => ({title:m.title.trim(),kind:m.kind,scope:lines(m.scope),deps:[...m.deps]}))};
  }
  const extra = Object.fromEntries([['effort',draft.effort],['instructions',draft.instructions.trim()]].filter(([,value]) => value));
  if (mode === "spawn") {
    if (!/^[a-z][a-z0-9_-]{1,31}$/.test(draft.name)) throw new Error("成员名须以小写字母开头，长 2–32 字符，仅含小写字母、数字、下划线或连字符。");
    if (!draft.target || !(caps.missions || []).some(m => m.id === draft.target)) throw new Error("请选择当前协作中的目标任务。");
    return {action:"spawn",board_id:caps.board_id,name:draft.name,kind:draft.kind,[draft.kind === "reviewer" ? "review_target" : "mission_id"]:draft.target,...(draft.model.trim() ? {model:draft.model.trim()} : {}),...extra};
  }
  const member = caps.members?.find(m => memberKey(m) === draft.member);
  if (!member?.can_resume) throw new Error(member?.resume_reason || "请选择可续跑的成员代次。");
  return {action:"resume",board_id:caps.board_id,name:member.name,run_id:member.run_id,...extra};
}
function textField(name,label,value,{rows = 2,max = 8000,placeholder = ""} = {}) {
  return `<label>${label}<textarea id="fc-${name}" name="${name}" rows="${rows}" maxlength="${max}" placeholder="${h(placeholder)}">${h(value)}</textarea></label>`;
}
function renderBlockers(blockers = []) {
  return blockers.length ? `<div class="fleet-blockers" role="status"><strong>当前阻断原因</strong><ul>${blockers.map(b => `<li>${h(b.message)} <span class="note">${h(b.code)}</span></li>`).join("")}</ul></div>` : "";
}
export function renderFleetMergeFacts(merge,blockers = []) {
  if (!merge) return renderBlockers(blockers);
  const mission = merge.mission || {}, review = merge.review || {}, diff = merge.diff || {};
  return `<section class="fleet-merge-facts"><h4>${h(mission.id)} · ${h(mission.title || "任务差异")}</h4>${mission.kind === "survey" ? `<p class="note">只读调研收账；不修改代码，不执行 Git 合并。</p>` : `<dl class="fleet-facts"><div><dt>合入分支</dt><dd>${h(merge.base_branch)} · <code>${h(merge.base_head || "未核实")}</code></dd></div><div><dt>任务分支</dt><dd>${h(merge.target_branch)} · <code>${h(merge.target_head || "未核实")}</code></dd></div><div><dt>共同基点</dt><dd><code>${h(merge.merge_base || "未核实")}</code></dd></div></dl><p class="note">${review.round ? `最新评审：第 ${h(review.round)} 轮 · ${h(review.verdict)} · 评审提交 ${h(review.reviewed_commit || "未记录")}` : "尚无可核实的最新评审。"}</p><p class="note">${review.current === true ? "评审提交与当前任务提交一致；仍需满足其他合并条件。" : "评审尚未覆盖当前任务提交，不能视为有效合并许可。"}</p><p class="note">未预检合并冲突；当前准入检查通过不保证实际合并无冲突。</p>`}${merge.deps?.length ? `<p class="note">依赖：${merge.deps.map(d => `${h(d.id)} · ${d.ready ? "已收账" : "尚未收账"} (${h(d.status || "未知")})`).join("；")}</p>` : ""}${renderBlockers(blockers)}${mission.kind !== "survey" ? `<details open data-key="fleet-changed-files-${h(mission.id)}"><summary>已提交改动文件（${merge.changed_files?.length || 0}）</summary>${merge.changed_files?.length ? `<ul>${merge.changed_files.map(file => `<li><code>${h(file)}</code></li>`).join("")}</ul>` : `<p class="note">没有已确认的改动文件。</p>`}</details><details open data-key="fleet-diff-${h(mission.id)}"><summary>已提交差异</summary>${diff.available ? `<pre class="fleet-diff">${h(diff.text || "当前差异为空")}</pre>` : `<p class="note">差异不可用。</p>`}${diff.truncated ? `<p class="fleet-warning">差异已截断，当前显示不是完整改动。</p>` : ""}${diff.note ? `<p class="note">${h(diff.note)}</p>` : ""}</details>` : ""}</section>`;
}
export function renderFleetCleanupFacts(cleanup,blockers = []) {
  if (!cleanup) return renderBlockers(blockers);
  const trees = cleanup.worktrees || [], unmerged = trees.filter(t => !t.merged);
  return `<section class="fleet-cleanup-facts"><h4>清理工作树并结束协作</h4><p>这是整个协作实例的结束操作。不会合并代码，也不会替你停止仍活跃的成员。</p>${renderBlockers(blockers)}${unmerged.length ? `<div class="callout warning"><strong>存在未合并的任务分支</strong><p>以下工作树中的已提交改动尚未合入目标分支。清理后原分支仍保留，可从分支查看成果。</p><ul>${unmerged.map(t => `<li>${h(t.mission_id)} · ${h(t.branch || "分支未核实")}</li>`).join("")}</ul></div>` : ""}<ul class="fleet-cleanup-list">${trees.map(t => `<li><strong>${h(t.mission_id)} · ${t.can_remove ? "可移除工作树" : "不能移除"}</strong><p><code>${h(t.path)}</code></p><p class="note">${h(t.branch || "分支未核实")} · ${t.merged ? "已合并" : "未合并，分支保留"} · ${t.dirty === false ? "工作树干净" : t.dirty === true ? "有未提交内容（含忽略文件）" : "工作树状态未知"} · ${t.live === false ? "未观察到活跃成员" : t.live === true ? "成员仍活跃" : "成员活动状态未知"}</p>${t.reason ? `<p class="note">${h(t.reason)}</p>` : ""}</li>`).join("") || `<li>没有待移除的工作树；执行仍会结束当前协作实例。</li>`}</ul><p class="note">保留分支：${(cleanup.retained_branches || []).map(h).join("、") || "没有已记录分支"}。</p><p class="note">${cleanup.archives_retained === true ? "审计轨迹与成员档案保留。" : "档案保留状态尚未确认，请勿执行。"}</p></section>`;
}
export function renderFleetCommandForm(state,disabled = false) {
  if (!state) return "";
  let html = `<section class="fleet-command-section" aria-label="统筹协作操作"><div class="section-heading"><h3 id="fleet-command-title" tabindex="-1">协作设置</h3><button type="button" class="quiet" data-fc-action="${state.open ? "close" : "open"}">${state.open ? "收起" : "发起协作操作"}</button></div>`;
  if (!state.open) return `${html}</section>`;
  const caps = state.caps, draft = state.draft, mode = state.mode;
  html += `<p class="note">拆分任务，安排成员执行，并查看各自进度。每次操作先预览，再确认，执行时仍按工具审批设置处理。</p><div class="fleet-command-tabs" role="group" aria-label="操作类型">${Object.entries(labels).filter(([key]) => !lifecycle(key) || caps && Object.hasOwn(caps,`can_${key}`)).map(([key,label]) => `<button type="button" data-fc-action="mode" data-fc-mode="${key}" aria-pressed="${mode === key}">${label}</button>`).join("")}</div>`;
  if (state.error) html += `<p class="form-error" role="alert">${h(workflowStatusText(state.error))}</p>`;
  if (state.loadError && state.loadError !== state.error) html += `<p class="form-error" role="alert">${h(workflowStatusText(state.loadError))}</p>`;
  if (state.draftError) html += `<p class="form-error" role="alert">${h(state.draftError)} 原始草稿未覆盖。</p>`;
  html += `<button type="button" class="quiet" data-fc-action="reload" ${state.loading ? "disabled" : ""}>${state.loading ? "正在读取…" : "重新核对可用操作"}</button>`;
  if (!caps) return `${html}<p class="note">尚未读取到当前可用操作；保留草稿，不能提交。</p></section>`;
  if (caps.reason) html += `<p class="note">${h(workflowStatusText(caps.reason))}</p>`;
  if (!caps.board_id && !["new","adopt"].includes(mode)) html += `<p class="note">尚无当前协作实例。历史看板只读，请先新建或明确采用当前目录已有状态。</p>`;
  html += `<form id="fleet-command-form" data-fc-key="${h(state.draftKey)}"><fieldset ${state.draftError ? "disabled" : ""}><legend>${labels[mode]}</legend>`;
  if (mode === "new") html += `<p>在当前会话工作目录创建新的协作状态。目录已有状态时不会覆盖，请改用“采用已有状态”。</p>`;
  if (mode === "adopt") html += `<p>明确读取当前目录已有的协作状态，建立本进程的新协作实例。旧线程和旧成员运行不会自动恢复；随后需按当前可用操作启动或续跑。</p>`;
  if (mode === "plan") {
    html += `<p class="note">任务编号由预览分配。此表单的依赖只选择已有任务；先建立前序任务后，可再规划依赖它的任务。</p>`;
    html += draft.missions.map((mission,index) => `<fieldset class="fleet-mission-editor"><legend>任务 ${index + 1}</legend><label>标题<input id="fc-title-${h(mission.id)}" data-fc-row="${h(mission.id)}" name="title" value="${h(mission.title)}" maxlength="300" required></label><label>类型<select id="fc-kind-${h(mission.id)}" data-fc-row="${h(mission.id)}" name="kind"><option value="build" ${mission.kind === "build" ? "selected" : ""}>建设任务</option><option value="survey" ${mission.kind === "survey" ? "selected" : ""}>调研任务</option></select></label><label>范围（每行一个工作区内相对路径或模式）<textarea id="fc-scope-${h(mission.id)}" data-fc-row="${h(mission.id)}" name="scope" rows="2" maxlength="12000">${h(mission.scope)}</textarea></label><div class="fleet-dependencies"><span>依赖已有任务</span>${(caps.missions || []).map(m => `<label><input id="fc-dep-${h(mission.id)}-${h(m.id)}" type="checkbox" data-fc-row="${h(mission.id)}" data-fc-dep="${h(m.id)}" ${mission.deps.includes(m.id) ? "checked" : ""}>${h(m.id)} · ${h(m.title)}</label>`).join("") || `<p class="note">暂无已有任务。</p>`}</div><button type="button" class="quiet" data-fc-action="remove" data-fc-row="${h(mission.id)}" ${draft.missions.length <= 1 ? "disabled" : ""}>移除此任务</button></fieldset>`).join("");
    html += `<button type="button" data-fc-action="add" ${draft.missions.length >= 20 ? "disabled" : ""}>添加任务</button>`;
  }
  if (mode === "merge") html += `<label>任务<select id="fc-merge-target" name="target"><option value="">选择要检查的任务</option>${(caps.missions || []).map(m => `<option value="${h(m.id)}" ${draft.target === m.id ? "selected" : ""}>${h(m.id)} · ${h(m.title)} · ${m.kind === "survey" ? "调研收账" : "代码合并"}</option>`).join("")}</select></label><p class="note">只检查已提交的分支差异。调研任务只收账，不发生代码合并。历史看板必须先明确采用，才能操作当前实例。</p><button type="button" data-fc-action="changes" ${!draft.target || state.changesLoading || state.loadError ? "disabled" : ""}>${state.changesLoading ? "正在检查差异…" : "查看差异与合并阻断"}</button>`;
  if (mode === "teardown") html += `<p>此操作会移除本次清单中的工作树并结束整个协作实例，不会自动合并任务。分支与审计、成员档案保留；未合并但已提交的改动仍留在原分支。</p><p class="note">任何工作树存在未提交内容（包括忽略文件）、活跃成员或无法核实的状态，整次清理都会被阻止。</p>`;
  if (mode === "spawn") html += `<label>成员名<input id="fc-name" name="name" value="${h(draft.name)}" maxlength="32" autocomplete="off" placeholder="例如 builder" required></label><label>角色<select id="fc-role" name="kind"><option value="worker" ${draft.kind === "worker" ? "selected" : ""}>执行成员</option><option value="reviewer" ${draft.kind === "reviewer" ? "selected" : ""}>评审成员</option></select></label><label>${draft.kind === "reviewer" ? "评审目标" : "领取任务"}<select id="fc-target" name="target"><option value="">选择目标任务</option>${(caps.missions || []).map(m => `<option value="${h(m.id)}" ${draft.target === m.id ? "selected" : ""}>${h(m.id)} · ${h(m.title)} · ${h(m.status || "")}</option>`).join("")}</select></label><datalist id="fc-models">${(caps.models || []).map(model => `<option value="${h(model)}"></option>`).join("")}</datalist><label>成员模型<input id="fc-model" name="model" value="${h(draft.model)}" list="fc-models" maxlength="512" placeholder="${h(caps.default_model || "继承默认模型")}"></label>`;
  if (mode === "resume") {
    const member = (caps.members || []).find(m => memberKey(m) === draft.member);
    html += `<label>成员与原运行代次<select id="fc-member" name="member"><option value="">选择可续跑成员</option>${(caps.members || []).map(m => `<option value="${h(memberKey(m))}" ${memberKey(m) === draft.member ? "selected" : ""} ${!m.can_resume ? "disabled" : ""}>${h(m.name)} · ${h(m.run_id || "无运行代次")}${!m.can_resume ? ` · ${h(m.resume_reason || "不可续跑")}` : ""}</option>`).join("")}</select></label>${member ? `<p class="note">固定原角色 ${h(member.kind)} · 模型 ${h(member.model || "原配置")} · 目标 ${h(member.mission_id || member.review_target || "未记录")}。不会修改原角色、模型或目标。</p>` : ""}`;
    if (draft.member && !member) html += `<p class="note">已保存草稿指向旧运行代次，不能自动用于新代成员。请明确重新选择续跑来源。</p>`;
    if (!(caps.members || []).some(m => m.can_resume)) html += `<p class="note">当前没有可续跑成员。</p>`;
  }
  if (["spawn","resume"].includes(mode)) html += `<label>推理强度<select id="fc-effort" name="effort"><option value="">继承默认或原配置</option>${(caps.efforts || []).map(e => `<option value="${h(e)}" ${draft.effort === e ? "selected" : ""}>${h(e)}</option>`).join("")}</select></label>${textField("instructions","补充说明",draft.instructions)}`;
  const unavailable = disabled || state.loading || state.previewLoading || state.draftError || !allowed(mode,caps,draft);
  html += `<div class="actions"><button type="submit" ${unavailable ? "disabled" : ""}>${state.previewLoading ? "正在预览…" : "预览操作"}</button>${!allowed(mode,caps,draft) ? `<span class="note">当前状态不允许此操作，请核对实例、任务或成员状态。</span>` : ""}</div></fieldset></form>`;
  if (mode === "merge" && (state.changes || state.changesError)) html += `<section class="fleet-changes" aria-label="任务差异检查">${state.changesError ? `<p class="form-error">${h(workflowStatusText(state.changesError))}${state.changes ? "；下方保留上次检查，可能过期。" : ""}</p>` : `<p class="note">只读检查，不授予合并权限。提交前需重新生成操作预览。</p>`}${state.changes ? renderFleetMergeFacts(state.changes.merge,state.changes.blockers) : ""}</section>`;
  if (state.preview) {
    const p = state.preview;
    const needsConfirmation = lifecycle(mode), previewValid = fleetLifecycleExecutable(mode,p);
    const confirmation = mode === "merge" ? p.merge?.mission?.kind === "survey" ? "我确认将此调研任务收账；不发生代码合并。" : "我已核对差异、目标分支与评审，确认合并此任务。" : "我确认移除清单中的工作树并结束协作；未合并分支、审计与档案仍保留。";
    html += `<section class="fleet-command-preview"><h4>确认：${h(workflowStatusText(p.label || labels[mode]))}</h4><p class="note">工具 ${h(p.tool)} · 基准分支 ${h(p.base_branch || "未记录")} · HEAD ${h(p.repo_head || "未记录")}</p>${p.existing ? `<p>已有状态 ${h(p.existing.state_id)} · ${h(p.existing.missions_count)} 项任务 · ${h(p.existing.members_count)} 位成员</p>` : ""}${p.member ? `<p class="note">成员 ${h(p.member.name)} · 模型 ${h(p.member.model)} · 推理强度 ${h(p.member.effort || "默认")} · 工作目录 ${h(p.member.workspace || "执行时创建")} ${p.member.source_run_id ? `· 来源运行 ${h(p.member.source_run_id)}` : ""}</p>` : ""}${(p.warnings || []).map(w => `<p class="note">${h(w)}</p>`).join("")}${p.missions?.length ? `<ul>${p.missions.map(m => `<li><strong>${h(m.id)} · ${h(m.title)}</strong><p class="note">${h(m.kind)} · 范围 ${(m.scope || []).map(h).join("、") || "未指定"} · 依赖 ${(m.deps || []).map(h).join("、") || "无"}</p></li>`).join("")}</ul>` : ""}${p.merge ? renderFleetMergeFacts(p.merge,p.blockers) : p.teardown ? renderFleetCleanupFacts(p.teardown,p.blockers) : renderBlockers(p.blockers)}<details data-key="fleet-command-preview-args"><summary>查看确切工具参数</summary><pre>${h(JSON.stringify(p.args,null,2))}</pre></details><p class="note">确认后仅提交此预览；如状态变化将要求重新预览，审批仍按工具权限执行。</p>${needsConfirmation ? `<label class="fleet-explicit-confirm"><input id="fc-confirm-${h(mode)}" type="checkbox" data-fc-confirm="${h(mode)}" ${state.confirmed === p.preview_token && previewValid ? "checked" : ""} ${!previewValid || unavailable ? "disabled" : ""}>${h(confirmation)}</label>${!previewValid ? `<p class="form-error">此预览不允许执行。解决阻断后重新预览，不能使用旧凭据提交。</p>` : ""}` : ""}<button type="button" class="primary" data-fc-action="confirm" ${unavailable || !previewValid || needsConfirmation && state.confirmed !== p.preview_token ? "disabled" : ""}>${mode === "merge" ? "确认提交合并或收账" : mode === "teardown" ? "确认清理并结束协作" : "确认提交操作"}</button></section>`;
  }
  return `${html}</section>`;
}
function parseDraft(raw) {
  const draft = JSON.parse(raw);
  if (!draft || !Array.isArray(draft.missions) || draft.missions.length < 1 || draft.missions.length > 20 || ![draft.name,draft.kind,draft.target,draft.model,draft.effort,draft.instructions,draft.member].every(v => typeof v === "string") || !["worker","reviewer"].includes(draft.kind) || draft.missions.some(m => !m || ![m.id,m.title,m.scope].every(v => typeof v === "string") || !["build","survey"].includes(m.kind) || !Array.isArray(m.deps) || m.deps.some(d => typeof d !== "string"))) throw new Error("无法读取已保存的操作草稿。");
  return draft;
}
export function createFleetCommandController(ui) {
  const states = new Map(), drafts = new Map();
  const context = () => ui.context();
  const path = sid => `/session/${encodeURIComponent(sid)}/fleet-commands`;
  const paint = sid => { if (context().active && context().sid === sid) ui.render(); };
  const valid = (conn,sid,s) => context().conn === conn && states.get(sid) === s;
  const blocked = () => { const c = context(); return !c.active || !c.writable || states.get(c.sid)?.loadError || c.session?.busy || c.pending.some(a => ["fleet_command","prompt","steer","weave","contest"].includes(a.kind)); };
  function bind(sid,s) {
    const key = fleetCommandDraftKey(s.draftIdentity,s.mode,s.caps?.board_id);
    if (!drafts.has(key)) {
      const raw = context().conn?.localState?.getDraft(key);
      try { drafts.set(key,{draft:raw ? parseDraft(raw) : freshDraft(),error:""}); }
      catch (error) { drafts.set(key,{draft:freshDraft(),error:error.message}); }
    }
    const entry = drafts.get(key); s.draftKey = key; s.draft = entry.draft; s.draftError = entry.error;
  }
  function state(sid) { if (!states.has(sid)) states.set(sid,{draftIdentity:ui.draftIdentity?.(sid)||sid,open:false,mode:"new",caps:null}); const s = states.get(sid); bind(sid,s); return s; }
  function changed(sid,s) { s.preview = null; s.confirmed = null; s.previewBody = null; s.error = ""; context().conn?.localState?.setDraft(s.draftKey,JSON.stringify(s.draft)); }
  async function load(conn,sid) {
    if (!conn || context().conn !== conn || conn.health?.fleet_command_protocol !== 1) return;
    const s = state(sid); if (s.loading) return; s.loading = true;
    try {
      const caps = await conn.client.get(`${path(sid)}/capabilities`,conn.controller.signal);
      if (!valid(conn,sid,s)) return;
      if (!caps || !Array.isArray(caps.missions) || !Array.isArray(caps.members)) throw new Error("协作能力响应格式不正确。");
      const fingerprint = JSON.stringify(caps);
      if (s.fingerprint && s.fingerprint !== fingerprint && s.preview) { s.preview = null; s.confirmed = null; s.error = "协作状态已变化，请重新预览。"; }
      if (s.fingerprint && s.fingerprint !== fingerprint && s.changes) s.changesError = "协作状态已变化，请重新检查差异";
      if (s.loadError && s.error === s.loadError) s.error = "";
      if (s.caps?.board_id !== caps.board_id) { s.changes = null; s.changesError = ""; s.changesSerial = (s.changesSerial || 0) + 1; s.changesLoading = false; }
      s.fingerprint = fingerprint; s.caps = caps; s.loadError = ""; bind(sid,s);
    } catch (error) { if (valid(conn,sid,s)) { s.preview = null; s.confirmed = null; s.error = `读取可用操作失败：${error.message}；已显示的信息可能过期，不能提交。`; s.loadError = s.error; } }
    finally { if (valid(conn,sid,s)) { s.loading = false; paint(sid); } }
  }
  async function preview() {
    const {sid,conn} = context(); if (!sid || blocked()) return;
    const s = state(sid); if (s.previewLoading || s.loading || s.draftError || !allowed(s.mode,s.caps,s.draft)) return;
    const snapshot = JSON.stringify(s.draft), key = s.draftKey, fingerprint = s.fingerprint;
    s.preview = null; s.confirmed = null; s.error = "";
    let body;
    try { body = fleetCommandRequest(s.mode,s.draft,s.caps); } catch(error) { s.error = error.message; paint(sid); return; }
    s.previewLoading = true; paint(sid);
    try {
      const result = await conn.client.post(`${path(sid)}/preview`,body,{signal:conn.controller.signal});
      if (!valid(conn,sid,s) || s.draftKey !== key || snapshot !== JSON.stringify(s.draft) || fingerprint !== s.fingerprint) return;
      if (lifecycle(body.action)) {
        if (result.action !== body.action || result.board_id !== body.board_id || body.action === "merge" && result.merge?.mission?.id !== body.mission_id || typeof result.can_execute !== "boolean" || !Array.isArray(result.blockers) || !result[body.action === "merge" ? "merge" : "teardown"]) throw new Error("服务没有返回完整的操作检查结果。");
        if (result.can_execute && !result.preview_token) throw new Error("服务没有返回有效预览凭据。");
      } else if (!result.preview_token) throw new Error("服务没有返回有效预览凭据。");
      s.preview = result; s.previewBody = body; s.previewDraft = snapshot;
    } catch(error) { if (valid(conn,sid,s)) s.error = `预览失败：${error.message}`; }
    finally { if (valid(conn,sid,s)) { s.previewLoading = false; paint(sid); } }
  }
  function input(event) {
    const node = event.target;
    if (node.dataset.fcConfirm) {
      const {sid,active} = context(); if (!sid || !active) return true;
      const s = state(sid); if (node.dataset.fcConfirm === s.mode && fleetLifecycleExecutable(s.mode,s.preview) && s.preview?.preview_token && !blocked()) s.confirmed = node.checked ? s.preview.preview_token : null;
      paint(sid); return true;
    }
    const form = node.closest?.("#fleet-command-form"); if (!form) return false;
    const {sid,active} = context(); if (!sid || !active) return true;
    const s = state(sid); if (s.draftError || form.dataset.fcKey !== s.draftKey) return true;
    if (node.dataset.fcRow) {
      const row = s.draft.missions.find(m => m.id === node.dataset.fcRow); if (!row) return true;
      if (node.dataset.fcDep) row.deps = node.checked ? [...new Set([...row.deps,node.dataset.fcDep])] : row.deps.filter(d => d !== node.dataset.fcDep);
      else if (["title","kind","scope"].includes(node.name)) row[node.name] = node.value;
    } else if (["name","kind","target","model","effort","instructions","member"].includes(node.name)) s.draft[node.name] = node.value;
    if (s.mode === "merge") { s.changes = null; s.changesError = ""; s.changesSerial = (s.changesSerial || 0) + 1; s.changesLoading = false; }
    changed(sid,s); paint(sid); return true;
  }
  function click(event) {
    const node = event.target.closest?.("[data-fc-action]"); if (!node) return false;
    const {sid,conn,active} = context(); if (!sid || !active) return true;
    const s = state(sid), action = node.dataset.fcAction;
    if (action === "open") { s.open = true; load(conn,sid); }
    if (action === "close") s.open = false;
    if (action === "reload") { load(conn,sid); return true; }
    if (action === "changes") { changes(); return true; }
    if (action === "mode" && Object.hasOwn(labels,node.dataset.fcMode)) { s.open = true; s.changes = null; s.changesError = ""; s.changesSerial = (s.changesSerial || 0) + 1; s.changesLoading = false; s.mode = node.dataset.fcMode; s.preview = null; s.confirmed = null; s.error = ""; bind(sid,s); }
    if (!s.draftError && action === "add" && s.draft.missions.length < 20) { s.draft.missions.push(freshDraft().missions[0]); changed(sid,s); }
    if (!s.draftError && action === "remove" && s.draft.missions.length > 1) { s.draft.missions = s.draft.missions.filter(m => m.id !== node.dataset.fcRow); changed(sid,s); }
    if (action === "confirm" && !blocked() && !s.loading && !s.draftError && allowed(s.mode,s.caps,s.draft) && s.preview && (!lifecycle(s.mode) || fleetLifecycleExecutable(s.mode,s.preview) && s.confirmed === s.preview.preview_token) && JSON.stringify(s.draft) === s.previewDraft) ui.submit(`${sid}:fleet_command:`,{kind:"fleet_command",sid,path:path(sid),body:{...s.previewBody,preview_token:s.preview.preview_token},draftKey:s.draftKey,draftText:s.previewDraft});
    paint(sid); return true;
  }
  async function changes() {
    const {sid,conn,active} = context(); if (!sid || !conn || !active) return;
    const s = state(sid); if (s.mode !== "merge" || s.loadError || !s.caps?.available || !s.caps.board_id || !s.caps.missions.some(m => m.id === s.draft.target)) return;
    const board = s.caps.board_id, target = s.draft.target, serial = s.changesSerial = (s.changesSerial || 0) + 1;
    s.changesLoading = true; s.changesError = ""; s.preview = null; s.confirmed = null; paint(sid);
    try {
      const data = await conn.client.get(`/session/${encodeURIComponent(sid)}/fleets/${encodeURIComponent(board)}/missions/${encodeURIComponent(target)}/changes`,conn.controller.signal);
      if (!valid(conn,sid,s) || s.mode !== "merge" || s.caps?.board_id !== board || s.draft.target !== target || s.changesSerial !== serial) return;
      if (data.board_id !== board || data.mission_id !== target || !data.merge || !Array.isArray(data.blockers)) throw new Error("差异响应身份或格式不匹配。");
      s.changes = data;
    } catch(error) { if (valid(conn,sid,s) && s.changesSerial === serial) s.changesError = `差异检查失败：${error.message}`; }
    finally { if (valid(conn,sid,s) && s.changesSerial === serial) { s.changesLoading = false; paint(sid); } }
  }
  return {load,input,click,preview,changes,
    open(){const {sid,conn,active}=context();if(!sid||!active)return;state(sid).open=true;if(conn)load(conn,sid);paint(sid);},
    render(sid) { const s = state(sid); return renderFleetCommandForm(context().conn?.healthy === false && s.changes ? {...s,changesError:"服务连接不可用，差异是上次记录"} : s,blocked()); },
    onSubmit(event) { if (event.target.id !== "fleet-command-form") return false; event.preventDefault(); if (event.target.dataset.fcKey === state(context().sid).draftKey) preview(); return true; },
    reset() { states.clear(); drafts.clear(); },
    accepted(conn,action) {
      if (action.kind !== "fleet_command" || action.state !== "accepted") return;
      // A transport receipt precedes tool approval and execution. Keep the configuration
      // so a later guard failure or denial does not discard the user's plan.
      const s = states.get(action.sid);
      if (s?.draftKey === action.draftKey) { s.preview = null; s.confirmed = null; s.previewBody = null; if (JSON.stringify(s.draft) === action.draftText) s.open = false; }
    },
    rejected(action,error) {
      if (action.kind !== "fleet_command") return;
      const s = states.get(action.sid); if (!s) return;
      s.preview = null; s.confirmed = null;
      s.error = error.recorded ? "此操作已有提交记录，执行结果须核对；不会自动重放。原草稿已保留。" : error.status === 409 ? "预览已过期或协作状态已变化。核对并清除此回执后，请重新预览；草稿已保留。" : `${error.message}；草稿已保留，请先核对待提交回执。`;
    },
  };
}
