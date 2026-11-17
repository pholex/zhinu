import {escapeHTML as h} from "./client.mjs";

const array = value => Array.isArray(value) ? value : [];
const kinds = ["weave_message","weave_stop"];
export const weaveControlKey = (batch,item) => JSON.stringify([batch.batch_id,item.id,item.execution_id || ""]);
export const weaveMessageDraftKey = (sid,batch,item) => `weave-message:${sid}:${batch}:${item}`;
export function weaveActionTarget(action) {
  const match = action.path?.match(/\/weaves\/([^/]+)\/items\/([^/]+)\/(messages|stop)$/);
  if (!match || typeof action.body?.execution_id !== "string" || !action.body.execution_id) return null;
  try { return [decodeURIComponent(match[1]),decodeURIComponent(match[2]),action.body.execution_id]; } catch { return null; }
}
export function parseWeaveMessageDrafts(raw) {
  if (!raw) return {};
  try {
    const value = JSON.parse(raw);
    if (!value || Array.isArray(value) || typeof value !== "object" || Object.values(value).some(v => typeof v !== "string")) throw new Error();
    return value;
  } catch { throw new Error("已保存的单项消息草稿无法读取，原数据未覆盖。"); }
}
export function weaveControlState(batch,item,{writable = false,protocol = false,stale = false,persisted = false,pending = []} = {}) {
  const key = weaveControlKey(batch,item), queued = kind => pending.some(a => a.kind === kind && JSON.stringify(weaveActionTarget(a)) === key);
  const current = writable && protocol && !stale && !batch.interrupted_by_restart && item.live === true && !item.recovered && !!item.execution_id && ["starting","running"].includes(item.execution_state);
  const reason = stale ? "快照尚未核对，暂不能操作此项。" : !item.execution_id ? "本项尚未开始，没有执行实例；暂不支持消息或单项停止。" : item.recovered || item.live !== true || batch.interrupted_by_restart ? "历史或未知执行实例不可控制；不会自动恢复或转投。" : !protocol ? "当前服务只支持查看单项结果。" : !writable ? "连接或本机提交持久化尚未就绪。" : !["starting","running"].includes(item.execution_state) ? "此执行实例已结束，不能继续发送。" : "";
  return {canMessage:!!(current && persisted && item.controls?.can_message && !queued("weave_message")),canStop:!!(current && item.controls?.can_stop && !queued("weave_stop")),reason,
    messageReason:reason || (!persisted ? "消息需要可靠的服务持久化，当前不能发送。" : item.controls?.message_reason || (!item.controls?.can_message ? item.controls?.reason : "")),stopReason:reason || item.controls?.stop_reason || (!item.controls?.can_stop ? item.controls?.reason : ""),messagePending:queued("weave_message"),stopPending:queued("weave_stop")};
}
export function renderWeaveOperation(operation) {
  const labels = {reserved:"已登记，尚未确认排队",queued:"已排队，尚未进入此执行实例上下文",accepted:"已进入此执行实例上下文",not_consumed:"执行实例已结束，消息未被消费",requested:"已请求停止，等待执行实例退出",stop_requested:"已请求停止，等待执行实例退出",settled:"当前执行实例已退出",stopped:"当前执行实例已退出",failed:"操作失败",unknown:"操作结果未知，请核对"};
  return `<article class="weave-operation"><strong>${operation.kind === "stop" ? "单项停止" : "单项消息"} · ${h(labels[operation.status] || "状态尚未识别")}</strong>${operation.text ? `<p class="fleet-message-text">${h(operation.text)}</p>` : ""}${operation.status === "accepted" ? `<p class="note">已加入上下文，不代表模型已经回答或遵循要求。</p>` : ""}${operation.kind === "stop" && ["settled","stopped"].includes(operation.status) ? `<p class="note">只说明这一执行实例已退出，不保证它是因停止请求结束。</p>` : ""}${operation.reason ? `<p class="note">${h(operation.reason)}</p>` : ""}${operation.persisted === false ? `<p class="fleet-warning">此停止回执未可靠持久化，不承诺服务重启后能按原键核对。</p>` : ""}${operation.warning ? `<p class="fleet-warning">${h(operation.warning)}</p>` : ""}<p class="note">操作 ${h(operation.operation_id)} · 执行 ${h(operation.execution_id)}</p></article>`;
}
export function renderWeaveItemControls(batch,item,options = {}) {
  const {protocol = false,drafts = {},draftError = "",error = "",receipts = [],receiptWarning = ""} = options;
  const operations = array(item.operations).filter(op => op.execution_id === item.execution_id), old = receipts.filter(v => v.ref.execution_id !== item.execution_id || !operations.some(op => op.operation_id === v.ref.operation_id));
  let html = operations.length ? `<details open data-key="weave-ops-${h(item.id)}-${h(item.execution_id)}"><summary>当前执行的操作记录 · ${operations.length}</summary>${operations.map(renderWeaveOperation).join("")}</details>` : "";
  if (old.length || receiptWarning) html += `<details data-key="weave-receipts-${h(item.id)}"><summary>已保存的操作回执 · ${old.length}</summary><p class="note">仅核对原操作，不会重新发送。${h(receiptWarning)}</p>${old.map(v => `<section>${v.error ? `<p class="form-error">回执读取失败：${h(v.error)}。${v.data ? "以下为上次记录，可能已过期。" : "目前无法确认结果。"}</p>` : ""}${v.data ? renderWeaveOperation(v.data) : `<p class="note">${v.ref.persisted === false ? "此停止回执未可靠持久化，重启后可能无法核对。" : "正在核对操作回执…"} ${h(v.ref.warning || "")} ${h(v.ref.operation_id)}</p>`}</section>`).join("")}</details>`;
  if (!protocol) return html;
  const key = weaveControlKey(batch,item), control = weaveControlState(batch,item,options), text = drafts[item.execution_id] || "", oldDrafts = Object.entries(drafts).filter(([id,text]) => id !== item.execution_id && text);
  html += `<section class="weave-controls fleet-controls" aria-label="单项任务交互"><p class="note">${item.execution_phase === "follow_up" ? "自动追问执行" : "首次执行"} · 当前执行实例 <code>${h(item.execution_id || "尚未建立")}</code></p>${control.reason ? `<p class="note">${h(control.reason)}</p>` : ""}${draftError || error ? `<p class="form-error">${h(draftError || error)}</p>` : ""}<form data-weave-message="${h(key)}"><label for="weave-message-${h(encodeURIComponent(key))}">给此执行实例补充说明<textarea id="weave-message-${h(encodeURIComponent(key))}" data-weave-message-draft="${h(key)}" rows="3" maxlength="8000" ${draftError || !item.execution_id ? "readonly" : ""}>${h(text)}</textarea></label><div class="actions"><button type="submit" ${!control.canMessage || draftError || !text.trim() ? "disabled" : ""}>${control.messagePending ? "等待提交核对" : "发送给此项"}</button><button type="button" data-weave-control="stop" data-weave-target="${h(key)}" ${!control.canStop ? "disabled" : ""}>${control.stopPending || item.stop_requested ? "停止请求待核对" : "请求停止此项"}</button></div></form>${control.messageReason && control.messageReason !== control.reason ? `<p class="note">消息：${h(control.messageReason)}</p>` : ""}${control.stopReason && control.stopReason !== control.reason ? `<p class="note">停止：${h(control.stopReason)}</p>` : ""}${options.persisted === false && control.canStop ? `<p class="fleet-warning">服务日志不可可靠持久化；仍可停止当前实例，但不承诺重启后可核对原停止回执。</p>` : ""}<p class="note">消息先排队；停止请求不等于已退出。单项停止不会停止兄弟任务，并阻止本项后续自动追问。已有成果与工作副本按原交付规则保留，不执行强制清理。</p>`;
  if (oldDrafts.length) html += `<details data-key="weave-old-drafts-${h(item.id)}"><summary>保留的旧执行草稿 · ${oldDrafts.length}</summary><p class="note">自动追问或新执行使用不同身份；旧草稿不会自动转投，请核对后手动复制。</p>${oldDrafts.map(([id,text]) => `<label>执行 ${h(id)}<textarea rows="3" readonly>${h(text)}</textarea></label>`).join("")}</details>`;
  return `${html}</section>`;
}
export function renderWeaveApprovalIdentity(identity) {
  if (identity?.kind !== "qixiang") return "";
  return `<div class="fleet-approval-owner"><strong>批量执行单项工具调用 · ${h(identity.label || identity.item_id || "任务身份未记录")}</strong><dl class="fleet-facts"><div><dt>批次</dt><dd>${h(identity.batch_id)}</dd></div><div><dt>任务</dt><dd>${h(identity.item_id)}</dd></div><div><dt>执行实例</dt><dd>${h(identity.execution_id)}</dd></div><div><dt>任务工作区</dt><dd><code>${h(identity.workspace || "未记录")}</code></dd></div></dl><p class="note">本次决定只允许或拒绝此任务的这一次工具调用，不授权其他条目。</p></div>`;
}
export function createWeaveControlController(ui) {
  const groups = new Map(), refs = new Map(), views = new Map(), errors = new Map();
  const context = () => ui.context(), state = sid => ui.state(sid), paint = sid => {if(context().active && context().sid === sid)ui.render();};
  const target = (sid,key) => {for(const batch of array(state(sid)?.items))for(const item of array(batch.items))if(weaveControlKey(batch,item) === key)return {batch,item};return null;};
  function group(sid,batch,item) {
    const key = weaveMessageDraftKey(sid,batch,item);
    if(!groups.has(key)) {try {groups.set(key,{key,drafts:parseWeaveMessageDrafts(context().conn?.localState?.getDraft(key)),error:""});}catch(error){groups.set(key,{key,drafts:{},error:error.message});}}
    return groups.get(key);
  }
  function references(sid,batch,item) {
    const key = `weave-receipts:${sid}:${batch}:${item}`;
    if(!refs.has(key))try {
      const raw=context().conn?.localState?.getDraft(key), data=raw ? JSON.parse(raw) : {refs:[],truncated:false};
      if(!Array.isArray(data.refs) || data.refs.length>64 || data.refs.some(r=>![r.operation_id,r.execution_id,r.kind].every(v=>typeof v==="string" && v.length<=200)))throw new Error("操作编号索引格式不可读取，原数据未覆盖。");
      refs.set(key,{...data,key,error:""});
    }catch(error){refs.set(key,{key,refs:[],error:error.message});}
    return refs.get(key);
  }
  function options(sid,batch,item) {
    const c=context(), s=state(sid), g=group(sid,batch.batch_id,item.id), saved=references(sid,batch.batch_id,item.id);
    return {writable:!!c.writable,protocol:c.conn?.health?.weave_control_protocol===1,stale:!!s?.error || c.conn?.healthy===false,persisted:s?.persisted===true,pending:c.pending || [],drafts:g.drafts,draftError:g.error,error:errors.get(weaveControlKey(batch,item)) || "",receipts:saved.refs.map(ref=>({ref,...views.get(`${sid}:${ref.operation_id}`)})),receiptWarning:saved.error || (saved.truncated ? "仅保留最近 64 条操作编号，这不是完整历史。" : "")};
  }
  function save(g,conn=context().conn) {if(Object.keys(g.drafts).length)conn?.localState?.setDraft(g.key,JSON.stringify(g.drafts));else conn?.localState?.deleteDraft(g.key);}
  function send(key,kind) {
    const c=context();if(!c.active || !c.sid)return;
    const t=target(c.sid,key);if(!t)return;
    const {batch,item}=t, control=weaveControlState(batch,item,options(c.sid,batch,item));
    if(kind==="weave_message" ? !control.canMessage : !control.canStop)return;
    const g=group(c.sid,batch.batch_id,item.id), text=g.drafts[item.execution_id] || "";
    if(kind==="weave_message" && (g.error || !text.trim()))return;
    const path=`/session/${encodeURIComponent(c.sid)}/weaves/${encodeURIComponent(batch.batch_id)}/items/${encodeURIComponent(item.id)}`;
    errors.delete(key);
    ui.submit(`${c.sid}:${kind}:${key}`,{kind,sid:c.sid,path:`${path}/${kind==="weave_message" ? "messages" : "stop"}`,body:{execution_id:item.execution_id,...(kind==="weave_message" ? {text} : {})},...(kind==="weave_message" ? {draftKey:g.key,draftText:text} : {})});
  }
  async function loadReceipts(conn,sid) {
    const s=state(sid);if(!s || conn!==context().conn || conn.health?.weave_control_protocol!==1)return;
    const jobs=[];
    for(const batch of array(s.items))for(const item of array(batch.items))for(const ref of references(sid,batch.batch_id,item.id).refs) {
      if(array(item.operations).some(op=>op.operation_id===ref.operation_id && op.execution_id===ref.execution_id))continue;
      const key=`${sid}:${ref.operation_id}`, prior=views.get(key);
      if(prior?.loading || prior?.data && !prior.error && ["accepted","not_consumed","settled","stopped","failed"].includes(prior.data.status) && prior.data.persisted!==false)continue;
      const view=prior || {ref};view.loading=true;views.set(key,view);
      jobs.push(async()=>{try {
        const {operation}=await conn.client.get(`/session/${encodeURIComponent(sid)}/weave-operations/${encodeURIComponent(ref.operation_id)}`,conn.controller.signal);
        if(context().conn!==conn || state(sid)!==s || views.get(key)!==view)return;
        if(!operation || operation.operation_id!==ref.operation_id || operation.execution_id!==ref.execution_id || operation.batch_id!==batch.batch_id || operation.item_id!==item.id || operation.kind!==ref.kind)throw new Error("回执身份与原执行实例不匹配。");
        view.data=operation;view.error="";
      }catch(error){if(context().conn===conn && views.get(key)===view)view.error=error.message;}finally{view.loading=false;}});
    }
    for(let offset=0;offset<jobs.length;offset+=8){if(context().conn!==conn || state(sid)!==s)return;await Promise.allSettled(jobs.slice(offset,offset+8).map(job=>job()));}
    paint(sid);
  }
  return {loadReceipts,
    reset(){groups.clear();refs.clear();views.clear();errors.clear();},
    render(sid,batch,item){return renderWeaveItemControls(batch,item,options(sid,batch,item));},
    input(event){const key=event.target.dataset.weaveMessageDraft;if(!key)return false;const c=context(), t=c.active && target(c.sid,key);if(!t || !t.item.execution_id)return true;const g=group(c.sid,t.batch.batch_id,t.item.id);if(g.error)return true;const text=event.target.value;if(text)Object.defineProperty(g.drafts,t.item.execution_id,{value:text,enumerable:true,writable:true,configurable:true});else delete g.drafts[t.item.execution_id];save(g);errors.delete(key);paint(c.sid);return true;},
    onSubmit(event){const key=event.target.dataset.weaveMessage;if(!key)return false;event.preventDefault();send(key,"weave_message");return true;},
    click(event){const node=event.target.closest("[data-weave-control]");if(!node)return false;if(node.dataset.weaveControl==="stop")send(node.dataset.weaveTarget,"weave_stop");return true;},
    accepted(conn,action){
      if(!kinds.includes(action.kind) || action.state!=="accepted")return;
      const identity=weaveActionTarget(action), id=action.result?.operation_id;
      if(identity && typeof id==="string" && id.length<=200){
        const [batch,item,execution]=identity, saved=references(action.sid,batch,item);if(saved.error)throw new Error(saved.error);
        if(!saved.refs.some(ref=>ref.operation_id===id))saved.refs.push({operation_id:id,execution_id:execution,kind:action.kind==="weave_message" ? "message" : "stop",persisted:action.result?.operation_persisted,warning:String(action.result?.operation_warning || "").slice(0,1000)});
        if(saved.refs.length>64){saved.refs=saved.refs.slice(-64);saved.truncated=true;}
        conn.localState?.setDraft(saved.key,JSON.stringify({refs:saved.refs,truncated:!!saved.truncated}));
      }
      if(action.kind!=="weave_message" || !action.draftKey || !["queued","accepted"].includes(action.result?.operation_status))return;
      const execution=action.body.execution_id;
      if(conn.localState)try{const drafts=parseWeaveMessageDrafts(conn.localState.getDraft(action.draftKey));if(drafts[execution]===action.draftText){delete drafts[execution];save({key:action.draftKey,drafts},conn);}}catch{/* Keep unreadable original drafts. */}
      const g=groups.get(action.draftKey);if(g?.drafts[execution]===action.draftText)delete g.drafts[execution];
    },
    rejected(action,error){if(!kinds.includes(action.kind))return;const identity=weaveActionTarget(action);if(identity)errors.set(JSON.stringify(identity),error.status===409 ? "执行实例已变化或结束。原草稿保留，不会自动转投新执行。" : error.message);},
  };
}
