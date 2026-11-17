import {workflowStatusText, escapeHTML as h, renderText} from "./client.mjs";

export const WORKFLOW_CHOICES = [
  {id:"auto", label:"对话", description:"直接描述需求，讨论问题或完成工作", symbol:"message"},
  {id:"weave", label:"批量执行", description:"将一组同类事项分别处理", symbol:"capabilities"},
  {id:"contest", label:"方案比选", description:"生成多个候选方案，比较后择优", symbol:"compare"},
  {id:"fleet", label:"统筹协作", description:"拆分复杂任务，由多个角色分工完成", symbol:"group"},
];

export function renderWeaveForm(state, capabilities, disabled = false) {
  if (!state?.open) return "";
  const draft = state.draft, resume = draft.mode === "resume", specs = capabilities?.specs || [];
  let html = `<section class="weave-compose" aria-label="批量执行任务编排"><div class="section-heading"><h3 id="weave-command-title" tabindex="-1">${resume ? "续跑选中任务" : "新建批量执行任务"}</h3><div class="actions">${resume ? `<button class="quiet" type="button" data-weave-reset>新建一组任务</button>` : ""}<button class="quiet" type="button" data-weave-close>收起</button></div></div><p class="note">${resume ? "将依据原子任务历史创建新批次；旧结果保留，不会自动重新执行其他项目。" : "列出要分别处理的事项，选择执行助手。预览每项任务后，再确认开始。"}</p>`;
  if (state.error) html += `<p class="form-error" role="alert">${h(workflowStatusText(state.error))}</p>`;
  if (!capabilities) return `${html}<p class="loading-line">正在读取可用执行助手…</p><button type="button" data-weave-capabilities>重新读取</button></section>`;
  if (capabilities.reason) html += `<p class="note">${h(workflowStatusText(capabilities.reason))}</p>`;
  html += `<form id="weave-form">`;
  if (resume) html += `<p class="note">来源批次：${h(draft.source_batch_id)} · 已选 ${draft.item_ids.length} 项</p><label>补充要求<textarea id="weave-instruction" name="instruction" rows="3" maxlength="12000" placeholder="说明这次希望继续或修正的内容">${h(draft.instruction || "")}</textarea></label>`;
  else {
    html += `<label>执行助手<select id="weave-spec" name="spec"><option value="">选择已配置的执行助手</option>${specs.map(spec => `<option value="${h(spec.name)}" ${draft.spec === spec.name ? "selected" : ""}>${h(spec.name)}${spec.readonly ? " · 只读" : ""}</option>`).join("")}</select></label>`;
    const spec = specs.find(s => s.name === draft.spec);
    if (spec?.description) html += `<p class="note">${h(spec.description)}</p>`;
    html += `<label>每项任务的共同要求<textarea id="weave-template" name="prompt_template" rows="3" maxlength="12000">${h(draft.prompt_template)}</textarea></label><p class="note">用 {{item}} 插入每行事项，其他指令会用于所有任务。</p><fieldset class="weave-items"><legend>任务清单</legend>${draft.items.map((item, index) => `<div class="weave-input-row"><label><span class="sr-only">任务 ${index + 1}</span><textarea id="weave-item-${h(item.id)}" data-weave-item="${h(item.id)}" rows="2" maxlength="12000" placeholder="第 ${index + 1} 项要做什么">${h(item.text)}</textarea></label><button class="quiet" type="button" data-weave-remove="${h(item.id)}" aria-label="删除第 ${index + 1} 项">移除</button></div>`).join("")}<button type="button" data-weave-add ${draft.items.length >= Math.min(64, capabilities.max_items || 64) ? "disabled" : ""}>添加一项</button></fieldset>`;
  }
  html += `<div class="actions"><button type="submit" ${disabled || state.loading || !capabilities.available ? "disabled" : ""}>${state.loading ? "正在预览…" : "预览任务"}</button></div></form>`;
  if (state.preview) html += `<section class="weave-preview" aria-label="任务提交预览"><h3>确认 ${h(state.preview.count)} 项任务 · ${h(state.preview.spec)}</h3><p class="note">预览不会执行工具，也不会代替执行时的审批。</p><ol>${(state.preview.items || []).map(item => `<li><strong>${h(item.label)}</strong><pre>${h(item.task)}</pre></li>`).join("")}</ol><button class="primary" type="button" data-weave-submit ${disabled || !(resume ? capabilities.can_resume : capabilities.can_start) ? "disabled" : ""}>确认提交${resume ? "续跑" : "新"}批次</button></section>`;
  return `${html}</section>`;
}

export function renderArtifactImage(image) {
  if (!image) return "";
  const label = image.version === "delivered" ? "图片 · 与交付时一致" : image.version === "current_changed" ? "图片 · 当前变更版本（非交付原图）" : "图片预览不可用";
  const prefix = image.mime_type === "image/png" ? "data:image/png;base64," : image.mime_type === "image/jpeg" ? "data:image/jpeg;base64," : "";
  const valid = image.available === true && ["delivered","current_changed"].includes(image.version) && prefix && typeof image.data_url === "string" && image.data_url.startsWith(prefix) && image.data_url.length <= 2796227 && /^[A-Za-z0-9+/]+={0,2}$/.test(image.data_url.slice(prefix.length)) && image.data_url.slice(prefix.length).length % 4 === 0 && Number.isInteger(image.width) && Number.isInteger(image.height) && image.width > 0 && image.height > 0 && image.width <= 8192 && image.height <= 8192 && image.width * image.height <= 16 * 1024 * 1024;
  return `<section class="artifact-image"><h4>${h(label)}</h4>${valid ? `<img src="${h(image.data_url)}" width="${image.width}" height="${image.height}" alt="${h(label)}" loading="lazy"><p class="note">${image.width} × ${image.height} · ${h(image.mime_type)} · 读取时校验 ${h(image.sha256 || "未提供")}</p>` : `<p class="note">${image.available ? "图片格式、大小或版本信息不符合预览要求，未加载。" : "当前没有可读取的图片预览。"}</p>`}${image.note ? `<p class="note">${h(image.note)}</p>` : ""}</section>`;
}

export function renderArtifactInspection(state, {canReveal = false} = {}) {
  if (!state) return "";
  if (state.loading && !state.data) return `<p class="loading-line" role="status">正在检查成果文件…</p>`;
  let html = state.error ? `<p class="form-error" role="alert">${h(workflowStatusText(state.error))}${state.data ? "；下方保留上次读取的快照。" : ""}</p>` : "";
  const data = state.data; if (!data) return html;
  html += `<div class="artifact-inspection"><div class="section-heading"><strong>${{delivered:"与交付时一致",current_changed:"交付后已变化",missing:"当前文件已不存在",unavailable:"当前文件不可读取"}[data.version] || "文件检查结果"}</strong>${canReveal && data.can_reveal ? `<button type="button" class="quiet" data-artifact-reveal="${h(state.key)}">在 Finder 中显示</button>` : ""}</div>`;
  if (data.warning) html += `<p class="note">${h(data.warning)}</p>`;
  const markdown = /\.(md|markdown)$/i.test(data.artifact?.path || "");
  const content = (part, title) => `<details class="artifact-version" data-key="artifact-${h(state.key)}-${h(title)}" open><summary>${title}</summary>${part?.available ? markdown ? `<div class="message assistant">${renderText(part.text || "")}</div>` : `<pre>${h(part.text || "")}</pre>` : `<p class="note">${h(part?.note || "没有已保存的正文可供预览。")}</p>`}${part?.truncated ? `<p class="note">正文已截断。</p>` : ""}</details>`;
  if (data.image) html += renderArtifactImage(data.image);
  if (!data.image?.available) html += content(data.delivered, "交付版本");
  if (!data.image?.available && (data.version === "current_changed" || !data.delivered?.available && data.current?.available)) html += content(data.current, "当前文件（读取时快照）");
  html += `<details class="artifact-version" data-key="artifact-diff-${h(state.key)}"><summary>${h(data.diff?.label || "改动差异")}</summary>${data.diff?.available ? `<pre class="artifact-diff">${h(data.diff.text || "没有差异。")}</pre>` : `<p class="note">${h(data.diff?.note || "没有改前内容，无法比较。")}</p>`}${data.diff?.truncated ? `<p class="note">差异内容已截断。</p>` : ""}</details></div>`;
  return html;
}

export function renderWeaves(state, options = {}) {
  if (!state?.items.length && !state?.error) return "";
  let html = state?.error ? `<div class="callout warning"><p>批量执行任务状态读取失败：${h(workflowStatusText(state.error))}${state.items.length ? "；下方保留上次快照，可能已过期。" : ""}</p><button type="button" data-weaves-retry>重新读取</button></div>` : "";
  if (!state?.items.length) return html;
  const labels = {queued:"排队中",running:"执行中",completed:"已完成",partial:"未完整结束",failed:"执行失败",aborted:"已中止",error:"启动失败",interrupted_by_restart:"重启中断"};
  html += `<section class="weave-section" aria-label="批量执行任务"><div class="section-heading"><h3>批量执行任务</h3><span class="note">${options.capabilities ? "历史结果保留；续跑另建批次" : "只读任务状态"}</span></div>`;
  if (state.warning) html += `<p class="note history-note">${h(workflowStatusText(state.warning))}</p>`;
  if (state.persisted === false) html += `<p class="note history-note">任务快照尚未确认持久化。</p>`;
  for (const [batchIndex, batch] of state.items.entries()) {
    const items = Array.isArray(batch.items) ? batch.items : [], completed = items.filter(item => item.status === "completed").length;
    const eligibility = options.capabilities?.batches?.find(b => b.batch_id === batch.batch_id);
    const resumableSelection = eligibility?.items?.some(item => item.can_resume && options.selections?.get(batch.batch_id)?.has(item.id));
    const active = items.some(item => ["queued", "running"].includes(item.status));
    const stateLabel = batch.interrupted_by_restart || items.some(item => item.status === "interrupted_by_restart") ? "重启中断" : batch.cancelled ? "已中止" : active ? "执行中" : batch.final ? completed === items.length ? "已完成" : "部分未完成" : "等待收束";
    html += `<details class="weave-batch" data-key="weave-${h(batch.batch_id)}" ${batchIndex === 0 || active ? "open" : ""}><summary><span>${h(batch.spec || "批量执行批次")}</span><span>${h(stateLabel)} · ${completed} / ${items.length} 项完成</span></summary>${batch.interrupted_by_restart ? `<p class="note weave-warning">服务重启打断了本批次。未记录的交接正文无法恢复，界面不会自动重跑。</p>` : !batch.final ? `<p class="note weave-warning">批次尚未收束；逐项结果正文将在收束后展示。</p>` : ""}<div class="weave-table-scroll"><table class="weave-table"><caption class="sr-only">${h(batch.spec || "批量执行批次")}的逐项进度与结果</caption><thead><tr><th scope="col">任务</th><th scope="col">状态</th><th scope="col">模型</th></tr></thead><tbody>${items.map(item => {
      const stateClass = ["failed", "error", "interrupted_by_restart"].includes(item.status) ? "error" : item.status === "completed" ? "success" : ["partial", "aborted"].includes(item.status) ? "warning" : "";
      const missing = batch.interrupted_by_restart && item.status === "completed" ? "交接正文未记录。" : !batch.final ? "结果正文将在批次收束后展示。" : ["queued", "running"].includes(item.status) ? "本项尚未返回结果。" : "本项没有可读取的结果正文。";
      const eligible = eligibility?.items?.find(i => i.id === item.id), checked = options.selections?.get(batch.batch_id)?.has(item.id);
      const selection = options.capabilities ? `<label class="resume-choice"><input id="resume-${h(item.id)}" type="checkbox" data-resume-batch="${h(batch.batch_id)}" data-resume-item="${h(item.id)}" ${checked && eligible?.can_resume ? "checked" : ""} ${!eligible?.can_resume ? "disabled" : ""}><span>${h(item.label || "未命名任务")}</span></label>${!eligible?.can_resume ? `<p class="note">${h(workflowStatusText(eligible?.resume_reason || "当前不可续跑；请刷新后核对状态。"))}</p>` : ""}` : h(item.label || "未命名任务");
      return `<tr><th scope="row"><span class="weave-item-index">${Number.isInteger(item.index) ? item.index + 1 : ""}</span>${selection}</th><td><span class="badge ${stateClass}">${h(labels[item.status] || item.status)}</span></td><td>${h(item.model || "—")}</td></tr><tr class="weave-detail-row"><td colspan="3"><details data-key="weave-item-${h(item.id)}"><summary>${options.itemControls ? "结果与交互" : "查看本项结果"}</summary><div class="weave-result">${item.answer ? renderText(item.answer) : `<p class="note">${missing}</p>`}${item.answer_truncated ? `<p class="note">结果正文已截断。</p>` : ""}${item.error ? `<p class="note">${h(workflowStatusText(item.error))}</p>` : ""}${item.worktree ? `<p class="note">工作副本：${h(item.worktree)}</p>` : ""}${options.artifactCard ? (item.artifacts || []).map(a => options.artifactCard(a,{batch_id:batch.batch_id,item_id:item.id})).join("") : ""}</div>${options.itemControls ? options.itemControls(batch,item) : ""}</details></td></tr>`;
    }).join("")}</tbody></table></div>${options.capabilities ? `<div class="weave-batch-actions"><button type="button" data-resume-weave="${h(batch.batch_id)}" ${!options.canSubmit || !options.capabilities.can_resume || !resumableSelection ? "disabled" : ""}>续跑选中项</button><p class="note">先补充要求并预览，再提交新批次。</p></div>` : ""}${batch.source_batch_id ? `<details class="weave-provenance" data-key="weave-origin-${h(batch.batch_id)}"><summary>查看续跑来源</summary><p class="note">来源批次：${h(batch.source_batch_id)}</p><ul>${(batch.source_item_ids || []).map(id => `<li>${h(state.items.find(b => b.batch_id === batch.source_batch_id)?.items?.find(i => i.id === id)?.label || id)}</li>`).join("")}</ul></details>` : ""}</details>`;
  }
  if (state.cursor !== null) html += `<div class="history-pagination"><span>已显示 ${state.items.length} / ${h(state.total)} 个批次</span><button type="button" data-weaves-more ${state.state === "loading" ? "disabled" : ""}>加载更早批次</button></div>`;
  return `${html}</section>`;
}
