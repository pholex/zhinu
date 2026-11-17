import {workflowStatusText, escapeHTML as h,renderText} from "./client.mjs";

const array = value => Array.isArray(value) ? value : [];
const missionLabels = {pending:"待启动",active:"进行中",completed:"已完成，尚未收账",merged:"已合并"};
const memberLabels = {running:"运行中",done:"运行已结束",failed:"运行失败",retired:"已退役"};
const roleLabels = {worker:"执行成员",reviewer:"评审成员"};
const findingLabels = {bug:"缺陷",improve:"改进",vuln:"安全问题",idea:"想法"};
const actionLabels = {
  init:"启动协作",adopt:"收养既有使命",plan:"登记使命",spawn:"启动成员",teardown:"清理协作",
  "member.spawn":"启动成员","member.done":"成员完整结束","member.failed":"成员执行失败",
  "member.cut_short":"成员未完整结束","member.reaped":"回收成员后台任务",
  "archive.failed":"成员存档失败","inbox.send":"发送成员消息",send:"发送成员消息",
  "mission.scope":"调整使命范围","mission.update":"更新使命",
  "finding.file":"归档发现","review.write":"记录评审","merge.blocked":"合并被阻止",
  "merge.noop":"调研收账",merge:"合并使命","worktree.keep":"保留工作树",
};
const stamp = value => {
  if (!value) return "时间未记录";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN");
};
const text = value => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value);
const tag = value => `<span class="badge">${h(value)}</span>`;
const retiredButAlive = (member,fleet) => fleet.live === true && !fleet.recovered && member.thread_alive === true && (member.status === "retired" || member.execution_state === "retired");
const retiredUnknownAfterRestart = (member,fleet) => fleet.recovered === true && member.recorded_thread_alive === true && (member.status === "retired" || member.execution_state === "retired");

export const fleetDraftKey = (sid,board,name) => `fleet-message:${sid}:${board}:${name}`;
export const fleetMemberKey = (fleet,member) => JSON.stringify([fleet.board_id,member.name,member.run_id]);
export function parseFleetDrafts(raw) {
  if (!raw) return {};
  let value;
  try { value = JSON.parse(raw); } catch { throw new Error("已保存的成员草稿无法读取，未覆盖原数据。"); }
  if (!value || Array.isArray(value) || typeof value !== "object" || Object.values(value).some(text => typeof text !== "string")) throw new Error("已保存的成员草稿无法读取，未覆盖原数据。");
  return value;
}

export function fleetActionTarget(action) {
  const match = action.path?.match(/\/fleets\/([^/]+)\/members\/([^/]+)\/(?:messages|stop)$/);
  if (!match || typeof action.body?.run_id !== "string") return null;
  try { return [decodeURIComponent(match[1]),decodeURIComponent(match[2]),action.body.run_id]; } catch { return null; }
}
export function fleetControlState(fleet,member,{writable = false,protocol = false,stale = false,persisted = false,pending = []} = {}) {
  const target = fleetMemberKey(fleet,member);
  const queued = kind => pending.some(action => action.kind === kind && JSON.stringify(fleetActionTarget(action)) === target);
  const known = ["starting","running"].includes(member.execution_state) || retiredButAlive(member,fleet);
  const live = writable && protocol && !stale && fleet.live === true && !fleet.recovered && !!member.run_id && known;
  return {canMessage:!!(live && persisted && member.controls?.can_message && !queued("fleet_message")),
    canStop:!!(live && member.controls?.can_stop && !queued("fleet_stop")),
    reason:stale ? "快照尚未核对，暂不能操作成员。" : fleet.recovered || fleet.live !== true ? "历史记录不能控制成员。" : !persisted ? "服务记录尚未确认持久化，不能发送消息；若服务仍确认本代可停止，可明确请求停止，但回执不保证重启后可核对。" : !protocol ? "当前服务仅支持只读看板。" : !writable ? "连接或持久化状态未就绪。" : !known ? "这一代成员已结束或状态未知，不能继续发送。" : member.controls?.reason || "",
    messagePending:queued("fleet_message"),stopPending:queued("fleet_stop")};
}
export function renderFleetOperations(member) {
  const labels = {reserved:"已登记，尚未确认排队",queued:"已排队，尚未进入成员上下文",accepted:"已进入成员上下文",not_consumed:"本代已结束，消息未被消费",stop_requested:"已请求停止，等待成员结束",stopped:"目标成员本代已结束",failed:"请求失败",unknown:"执行状态未知，请核对"};
  const operations = array(member.operations).filter(op => op.run_id === member.run_id);
  if (!operations.length) return "";
  return `<details class="fleet-operation-history" data-key="fleet-operations-${h(member.run_id)}" open><summary>本代消息与停止记录 · ${operations.length}</summary><ol>${operations.map(op => `<li><div class="section-heading"><strong>${h(op.kind === "stop" ? "停止成员" : "成员消息")}</strong>${tag(labels[op.status] || "状态尚未识别")}</div>${op.text ? `<p class="fleet-message-text">${h(op.text)}</p>` : ""}${op.status === "accepted" ? `<p class="note">消息已加入成员上下文，不代表已回答或已完成要求。</p>` : ""}${op.error || op.reason ? `<p class="note">${h(op.error || op.reason)}</p>` : ""}<p class="note">${h(stamp(op.updated_at || op.created_at))} · ${h(op.operation_id)}</p></li>`).join("")}</ol></details>`;
}
export function renderFleetMemberControls(fleet,member,options = {}) {
  const {protocol = false,drafts = {},error = "",draftError = "",receipts = [],receiptWarning = ""} = options;
  let html = renderFleetOperations(member);
  const oldReceipts = receipts.filter(view => view.ref.run_id !== member.run_id);
  if (oldReceipts.length || receiptWarning) html += `<details class="fleet-operation-history" data-key="fleet-old-operations-${h(fleet.board_id)}-${h(member.name)}"><summary>旧代提交记录 · ${oldReceipts.length}</summary><p class="note">仅核对保存的操作编号，不会重新发送。${h(receiptWarning)}</p>${oldReceipts.map(view => `<section><p class="note">运行代次 ${h(view.ref.run_id)}</p>${view.error ? `<p class="form-error">回执状态读取失败：${h(view.error)}。${view.data ? "以下为上次记录，可能已过期。" : "目前无法确认操作结果。"}</p>` : ""}${view.data ? renderFleetOperations({run_id:view.ref.run_id,operations:[view.data]}) : `<p class="note">${view.error ? "" : "正在核对回执…"} 操作编号 ${h(view.ref.operation_id)}</p>`}</section>`).join("")}</details>`;
  if (!protocol) return html;
  const key = fleetMemberKey(fleet,member), control = fleetControlState(fleet,member,options);
  const old = Object.entries(drafts).filter(([run,content]) => run !== member.run_id && content);
  html += `<section class="fleet-controls" aria-label="与 ${h(member.name)} 交互"><p class="note">目标成员 ${h(member.name)} · 运行代次 <code>${h(member.run_id || "未记录")}</code></p>${control.reason ? `<p class="note">${h(control.reason)}</p>` : ""}${error || draftError ? `<p class="form-error" role="alert">${h(error || draftError)}</p>` : ""}<form data-fleet-message="${h(key)}"><label for="fleet-draft-${h(encodeURIComponent(key))}">给此成员补充说明<textarea id="fleet-draft-${h(encodeURIComponent(key))}" data-fleet-draft="${h(key)}" rows="3" maxlength="8000" ${draftError ? "readonly" : ""}>${h(drafts[member.run_id] || "")}</textarea></label><div class="actions"><button type="submit" ${!control.canMessage || draftError || !(drafts[member.run_id] || "").trim() ? "disabled" : ""}>${control.messagePending ? "等待提交核对" : "发送给此成员"}</button><button type="button" data-fleet-action="stop" data-fleet-member="${h(key)}" ${!control.canStop ? "disabled" : ""}>${control.stopPending || member.stop_requested ? "停止请求待核对" : "请求停止此成员"}</button></div></form><p class="note">发送后先排队；停止请求不等于已经停止。停止保留工作树和已有成果，不执行清理。</p>`;
  if (old.length) html += `<details class="fleet-old-drafts" data-key="old-member-drafts-${h(fleet.board_id)}-${h(member.name)}"><summary>保留的旧代草稿 · ${old.length}</summary><p class="note">这些草稿属于已更换的运行代次，不会自动交给当前成员。需要复用时请检查并手动复制。</p>${old.map(([run,content]) => `<label>运行 ${h(run)}<textarea rows="3" readonly>${h(content)}</textarea></label>`).join("")}</details>`;
  return `${html}</section>`;
}
export function renderFleetApprovalIdentity(identity) {
  if (!identity || identity.kind === "qixiang") return "";
  return `<div class="fleet-approval-owner"><strong>统筹协作成员工具调用 · ${h(identity.member_name || "身份未记录")}</strong><dl class="fleet-facts"><div><dt>使命</dt><dd>${h(identity.mission_id || "未关联使命")}</dd></div><div><dt>成员工作区</dt><dd><code>${h(identity.workspace || "未记录")}</code></dd></div><div><dt>成员运行</dt><dd><code>${h(identity.member_run_id || "未记录")}</code></dd></div><div><dt>协作实例</dt><dd><code>${h(identity.board_id || "未记录")}</code></dd></div></dl><p class="note">本次决定仅处理此成员的这一次工具调用。</p></div>`;
}

export function missionState(mission) {
  if (mission.status === "merged" && mission.kind === "survey") return "已收账（无代码合并）";
  return missionLabels[mission.status] || mission.status || "状态未记录";
}
export function memberState(member,fleet) {
  const state = member.execution_state, outcome = member.execution_outcome;
  if ((fleet.recovered || fleet.live === false) && ["starting","running"].includes(state) || member.status === "running" && state === "unknown") return "运行状态未知";
  if (state === "unknown") return "运行状态未知";
  if (retiredButAlive(member,fleet)) return "花名册已退役，线程仍活跃";
  if (retiredUnknownAfterRestart(member,fleet)) return "历史花名册已退役，运行状态未知";
  if (state === "retired" || member.status === "retired") return "已退役";
  if (state === "starting") return "正在启动";
  if (state === "running") return "执行中";
  if (outcome === "partial") return "已结束，未完整完成";
  if (outcome === "failed" || member.status === "failed") return "执行失败";
  if (outcome === "completed") return "执行已完成";
  return memberLabels[member.status] || "结果尚未确认";
}
function renderMission(mission,missions,board) {
  const clipped = [["title_truncated","标题"],["scope_truncated","负责范围"],["deps_truncated","依赖"],["notes_truncated","备注"]].filter(([key]) => mission[key]).map(([,label]) => label);
  const dependencies = array(mission.deps).map(id => {
    const dep = missions.find(item => item.id === id);
    return `${id}${dep ? ` · ${dep.title} · ${missionState(dep)}` : " · 本快照没有此使命"}`;
  });
  return `<details class="fleet-mission" data-key="fleet-${h(board)}-mission-${h(mission.id)}" open><summary><span><strong>${h(mission.id)}</strong> ${h(mission.title)}</span>${tag(missionState(mission))}</summary><div class="fleet-detail">${clipped.length ? `<p class="fleet-warning">${h(clipped.join("、"))}已截断，当前内容不完整。</p>` : ""}<dl class="fleet-facts"><div><dt>类型</dt><dd>${h(mission.kind === "survey" ? "只读调研" : mission.kind === "build" ? "代码实现" : mission.kind)}</dd></div><div><dt>负责人</dt><dd>${h(mission.owner || "尚未指派")}</dd></div><div><dt>负责范围</dt><dd>${array(mission.scope).length ? array(mission.scope).map(scope => `<code>${h(scope)}</code>`).join(" ") : h(mission.kind === "survey" ? "调研任务，不分配写入范围" : "范围未记录")}</dd></div><div><dt>依赖</dt><dd>${dependencies.length ? dependencies.map(dep => `<p>${h(dep)}</p>`).join("") : "无已登记依赖"}</dd></div>${mission.branch ? `<div><dt>分支</dt><dd><code>${h(mission.branch)}</code></dd></div>` : ""}${mission.worktree ? `<div><dt>工作树</dt><dd><code>${h(mission.worktree)}</code></dd></div>` : ""}</dl>${array(mission.notes).length ? `<ul class="fleet-notes">${mission.notes.map(note => `<li>${h(note)}</li>`).join("")}</ul>` : ""}</div></details>`;
}
function renderMember(member,fleet,stale,controls = "") {
  const target = member.mission_id || member.review_target;
  const mission = array(fleet.missions).find(m => m.id === target || m.branch === target);
  return `<article class="fleet-member"><div class="section-heading"><h4>${h(member.name)}</h4>${tag(`${stale ? "上次记录：" : ""}${memberState(member,fleet)}`)}</div><p class="note">${h(roleLabels[member.kind] || member.kind)}${target ? ` → ${h(target)}${mission ? ` · ${h(mission.title)}` : ""}` : " · 未关联使命"}</p><dl class="fleet-facts"><div><dt>配置模型</dt><dd>${h(member.model || "未记录")}${member.model_truncated ? "（模型名称已截断）" : ""}${member.effort ? ` · ${h(member.effort)}` : ""}</dd></div><div><dt>花名册记录</dt><dd>${h(memberLabels[member.status] || member.status || "未记录")}</dd></div>${member.recorded_execution_outcome ? `<div><dt>重启前记录</dt><dd>${h({running:"当时执行中",completed:"当时已完成",partial:"当时未完整完成",failed:"当时已失败",unknown:"结果未知"}[member.recorded_execution_outcome] || member.recorded_execution_outcome)}</dd></div>` : ""}${member.stop_reason ? `<div><dt>结束原因</dt><dd>${h(member.stop_reason)}</dd></div>` : ""}${member.run_id ? `<div><dt>成员运行</dt><dd><code>${h(member.run_id)}</code></dd></div>` : ""}</dl>${member.error ? `<p class="form-error">${h(workflowStatusText(member.error))}</p>` : ""}${member.handoff ? `<details data-key="fleet-${h(fleet.board_id)}-handoff-${h(member.name)}"><summary>查看交接正文</summary><div class="message assistant">${renderText(member.handoff)}</div>${member.handoff_truncated ? `<p class="note">交接正文已截断，当前内容不是完整记录。</p>` : ""}</details>` : `<p class="note">尚无已记录的交接正文。</p>`}${controls}</article>`;
}
function renderReviews(fleet) {
  const reviews = array(fleet.reviews);
  return `<details class="fleet-ledger" data-key="fleet-${h(fleet.board_id)}-reviews"><summary>评审记录 · ${reviews.length}</summary><p class="note">评审反映记录时的结论；未重新校验当前提交，不代表现在可以合并。</p>${reviews.length ? reviews.map(review => `<article><div class="section-heading"><strong>${h(review.mission_id)} · 第 ${h(review.round)} 轮</strong>${tag(review.verdict === "clean" ? "评审记录：通过" : review.verdict)}</div><p class="note">${h(review.from)} · ${h(stamp(review.recorded_at))}</p><p class="note">被评审提交 <code>${h(review.reviewed_commit || "未记录")}</code> · 当前提交未复核</p><div class="message assistant">${renderText(review.summary || "未记录评审正文")}</div>${review.summary_truncated ? `<p class="fleet-warning">评审正文已截断。</p>` : ""}</article>`).join("") : `<p class="note">尚无已记录的评审。</p>`}</details>`;
}
function renderFindings(fleet) {
  const findings = array(fleet.findings);
  return `<details class="fleet-ledger" data-key="fleet-${h(fleet.board_id)}-findings"><summary>发现 · ${findings.length}</summary>${findings.length ? findings.map(finding => `<article><div class="section-heading"><strong>${h(finding.title)}</strong>${tag(findingLabels[finding.kind] || finding.kind)}</div><p class="note">${h(finding.from)}${finding.mission_id ? ` · ${h(finding.mission_id)}` : ""} · ${h(stamp(finding.recorded_at))}</p><div class="message assistant">${renderText(finding.body || "未记录正文")}</div>${finding.title_truncated || finding.body_truncated ? `<p class="fleet-warning">发现的${finding.title_truncated ? "标题" : ""}${finding.title_truncated && finding.body_truncated ? "与" : ""}${finding.body_truncated ? "正文" : ""}已截断。</p>` : ""}</article>`).join("") : `<p class="note">尚无已归档的发现。</p>`}</details>`;
}
function renderActivity(fleet) {
  const activity = array(fleet.activity);
  return `<details class="fleet-ledger" data-key="fleet-${h(fleet.board_id)}-activity"><summary>最近活动 · ${activity.length}</summary>${activity.length ? `<ol class="fleet-activity">${activity.map(entry => `<li><div class="section-heading"><strong>${h(actionLabels[entry.action] || entry.action)}</strong><span class="note">${h(stamp(entry.at))}</span></div><p class="note">${h(entry.actor)}</p>${["send","inbox.send"].includes(entry.action) ? `<p class="note">消息发送记录不表示成员已读取或模型已接纳。</p>` : ""}${entry.details && Object.keys(entry.details).length ? `<dl class="fleet-facts">${Object.entries(entry.details).map(([key,value]) => `<div><dt>${h(key)}</dt><dd>${h(text(value))}</dd></div>`).join("")}</dl>` : ""}${entry.details_truncated ? `<p class="fleet-warning">活动详情已截断。</p>` : ""}</li>`).join("")}</ol>` : `<p class="note">当前快照没有活动记录。</p>`}</details>`;
}

export function renderFleets(state,{parentBusy = false,interactive = false,hideEmpty = false,memberControls = () => ""} = {}) {
  if (!state) return "";
  const fleets = array(state.fleets);
  if(hideEmpty && !fleets.length && !state.error && !state.loading && !state.warning && state.persisted!==false && state.available!==false)return "";
  let html = `<section class="fleet-section" aria-label="统筹协作看板"><div class="section-heading"><h3>统筹协作 · 协作看板</h3><span class="note">${interactive ? "成员交互" : "只读"}</span></div>`;
  if (state.error) html += `<div class="callout warning" role="status"><p>协作记录读取失败：${h(workflowStatusText(state.error))}${fleets.length ? "。保留上次快照，成员当前状态尚未核对。" : "。尚未取得协作记录，不能判断是否有成员执行。"}</p><button type="button" data-fleet-action="reload">重新读取</button></div>`;
  if (state.loading && !fleets.length) return `${html}<p class="loading-line">正在读取协作记录…</p></section>`;
  if (fleets.length && state.reason) html += `<p class="note">${h(workflowStatusText(state.reason))}</p>`;
  if (state.warning) html += `<p class="note">${h(workflowStatusText(state.warning))}</p>`;
  if (state.persisted === false) html += `<p class="note">这份协作快照尚未确认持久化，重启后不保证可读取。</p>`;
  if (!fleets.length && !state.error) html += `<p class="note">${h(workflowStatusText(state.available === false ? state.reason || "当前会话没有启用统筹协作能力。" : "当前会话尚无已记录的统筹协作活动。"))}</p>`;
  for (const fleet of fleets) {
    const historical = fleet.recovered || fleet.live === false;
    const stale = historical || !!state.error;
    const missions = array(fleet.missions), members = array(fleet.members);
    html += `<article class="fleet-board"><div class="section-heading"><h4>${h(fleet.source?.workspace || "工作目录未记录")}</h4>${tag(state.error ? "旧快照，待核对" : historical ? "历史快照" : "本服务观测")}</div><p class="note">基线分支 ${h(fleet.base_branch || "未记录")} · 观测于 ${h(stamp(fleet.observed_at))}</p>`;
    if (fleet.recovered) html += `<p class="fleet-warning">服务重启后只恢复了记录；未收养或恢复成员。原运行中成员的当前状态未知。</p>`;
    else if (historical) html += `<p class="fleet-warning">这是历史观测，无法据此确认成员当前仍在执行或已经停止。</p>`;
    html += `<p class="fleet-lifecycle">${state.error ? parentBusy ? "父会话上次记录为执行中。" : "父会话上次记录为轮次空闲。" : parentBusy ? "父会话当前轮次执行中。" : "父会话当前轮次空闲。"}停止父会话本轮不等于停止统筹协作成员，成员状态见下方。</p><p class="note">${stale ? "上次记录" : "协议记录"}：${fleet.active === true ? "协作已启用" : fleet.active === false ? "协作未启用" : "启用状态未知"}。此标记不代替成员运行状态。</p><div class="fleet-overview"><span>${missions.length} 项使命</span><span>${members.length} 位成员</span><span>${missions.filter(m => m.status === "merged").length} 项已收账</span></div><section class="fleet-missions"><h4>使命与依赖</h4><p class="note">使命“已完成”不等于已合并，也不保证成员执行完整；调研收账不涉及代码合并。</p>${missions.length ? missions.map(m => renderMission(m,missions,fleet.board_id)).join("") : `<p class="note">尚无已登记使命。</p>`}</section><section class="fleet-members"><h4>成员与交接</h4>${members.length ? `<div class="fleet-member-grid">${members.map(m => renderMember(m,fleet,stale,memberControls(fleet,m))).join("")}</div>` : `<p class="note">花名册中尚无成员。</p>`}</section>${renderFindings(fleet)}${renderReviews(fleet)}${renderActivity(fleet)}`;
    const limited = Object.entries(fleet.limits || {}).filter(([,count]) => Number(count) > 0), names = {missions:"使命",members:"成员",reviews:"评审",findings:"发现",activity:"活动"};
    if (limited.length) html += `<p class="fleet-warning">快照有截断：${limited.map(([name,count]) => `${h(names[name] || name)}省略 ${h(count)} 项`).join("；")}。当前看板不是完整记录。</p>`;
    if (array(fleet.gaps).length) html += `<ul class="fleet-gaps">${fleet.gaps.map(gap => `<li>${h(gap)}</li>`).join("")}</ul>`;
    html += `<details class="technical" data-key="fleet-${h(fleet.board_id)}-source"><summary>快照来源</summary><dl class="fleet-facts"><div><dt>协作实例</dt><dd><code>${h(fleet.board_id)}</code> · 版本 ${h(fleet.revision)}</dd></div><div><dt>来源轮次</dt><dd><code>${h(fleet.source_run_id || "未记录")}</code></dd></div><div><dt>记录身份</dt><dd><code>${h(fleet.source?.state_id || "未记录")}</code></dd></div><div><dt>建立方式</dt><dd>${h(fleet.source?.origin === "adopted" ? "收养既有使命" : fleet.source?.origin === "new" ? "新建" : fleet.source?.origin || "未记录")}</dd></div></dl></details></article>`;
  }
  if (state.cursor !== null && state.cursor !== undefined) html += `<div class="history-pagination"><span>已显示 ${fleets.length} / ${h(state.total)} 个协作实例</span><button type="button" data-fleet-action="more" ${state.loading ? "disabled" : ""}>加载更早实例</button></div>`;
  return `${html}</section>`;
}

export function createFleetController(ui) {
  const states = new Map(), groups = new Map(), errors = new Map(), receiptGroups = new Map(), receiptViews = new Map();
  const context = () => ui.context();
  const paint = sid => { if (context().sid === sid && context().active) ui.render(); };
  const state = sid => {
    if (!states.has(sid)) states.set(sid,{fleets:[],cursor:null,total:0,loading:false,loaded:false});
    return states.get(sid);
  };
  async function load(conn,sid,more = false) {
    if (!conn || context().conn !== conn || conn.health?.fleet_protocol !== 1) return;
    const s = state(sid); if (s.loading) return;
    s.loading = true; paint(sid);
    try {
      const limit = more ? 20 : Math.max(20,Math.min(64,s.fleets.length));
      const page = await conn.client.get(`/session/${encodeURIComponent(sid)}/fleets?cursor=${more ? s.cursor || 0 : 0}&limit=${limit}`,conn.controller.signal);
      if (context().conn !== conn || states.get(sid) !== s) return;
      if (!Array.isArray(page.fleets) || page.session_id !== sid) throw new Error("服务返回的协作快照不属于当前会话或格式不正确。");
      s.fleets = more ? [...new Map([...s.fleets,...page.fleets].map(f => [f.board_id,f])).values()] : page.fleets;
      s.cursor = page.next_cursor; s.total = page.total; s.persisted = page.persisted; s.warning = page.warning;
      s.available = page.available; s.reason = page.reason; s.error = ""; s.loaded = true;
      await loadReceipts(conn,sid,s);
    } catch (error) {
      if (context().conn === conn && states.get(sid) === s && !conn.controller.signal.aborted) s.error = error.message;
    } finally { if (context().conn === conn && states.get(sid) === s) { s.loading = false; paint(sid); } }
  }
  function group(sid,fleet,member) {
    const key = fleetDraftKey(sid,fleet.board_id,member.name);
    if (!groups.has(key)) {
      try { groups.set(key,{key,drafts:parseFleetDrafts(context().conn?.localState?.getDraft(key)),error:""}); }
      catch (error) { groups.set(key,{key,drafts:{},error:error.message}); }
    }
    return groups.get(key);
  }
  function receipts(sid,board,name) {
    const key = `fleet-receipts:${sid}:${board}:${name}`;
    if (!receiptGroups.has(key)) {
      try {
        const raw = context().conn?.localState?.getDraft(key), value = raw ? JSON.parse(raw) : {refs:[],truncated:false};
        if (!Array.isArray(value.refs) || value.refs.length > 64 || value.refs.some(ref => ![ref.operation_id,ref.run_id,ref.kind].every(v => typeof v === "string" && v.length <= 200))) throw new Error("本机操作编号索引格式不可读；原数据未覆盖。");
        receiptGroups.set(key,{...value,key,error:""});
      } catch (error) { receiptGroups.set(key,{key,refs:[],error:error.message}); }
    }
    return receiptGroups.get(key);
  }
  async function loadReceipts(conn,sid,s) {
    if (conn.health?.fleet_control_protocol !== 1) return;
    const jobs = [];
    for (const fleet of s.fleets) for (const member of array(fleet.members)) {
      const group = receipts(sid,fleet.board_id,member.name);
      for (const ref of group.refs) {
        if (ref.run_id === member.run_id) continue;
        const key = `${sid}:${ref.operation_id}`, prior = receiptViews.get(key);
        if (prior?.loading || prior?.data && !prior.error && ["accepted","not_consumed","stopped","failed"].includes(prior.data.status)) continue;
        const view = prior || {ref}; view.loading = true; receiptViews.set(key,view);
        jobs.push(async () => {
          try {
            const {operation} = await conn.client.get(`/session/${encodeURIComponent(sid)}/fleet-operations/${encodeURIComponent(ref.operation_id)}`,conn.controller.signal);
            if (context().conn !== conn || states.get(sid) !== s || receiptViews.get(key) !== view) return;
            if (!operation || operation.operation_id !== ref.operation_id || operation.run_id !== ref.run_id || operation.board_id !== fleet.board_id || operation.member_name !== member.name || operation.kind !== ref.kind) throw new Error("回执来源与保存的目标不一致。");
            view.data = operation; view.error = "";
          } catch (error) { if (context().conn === conn && receiptViews.get(key) === view) view.error = error.message; }
          finally { view.loading = false; }
        });
      }
    }
    for (let offset = 0; offset < jobs.length; offset += 8) {
      if (context().conn !== conn || states.get(sid) !== s) return;
      await Promise.allSettled(jobs.slice(offset,offset + 8).map(job => job()));
    }
  }
  function saveGroup(g,conn = context().conn) {
    if (Object.keys(g.drafts).length) conn?.localState?.setDraft(g.key,JSON.stringify(g.drafts));
    else conn?.localState?.deleteDraft(g.key);
  }
  function target(sid,key) {
    for (const fleet of array(states.get(sid)?.fleets)) for (const member of array(fleet.members)) if (fleetMemberKey(fleet,member) === key) return {fleet,member};
    return null;
  }
  function options(sid,fleet,member) {
    const c = context(), s = states.get(sid), g = group(sid,fleet,member), saved = receipts(sid,fleet.board_id,member.name);
    return {writable:!!c.writable,protocol:c.conn?.health?.fleet_control_protocol === 1,stale:!!s?.error || c.conn?.healthy === false,persisted:s?.persisted === true,pending:c.pending || [],drafts:g.drafts,draftError:g.error,error:errors.get(fleetMemberKey(fleet,member)) || "",
      receipts:saved.refs.map(ref => ({ref,...receiptViews.get(`${sid}:${ref.operation_id}`)})),receiptWarning:saved.error || (saved.truncated ? "本机仅保留最近 64 条操作编号，当前列表不是完整操作历史。" : "")};
  }
  function send(key,kind) {
    const c = context(); if (!c.active || !c.sid) return;
    const resolved = target(c.sid,key); if (!resolved) return;
    const {fleet,member} = resolved, control = fleetControlState(fleet,member,options(c.sid,fleet,member));
    if (kind === "fleet_message" ? !control.canMessage : !control.canStop) return;
    const g = group(c.sid,fleet,member), content = g.drafts[member.run_id] || "";
    if (kind === "fleet_message" && (g.error || !content.trim())) return;
    const base = `/session/${encodeURIComponent(c.sid)}/fleets/${encodeURIComponent(fleet.board_id)}/members/${encodeURIComponent(member.name)}`;
    errors.delete(key);
    ui.submit(`${c.sid}:${kind}:${key}`,{kind,sid:c.sid,path:`${base}/${kind === "fleet_message" ? "messages" : "stop"}`,body:{run_id:member.run_id,...(kind === "fleet_message" ? {text:content} : {})},...(kind === "fleet_message" ? {draftKey:g.key,draftText:content} : {})});
  }
  return {load,reset:() => { states.clear(); groups.clear(); errors.clear(); receiptGroups.clear(); receiptViews.clear(); },
    render(sid,{hideEmpty=false}={}) {
      const s = states.get(sid), c = context();
      return renderFleets(s && c.conn?.healthy === false ? {...s,error:s.error || "服务连接异常"} : s,{hideEmpty,parentBusy:!!c.session?.busy,interactive:c.conn?.health?.fleet_control_protocol === 1,
        memberControls:(fleet,member) => renderFleetMemberControls(fleet,member,options(sid,fleet,member))});
    },
    hasRecords(sid){const s=states.get(sid);return !!(s?.fleets?.length || s?.error);},
    hasUnsettled(sid) { return array(states.get(sid)?.fleets).some(f => array(f.members).some(m => ["running","starting","unknown"].includes(m.execution_state) || retiredButAlive(m,f) || retiredUnknownAfterRestart(m,f))); },
    input(event) {
      const key = event.target.dataset.fleetDraft; if (!key) return false;
      const c = context(); if (!c.active || !c.sid) return true;
      const resolved = target(c.sid,key); if (!resolved) return true;
      const {fleet,member} = resolved, g = group(c.sid,fleet,member); if (g.error) return true;
      if (event.target.value) Object.defineProperty(g.drafts,member.run_id,{value:event.target.value,enumerable:true,writable:true,configurable:true});
      else delete g.drafts[member.run_id];
      saveGroup(g); errors.delete(key); paint(c.sid); return true;
    },
    onSubmit(event) { const key = event.target.dataset.fleetMessage; if (!key) return false; event.preventDefault(); send(key,"fleet_message"); return true; },
    accepted(conn,action) {
      if (!["fleet_message","fleet_stop"].includes(action.kind) || action.state !== "accepted") return;
      const identity = fleetActionTarget(action), id = action.result?.operation_id;
      if (identity && typeof id === "string" && id.length <= 200) {
        const [board,name,run] = identity, saved = receipts(action.sid,board,name);
        if (saved.error) throw new Error(saved.error);
        if (!saved.refs.some(ref => ref.operation_id === id)) saved.refs.push({operation_id:id,run_id:run,kind:action.kind === "fleet_message" ? "message" : "stop"});
        if (saved.refs.length > 64) { saved.refs = saved.refs.slice(-64); saved.truncated = true; }
        conn.localState?.setDraft(saved.key,JSON.stringify({refs:saved.refs,truncated:!!saved.truncated}));
      }
      if (action.kind !== "fleet_message" || !action.draftKey || typeof action.draftText !== "string" || !["queued","accepted"].includes(action.result?.operation_status)) return;
      const run = action.body?.run_id;
      if (conn.localState) {
        try { const saved = parseFleetDrafts(conn.localState.getDraft(action.draftKey)); if (saved[run] === action.draftText) { delete saved[run]; saveGroup({key:action.draftKey,drafts:saved},conn); } }
        catch { /* Keep unreadable local drafts; their error remains visible. */ }
      }
      const g = groups.get(action.draftKey); if (g?.drafts[run] === action.draftText) delete g.drafts[run];
    },
    rejected(action,error) {
      if (!["fleet_message","fleet_stop"].includes(action.kind)) return;
      const key = fleetActionTarget(action); if (!key) return;
      errors.set(JSON.stringify(key),error.status === 409 ? "目标成员的运行代次或状态已变化。原草稿仍保留，请刷新核对；不会自动转发给新一代成员。" : error.message);
    },
    click(event) {
      const node = event.target.closest("[data-fleet-action]"); if (!node) return false;
      const c = context(); if (node.dataset.fleetAction === "stop") { send(node.dataset.fleetMember,"fleet_stop"); return true; }
      if (c.sid && c.active) load(c.conn,c.sid,node.dataset.fleetAction === "more"); return true;
    },
  };
}
