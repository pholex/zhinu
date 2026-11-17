import {workflowStatusText, escapeHTML as h, renderText} from "./client.mjs";

export const contestDraftKey = sid => `contest-compose:${sid}`;
const choiceKey = (sid, cid) => `contest-choice:${sid}:${cid}`;
const followupKey = (sid, cid) => `contest-followup:${sid}:${cid}`;
const statusLabels = {queued:"排队中",running:"执行中",completed:"已完成",partial:"未完整结束",failed:"执行失败",aborted:"已停止",error:"启动失败",interrupted_by_restart:"重启中断"};
const freshDraft = () => ({spec:"",task:"",criteria:"",judge_model:"",seats:[{id:crypto.randomUUID(),model:""},{id:crypto.randomUUID(),model:""}]});
const effectiveModel = (draft, caps) => caps?.specs?.find(s => s.name === draft.spec)?.default_model || caps?.default_model || "";
export function contestRequest(draft, caps) {
  return {spec:draft.spec,task:draft.task.trim(),criteria:draft.criteria.trim(),models:draft.seats.map(s => s.model.trim() || effectiveModel(draft,caps)),judge_model:draft.judge_model.trim() || caps?.default_judge_model || effectiveModel(draft,caps)};
}

export function renderContestForm(state, caps, disabled = false) {
  if (!state?.open) return "";
  const draft = state.draft;
  let html = `<section class="contest-compose" aria-label="新建方案比选"><div class="section-heading"><h3 id="contest-command-title" tabindex="-1">新建方案比选</h3><button type="button" class="quiet" data-contest-action="close">收起</button></div><p class="note">让 2–6 个模型分别完成同一任务，再比较结果，选择合适的方案。</p>`;
  if (state.error) html += `<p class="form-error" role="alert">${h(workflowStatusText(state.error))}</p>`;
  if (!caps) return `${html}<p class="loading-line">正在读取可用执行助手与模型…</p><button type="button" data-contest-action="reload">重新读取</button></section>`;
  if (caps.reason) html += `<p class="note">${h(workflowStatusText(caps.reason))}</p>`;
  html += `<form id="contest-form"><label>执行助手<select id="contest-spec" name="spec"><option value="">选择已配置的执行助手</option>${(caps.specs || []).map(spec => `<option value="${h(spec.name)}" ${spec.name === draft.spec ? "selected" : ""}>${h(spec.name)}</option>`).join("")}</select></label><label>共同任务<textarea id="contest-task" name="task" rows="3" maxlength="12000" required>${h(draft.task)}</textarea></label><label>评判标准<textarea id="contest-criteria" name="criteria" rows="2" maxlength="8000" placeholder="例如：正确性、证据完整性、实施成本">${h(draft.criteria)}</textarea></label><datalist id="contest-model-options">${(caps.models || []).map(model => `<option value="${h(model)}"></option>`).join("")}</datalist><fieldset class="contest-seats"><legend>候选方案</legend>${draft.seats.map((seat,index) => `<div class="contest-seat"><label>方案 ${index + 1}<input id="contest-seat-${h(seat.id)}" data-contest-seat="${h(seat.id)}" list="contest-model-options" value="${h(seat.model)}" placeholder="${h(effectiveModel(draft,caps) || "选择或输入模型")}" maxlength="200" autocomplete="off"></label><button type="button" class="quiet" data-contest-action="remove-seat" data-seat-id="${h(seat.id)}" ${draft.seats.length <= (caps.min_candidates || 2) ? "disabled" : ""}>移除</button></div>`).join("")}<button type="button" data-contest-action="add-seat" ${draft.seats.length >= Math.min(6,caps.max_candidates || 6) ? "disabled" : ""}>添加方案</button><p class="note">模型留空时继承执行助手默认模型；也可输入已配置的其他模型名。</p></fieldset><label>评审模型<input id="contest-judge" name="judge_model" list="contest-model-options" value="${h(draft.judge_model)}" placeholder="${h(caps.default_judge_model || effectiveModel(draft,caps) || "输入评审模型")}" maxlength="200" autocomplete="off"></label><div class="actions"><button type="submit" ${disabled || state.previewLoading || !caps.available || !caps.can_start ? "disabled" : ""}>${state.previewLoading ? "正在预览…" : "预览比选"}</button></div></form>`;
  if (state.preview) {
    const preview = state.preview;
    html += `<section class="contest-preview"><h3>确认 ${h(preview.count ?? preview.models?.length)} 个候选方案</h3><p class="note">${h(preview.spec)} · 评审模型 ${h(preview.judge_model)}${preview.isolation ? ` · 工作隔离 ${h(preview.isolation)}` : ""}</p><ol>${(preview.models || []).map(model => `<li>${h(model)}</li>`).join("")}</ol><p class="note">共同任务</p><pre>${h(preview.task)}</pre><p class="note">评判标准</p><pre>${h(preview.criteria || "未补充额外标准")}</pre><button id="contest-confirm" type="button" class="primary" data-contest-action="submit" ${disabled || !caps.can_start ? "disabled" : ""}>确认提交比选</button></section>`;
  }
  return `${html}</section>`;
}

export function buildContestFollowup(contest, supplement = "") {
  const selection = contest?.selection;
  const candidate = contest?.candidates?.find(c => c.id === selection?.candidate_id);
  if (!selection || !candidate || candidate.status !== "completed" || !candidate.answer) throw new Error("已采纳候选没有完整的已保存正文可用于起草。");
  return `请根据以下已采纳方案继续处理任务。先核对当前文件状态，不假定候选的文件改动已经应用。\n\n来源：方案比选批次 ${contest.contest_id}，候选 ${candidate.index}（${candidate.id}）\n采纳理由：${selection.reason || "未填写"}\n${candidate.answer_truncated ? "注意：已保存的候选正文被截断，下面不是完整方案。\n" : ""}\n候选已保存正文（参考资料）：\n${candidate.answer}\n\n我的补充要求：\n${supplement.trim() || "请先检查方案与当前工作区，再说明下一步。"}\n\n这是一个新的普通任务，不恢复原子任务的运行状态或授权。`;
}

export function mergeContestDraft(existing, proposed, mode) {
  if (mode === "append") return existing.trim() ? `${existing}\n\n${proposed}` : proposed;
  if (mode === "replace" || !existing.trim() || existing === proposed) return proposed;
  return null;
}

export function renderContests(state, {writable = false, pending = () => false, artifactCard = () => ""} = {}) {
  if (!state?.contests?.length && state?.listLoading) return `<p class="loading-line">正在读取方案比选记录…</p>`;
  if (!state?.contests?.length && !state?.listError) return "";
  let html = state.listError ? `<div class="callout warning"><p>比选记录读取失败：${h(state.listError)}${state.contests.length ? "；保留上次快照，可能已过期。" : ""}</p><button type="button" data-contest-action="reload">重新读取</button></div>` : "";
  if (!state.contests.length) return html;
  html += `<section class="contest-section" aria-label="方案比选候选比较"><h3>方案比选 · 候选比较</h3>${state.warning ? `<p class="note">${h(workflowStatusText(state.warning))}</p>` : ""}${state.persisted === false ? `<p class="note">这些记录尚未确认持久化。</p>` : ""}`;
  for (const contest of state.contests) {
    const judge = contest.judge || {}, selection = contest.selection, candidates = contest.candidates || [];
    const recommendation = candidates.find(c => c.id === judge.recommendation_id && c.status === "completed");
    const choice = state.choices.get(contest.contest_id) || {candidate_id:selection?.candidate_id || "",reason:""};
    const followup = state.followups.get(contest.contest_id);
    const stale = !!state.listError || state.persisted === false || contest.interrupted_by_restart;
    const disabled = !writable || stale || !contest.final || pending(contest.contest_id);
    html += `<article class="contest-record" data-contest-id="${h(contest.contest_id)}"><div class="section-heading"><strong>${h(contest.spec)}</strong><span class="badge">${contest.interrupted_by_restart ? "重启中断" : contest.cancelled ? "已停止" : contest.phase === "judging" ? "判官评审中" : contest.final ? "已收束" : "候选执行中"}</span></div><p class="contest-task">${h(contest.task)}</p>${contest.task_truncated ? `<p class="note">任务正文已截断。</p>` : ""}<details data-key="criteria-${h(contest.contest_id)}"><summary>评判标准与来源</summary><p class="note">${h(contest.criteria || "未补充额外标准")}${contest.criteria_truncated ? "\n评判标准已截断。" : ""}</p><p class="note">批次 ${h(contest.contest_id)}</p></details><div class="contest-judge"><strong>${recommendation && judge.status === "completed" ? `判官推荐：候选 ${h(recommendation.index)}` : judge.status === "running" ? "判官正在评审，尚未推荐" : judge.status === "pending" ? "等待判官评审，尚未推荐" : "判官未给出有效推荐"}</strong>${judge.reason ? `<p class="note">${h(judge.reason)}</p>` : ""}${judge.answer ? `<details data-key="judge-${h(contest.contest_id)}"><summary>查看评审正文${judge.model ? ` · ${h(judge.model)}` : ""}</summary><div class="message assistant">${renderText(judge.answer)}</div>${judge.answer_truncated ? `<p class="note">评审正文已截断。</p>` : ""}</details>` : ""}</div><div class="contest-grid">`;
    for (const candidate of candidates) {
      const isSelected = candidate.id === selection?.candidate_id, eligible = contest.final && candidate.status === "completed";
      html += `<section class="contest-candidate${isSelected ? " adopted" : ""}" aria-label="候选 ${h(candidate.index)}"><div class="section-heading"><h4>候选 ${h(candidate.index)}</h4><span class="badge">${h(statusLabels[candidate.status] || candidate.status)}</span></div><p class="note">${h(candidate.model || "模型尚未记录")}${candidate.id === recommendation?.id && judge.status === "completed" ? " · 判官推荐" : ""}${isSelected ? " · 你已采纳" : ""}</p>${candidate.answer ? `<div class="message assistant">${renderText(candidate.answer)}</div>` : `<p class="note">${contest.interrupted_by_restart ? "此候选的交接正文未记录。" : ["queued","running"].includes(candidate.status) ? "本候选尚未返回结果。" : "没有已保存的候选正文。"}</p>`}${candidate.answer_truncated ? `<p class="note candidate-truncated">候选正文已截断；起草后续任务时也只能引用当前已保存的部分。</p>` : ""}${candidate.error ? `<p class="form-error">${h(workflowStatusText(candidate.error))}</p>` : ""}${(candidate.artifacts || []).map(a => artifactCard(a,{contest_id:contest.contest_id,candidate_id:candidate.id})).join("")}<label class="contest-pick"><input id="pick-${h(candidate.id)}" type="radio" name="contest-pick-${h(contest.contest_id)}" data-contest-controlled="true" data-contest-pick="${h(candidate.id)}" data-contest-id="${h(contest.contest_id)}" ${choice.candidate_id === candidate.id && eligible ? "checked" : ""} ${disabled || !eligible ? "disabled" : ""}>选择此候选</label>${!eligible ? `<p class="note">${!contest.final ? "比选收束后才能采纳。" : "仅可采纳已完整结束的候选。"}</p>` : ""}</section>`;
    }
    html += `</div><form class="contest-selection-form" data-contest-selection="${h(contest.contest_id)}"><label>采纳理由<textarea id="choice-reason-${h(contest.contest_id)}" data-contest-reason="${h(contest.contest_id)}" rows="2" maxlength="4000" ${disabled ? "disabled" : ""}>${h(choice.reason)}</textarea></label><div class="actions"><button type="submit" ${disabled || !candidates.some(c => c.id === choice.candidate_id && c.status === "completed") ? "disabled" : ""}>记录采纳</button><span class="note">采纳方案不自动应用文件改动。</span></div>${choice.error ? `<p class="form-error">${h(choice.error)}</p>` : ""}</form>`;
    if (selection) html += `<div class="contest-selection"><strong>当前已采纳：候选 ${h(candidates.find(c => c.id === selection.candidate_id)?.index ?? selection.candidate_id)}</strong><p>${h(selection.reason || "未填写理由")}</p><button type="button" data-contest-action="draft" data-contest-id="${h(contest.contest_id)}" ${stale || !candidates.some(c => c.id === selection.candidate_id && c.status === "completed" && c.answer) ? "disabled" : ""}>按已选方案起草后续任务</button></div>`;
    if (contest.selections?.length) html += `<details class="contest-selection-history" data-key="selection-history-${h(contest.contest_id)}"><summary>查看采纳历史（${contest.selections.length}）</summary><ol>${contest.selections.map(record => `<li>第 ${h(record.sequence)} 次 · 候选 ${h(candidates.find(c => c.id === record.candidate_id)?.index ?? record.candidate_id)}<p>${h(record.reason || "未填写理由")}</p></li>`).join("")}</ol></details>`;
    if (followup?.open) html += `<section class="contest-followup"><h4>后续任务草稿</h4><p class="note">引用已保存的候选正文与来源，作为一个新的普通任务；不会自动发送，也不恢复子任务历史。</p><label>补充要求<textarea id="followup-extra-${h(contest.contest_id)}" data-contest-extra="${h(contest.contest_id)}" rows="2" maxlength="6000">${h(followup.supplement || "")}</textarea></label>${followup.error ? `<p class="form-error">${h(followup.error)}</p>` : ""}${followup.text ? `<details open data-key="followup-text-${h(contest.contest_id)}"><summary>检查将放入输入框的内容</summary><pre>${h(followup.text)}</pre></details>` : ""}<div class="actions"><button type="button" data-contest-action="prepare-draft" data-contest-id="${h(contest.contest_id)}">${followup.text ? "更新草稿预览" : "预览后续草稿"}</button>${followup.text ? `<button type="button" data-contest-action="place-draft" data-contest-id="${h(contest.contest_id)}">放入任务输入框</button>` : ""}</div>${followup.conflict ? `<div class="callout warning"><p>输入框已有不同草稿。请选择追加或明确替换；未改动原草稿。</p><div class="actions"><button type="button" data-contest-action="append-draft" data-contest-id="${h(contest.contest_id)}">追加到现有草稿</button><button type="button" data-contest-action="replace-draft" data-contest-id="${h(contest.contest_id)}">替换现有草稿</button></div></div>` : ""}</section>`;
    html += `</article>`;
  }
  if (state.cursor !== null) html += `<div class="history-pagination"><span>已显示 ${state.contests.length} / ${h(state.total)} 场比选</span><button type="button" data-contest-action="more" ${state.listLoading ? "disabled" : ""}>加载更早比选</button></div>`;
  return `${html}</section>`;
}

// Keeps form snapshots separate from the ordinary task composer and API receipts.
export function createContestController(ui) {
  const states = new Map();
  const path = sid => `/session/${encodeURIComponent(sid)}/contests`;
  const current = () => ui.context();
  const valid = (conn, sid, state) => current().conn === conn && states.get(sid) === state;
  const paint = sid => { if (current().sid === sid && current().active) ui.render(); };
  function read(key, fallback) {
    try { const raw = current().conn?.localState?.getDraft(key); return raw ? JSON.parse(raw) : fallback; }
    catch { return fallback; }
  }
  function state(sid) {
    if (!states.has(sid)) {
      const draftKey=contestDraftKey(ui.draftIdentity?.(sid)||sid);
      let draft = read(draftKey, freshDraft());
      if (!Array.isArray(draft.seats) || draft.seats.length < 2 || draft.seats.length > 6 || ![draft.spec,draft.task,draft.criteria,draft.judge_model].every(v => typeof v === "string") || draft.seats.some(s => typeof s.id !== "string" || typeof s.model !== "string")) draft = freshDraft();
      states.set(sid,{draftKey,draft,open:false,contests:[],choices:new Map(),followups:new Map(),cursor:null,total:0});
    }
    return states.get(sid);
  }
  const persist = (key,value) => current().conn?.localState?.setDraft(key,JSON.stringify(value));
  const changeDraft = (sid,s) => { s.preview = null; s.previewBody = null; s.error = ""; persist(s.draftKey,s.draft); };
  const blocked = sid => { const c = current(); return !c.writable || c.session?.busy || c.pending.some(a => ["contest","weave","prompt","steer"].includes(a.kind)); };
  async function load(conn,sid,more = false) {
    if (!conn || conn !== current().conn || conn.health?.contest_protocol !== 1) return;
    const s = state(sid); if (s.listLoading) return;
    s.listLoading = true;
    const limit = more ? 20 : Math.min(64,Math.max(20,s.contests.length));
    const results = await Promise.allSettled([
      conn.client.get(`${path(sid)}?cursor=${more ? s.cursor || 0 : 0}&limit=${limit}`,conn.controller.signal),
      conn.client.get(`${path(sid)}/capabilities`,conn.controller.signal),
    ]);
    if (!valid(conn,sid,s)) return;
    s.listLoading = false;
    const [list,caps] = results;
    if (list.status === "fulfilled" && Array.isArray(list.value.contests)) {
      const data = list.value;
      s.contests = more ? [...new Map([...s.contests,...data.contests].map(c => [c.contest_id,c])).values()] : data.contests;
      s.cursor = data.next_cursor; s.total = data.total; s.warning = data.warning; s.persisted = data.persisted; s.listError = "";
      for (const c of s.contests) {
        if (!s.choices.has(c.contest_id)) s.choices.set(c.contest_id,read(choiceKey(sid,c.contest_id),{candidate_id:c.selection?.candidate_id || "",reason:""}));
      }
    } else s.listError = list.reason?.message || "比选记录格式不正确";
    if (caps.status === "fulfilled") {
      const fingerprint = JSON.stringify([caps.value.specs,caps.value.default_model,caps.value.default_judge_model]);
      if (s.fingerprint && s.fingerprint !== fingerprint) { s.preview = null; s.error = "可用模型或织手设置发生变化，请重新预览。"; }
      s.fingerprint = fingerprint; s.caps = caps.value;
    } else { s.caps = null; s.error = caps.reason?.message || "无法读取比选能力"; }
    paint(sid);
  }
  async function preview() {
    const {conn,sid,active} = current(); if (!active || !sid || blocked(sid)) return;
    const s = state(sid); if (s.previewLoading || !s.caps?.can_start) return;
    const snapshot = JSON.stringify(s.draft), fingerprint = s.fingerprint, body = contestRequest(s.draft,s.caps);
    if (!body.spec || !body.task || body.models.some(m => !m) || !body.judge_model) { s.error = "请选择织手、填写任务，并为每个席位和判官确定模型。"; paint(sid); return; }
    s.previewLoading = true; s.error = ""; s.preview = null; paint(sid);
    try {
      const result = await conn.client.post(`${path(sid)}/preview`,body,{signal:conn.controller.signal});
      if (!valid(conn,sid,s) || snapshot !== JSON.stringify(s.draft) || fingerprint !== s.fingerprint) return;
      if (!result.preview_token) throw new Error("服务没有返回有效预览凭据。");
      s.preview = result; s.previewBody = body; s.previewDraft = snapshot;
    } catch (error) { if (valid(conn,sid,s)) s.error = error.message; }
    finally { if (valid(conn,sid,s)) { s.previewLoading = false; paint(sid); } }
  }
  function choice(s,cid) {
    if (!s.choices.has(cid)) s.choices.set(cid,{candidate_id:"",reason:""});
    return s.choices.get(cid);
  }
  function input(event) {
    const {sid,active} = current(); if (!sid || !active) return false;
    const node = event.target, s = state(sid);
    if (node.closest("#contest-form")) {
      if (node.dataset.contestSeat) { const seat = s.draft.seats.find(v => v.id === node.dataset.contestSeat); if (seat) seat.model = node.value; }
      else if (["spec","task","criteria","judge_model"].includes(node.name)) s.draft[node.name] = node.value;
      changeDraft(sid,s); paint(sid); return true;
    }
    const cid = node.dataset.contestPick ? node.dataset.contestId : node.dataset.contestReason;
    if (cid) { const c = choice(s,cid); if (node.dataset.contestPick) c.candidate_id = node.dataset.contestPick; else c.reason = node.value; c.error = ""; persist(choiceKey(sid,cid),c); paint(sid); return true; }
    if (node.dataset.contestExtra) { const f = s.followups.get(node.dataset.contestExtra); if (f) { f.supplement = node.value; f.text = ""; f.conflict = false; persist(followupKey(sid,node.dataset.contestExtra),{supplement:f.supplement}); paint(sid); } return true; }
    return false;
  }
  function onSubmit(event) {
    if (event.target.id === "contest-form") { event.preventDefault(); preview(); return true; }
    const cid = event.target.dataset.contestSelection; if (!cid) return false;
    event.preventDefault(); const {sid,writable,pending} = current(), s = state(sid), c = s.contests.find(c => c.contest_id === cid), pick = choice(s,cid);
    if (!writable || s.listError || s.persisted === false || c?.interrupted_by_restart || current().session?.busy || pending.some(a => a.kind === "selection" && a.contestId === cid) || !c?.final) return true;
    if (!c.candidates.some(v => v.id === pick.candidate_id && v.status === "completed") || !pick.reason.trim()) { pick.error = "请选择一个已完成候选，并说明采纳理由。"; paint(sid); return true; }
    ui.submit(`${sid}:selection:${cid}`,{kind:"selection",sid,path:`${path(sid)}/${encodeURIComponent(cid)}/selection`,body:{candidate_id:pick.candidate_id,reason:pick.reason.trim(),expected_revision:c.selection_revision},contestId:cid,draftKey:choiceKey(sid,cid),draftText:JSON.stringify(pick)});
    return true;
  }
  function click(event) {
    const node = event.target.closest("[data-contest-action]"); if (!node) return false;
    const {conn,sid,active} = current(); if (!active || !sid) return true;
    const s = state(sid), action = node.dataset.contestAction, cid = node.dataset.contestId;
    if (action === "close") s.open = false;
    if (action === "reload" || action === "more") { load(conn,sid,action === "more"); return true; }
    if (action === "add-seat" && s.draft.seats.length < Math.min(6,s.caps?.max_candidates || 6)) { s.draft.seats.push({id:crypto.randomUUID(),model:""}); changeDraft(sid,s); }
    if (action === "remove-seat" && s.draft.seats.length > 2) { s.draft.seats = s.draft.seats.filter(v => v.id !== node.dataset.seatId); changeDraft(sid,s); }
    if (action === "submit" && !blocked(sid) && s.caps?.can_start && s.preview && s.previewDraft === JSON.stringify(s.draft)) ui.submit(`${sid}:contest:`,{kind:"contest",sid,path:path(sid),body:{...s.previewBody,preview_token:s.preview.preview_token},draftKey:s.draftKey,draftText:s.previewDraft});
    if (cid) {
      const contest = s.contests.find(c => c.contest_id === cid);
      if (s.listError || s.persisted === false || contest?.interrupted_by_restart) { const f = s.followups.get(cid); if (f) f.error = "记录尚未确认可用，请刷新并核对后再起草。"; paint(sid); return true; }
      if (action === "draft") s.followups.set(cid,{...read(followupKey(sid,cid),{supplement:""}),open:true});
      const f = s.followups.get(cid);
      if (f && action === "prepare-draft") {
        try { f.text = buildContestFollowup(contest,f.supplement); f.selection = JSON.stringify(contest.selection); f.error = ""; f.conflict = false; }
        catch (error) { f.error = error.message; }
      }
      if (f?.text && ["place-draft","append-draft","replace-draft"].includes(action)) {
        if (f.selection !== JSON.stringify(contest?.selection)) { f.text = ""; f.conflict = false; f.error = "当前采纳记录已变化，请重新预览草稿。"; }
        else {
          const merged = mergeContestDraft(ui.getComposer(),f.text,action === "append-draft" ? "append" : action === "replace-draft" ? "replace" : undefined);
          if (merged === null) f.conflict = true;
          else { ui.putComposer(merged); f.conflict = false; f.error = ""; f.open = false; }
        }
      }
    }
    paint(sid); return true;
  }
  function accepted(conn,action) {
    if (!["contest","selection"].includes(action.kind) || action.state !== "accepted") return;
    if (action.draftKey && conn.localState?.getDraft(action.draftKey) === action.draftText) conn.localState.deleteDraft(action.draftKey);
    const s = states.get(action.sid); if (!s) return;
    if (action.kind === "contest" && JSON.stringify(s.draft) === action.draftText) { s.draft = freshDraft(); s.open = false; s.preview = null; }
    if (action.kind === "selection" && JSON.stringify(s.choices.get(action.contestId)) === action.draftText) s.choices.delete(action.contestId);
  }
  return {load,accepted,input,click,onSubmit,reset:() => states.clear(),
    open() { const {sid,conn,active} = current(); if (!sid || !active) return; state(sid).open = true; paint(sid); load(conn,sid); },
    hasRecords(sid){const s=states.get(sid);return !!(s?.contests?.length || s?.listError);},
    form(sid) { const s = state(sid); return renderContestForm(s,s.caps,blocked(sid)); },
    results(sid) { const s = state(sid); return renderContests(s,{writable:current().writable && !current().session?.busy,pending:cid => current().pending.some(a => a.kind === "selection" && a.contestId === cid),artifactCard:ui.artifactCard}); },
    rejected(action,error) { const s = states.get(action.sid); if (!s || error.status !== 409 || error.recorded) return;
      if (action.kind === "contest") { s.preview = null; s.error = "预览已过期或状态已变化。核对并清除此回执后，请重新预览。"; }
      if (action.kind === "selection") choice(s,action.contestId).error = "采纳记录已变化，请刷新核对并清除此回执，再记录新的选择。";
    },
  };
}
