import {escapeHTML as h} from './client.mjs';
const text=v=>v===undefined ? '（此键已删除）' : typeof v==='string' ? v : JSON.stringify(v,null,2);
export function renderLocalConflicts(state){
  if(!state?.conflicts?.length)return '';
  return `<section class="callout warning" role="alert"><h3>多个窗口修改了同一内容</h3><p>自动合并已暂停，原输入保留。提交回执冲突不能以另一新请求覆盖；先核对当前会话与服务。</p>${state.conflicts.map(c=>`<article><h4>${c.group==='drafts' ? '草稿' : '提交回执'} · ${h(c.key)}</h4><details><summary>本窗口内容</summary><pre>${h(text(c.local))}</pre></details><details><summary>服务已保存内容</summary><pre>${h(text(c.remote))}</pre></details>${c.group==='drafts' ? `<button type="button" data-state-conflict="local" data-state-group="${h(c.group)}" data-state-key="${h(c.key)}">明确保留本窗口草稿</button>` : ''}<button type="button" data-state-conflict="remote" data-state-group="${h(c.group)}" data-state-key="${h(c.key)}">${c.group==='drafts' ? '明确采用已保存草稿' : '已核对，采用已保存提交回执'}</button></article>`).join('')}</section>`;
}
export async function resolveLocalConflict(state,group,key,choice){
  state.resolveConflict(group,key,choice);
  if(!state.conflicts.length)await state.flush();
  return {rebuildOutbox:group==='outbox',reloadDraft:group==='drafts' && choice==='remote',key};
}
