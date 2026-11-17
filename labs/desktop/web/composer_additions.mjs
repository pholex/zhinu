import {escapeHTML as h} from './client.mjs';
import {icon,positionMenu} from './ui.mjs';
import {validateCapabilities} from './capabilities.mjs';

export function appendReferences(draft, entries) {
  const fresh=[...new Set(entries.filter(Boolean))].filter(entry=>!draft.includes(entry));
  return fresh.length ? [draft.trimEnd(),...fresh].filter(Boolean).join('\n') : draft;
}
export function localReference(entry) {
  if(!entry || typeof entry.path!=='string' || !entry.path.startsWith('/') || !['file','directory'].includes(entry.kind))throw Error('无法识别所选文件。');
  return `参考${entry.kind==='directory'?'文件夹':'文件'}：${JSON.stringify(entry.path)}`;
}

/** Add context in place; file paths are read by core tools only after submission. */
export function createComposerAdditions(ui) {
  const root=ui.root,panel=root.querySelector('.menu-items');
  let target=null,skills=[],request=0;
  const sameTarget=t=>{const c=ui.context();return c.enabled && c.conn===t.conn && c.sid===t.sid && c.workspace===t.workspace;};
  const row=(action,symbol,label,description)=>`<button type="button" data-add="${action}">${icon(symbol)}<span class="add-copy"><strong>${h(label)}</strong><small>${h(description)}</small></span></button>`;
  const back=()=>`<button type="button" data-add="back" class="add-back">${icon('arrowLeft')}<span>返回</span></button>`;
  function place(entries,t=target) {
    if(!t || !sameTarget(t)){ui.notice('当前任务已变化，请重新选择要添加的内容。');close();return;}
    ui.put(appendReferences(ui.get(),entries));close();ui.focus();
  }
  function paint(html) {panel.innerHTML=html;positionMenu(root,ui.boundary);}
  function home() {
    target=ui.context();request++;
    paint(`<p class="menu-label">添加到本次任务</p>${row('files','folder','文件或文件夹','选择本地资料，发送后按需读取')}${row('skills','capabilities','使用技能','选择已有技能辅助完成任务')}<p class="add-note">添加后可在输入框检查、修改，再发送。</p>`);
    const button=panel.querySelector('[data-add="files"]');
    if(!ui.pickFiles){button.disabled=true;button.title='请在桌面应用中选择本地文件';}
  }
  function close(){root.open=false;request++;}
  async function loadSkills() {
    const mine=++request,t=target;
    paint(`${back()}<p class="menu-label">使用技能</p><p class="add-note" role="status">正在读取技能…</p>`);
    try {
      const data=validateCapabilities(await t.conn.client.get(`/desktop/capabilities${t.sid?`?session_id=${encodeURIComponent(t.sid)}`:''}`,t.conn.controller?.signal));
      if(mine!==request || !root.open || !sameTarget(t))return;
      skills=data.skills_enabled===false?[]:data.skills.filter(s=>s.enabled!==false&&(data.scope!=='session_loaded'||s.loaded));
      paint(`${back()}<input type="search" data-add-search placeholder="搜索技能" aria-label="搜索技能"><div class="add-skills"></div><p class="add-note">${data.skills_enabled===false?'技能已在当前配置中关闭。':'选择后加入任务请求，发送后由模型调用。'}</p>`);
      filterSkills('');panel.querySelector('input').focus();
    }catch(error){if(mine===request && root.open)paint(`${back()}<p class="add-note" role="alert">${h(error.message)}</p><button type="button" data-add="skills">重试</button>`);}
  }
  function filterSkills(query) {
    const rows=skills.map((s,i)=>({...s,index:i})).filter(s=>`${s.name||s.id} ${s.description}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
    panel.querySelector('.add-skills').innerHTML=rows.map(s=>row(`skill:${s.index}`,'capabilities',s.name||s.id,s.description)).join('')||'<p class="add-note">没有可用的匹配技能。</p>';
    positionMenu(root,ui.boundary);
  }
  // Initialize before opening; a deferred toggle must never replace an active subview.
  root.querySelector('summary').addEventListener('click',e=>{
    e.preventDefault();
    if(root.open){close();return;}
    if(!ui.context().enabled)return;
    home();root.open=true;positionMenu(root,ui.boundary);
  });
  root.addEventListener('toggle',()=>{if(!root.open)request++;});
  panel.addEventListener('input',e=>{if(e.target.matches('[data-add-search]'))filterSkills(e.target.value);});
  panel.addEventListener('click',async e=>{
    const button=e.target.closest('[data-add]');if(!button || button.disabled)return;
    const action=button.dataset.add;
    if(action==='back'){home();return;}
    if(!target || !sameTarget(target)){ui.notice('当前任务暂不能添加内容。');close();return;}
    if(action==='files') {
      const t=target;close();
      try {const result=await ui.pickFiles();if(result?.cancelled)return;place((result?.items||[]).map(localReference),t);}
      catch(error){ui.notice(error.message);}
    }else if(action==='skills') await loadSkills();
    else if(action.startsWith('skill:')) {const skill=skills[Number(action.slice(6))];if(skill)place([`请使用技能 ${skill.id} 协助完成本次任务。`]);}
  });
  return {close};
}
