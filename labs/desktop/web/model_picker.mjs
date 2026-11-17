import {escapeHTML as h} from './client.mjs';
import {icon, positionMenu} from './ui.mjs';

/** Only configured choices can become a new conversation's default. */
export function defaultComposerModel({models = [], pending = "", saved = "", effective = ""} = {}) {
  const ids = models.map(model => model.id).filter(Boolean);
  return [pending, saved, effective].find(id => id && ids.includes(id)) || ids[0] || effective || "";
}

let pickerSequence = 0;

/** A presentation layer over the existing select; choosing never submits a task. */
export function createModelPicker({root, select, label = "模型", symbol = "cpu", heading = "选择模型", defaultLabel = "跟随服务默认模型", searchable = true, hint = "选择新会话使用的模型", compact = false, activateOnReselect = false}) {
  if (!root || !select || select.tagName !== 'SELECT') throw new TypeError('模型选择器需要容器和原生 select');
  const doc = root.ownerDocument, win = doc.defaultView;
  const originalHidden = select.hidden, identifier = `model-options-${++pickerSequence}`;
  const details = doc.createElement('details');
  details.className = `toolbar-menu model-picker${compact ? ' choice-picker' : ''}`;
  details.innerHTML = `<summary class="model-picker-trigger" aria-haspopup="listbox" aria-controls="${identifier}" aria-expanded="false">${icon(symbol)}<span class="model-picker-label"></span>${icon('chevron')}</summary><div class="menu-items model-picker-menu"><p class="model-picker-heading">${h(heading)}</p><label class="model-picker-search">${icon('search')}<input type="search" placeholder="搜索模型" aria-label="搜索模型" autocomplete="off" spellcheck="false"></label><div class="model-picker-options" id="${identifier}" role="listbox" aria-label="${h(label)}"></div><p class="model-picker-empty" role="status" hidden>没有匹配的模型</p></div>`;
  const trigger = details.querySelector('summary'), caption = details.querySelector('.model-picker-label');
  const searchLabel = details.querySelector('.model-picker-search'), search = searchLabel.querySelector('input');
  const list = details.querySelector('.model-picker-options'), empty = details.querySelector('.model-picker-empty');
  let models = [], choices = [], rows = [], query = '', destroyed = false, lastList = '';
  const removers = [];
  function listen(node, name, fn) {node.addEventListener(name, fn);removers.push(()=>node.removeEventListener(name, fn));}
  function reposition() {
    if (!details.open || destroyed) return;
    const boundary = doc.getElementById('workspace') || root.closest('main') || doc.documentElement;
    const viewportTop = win.visualViewport?.offsetTop || 0;
    const top = Math.max(viewportTop, boundary.getBoundingClientRect().top) + 12;
    const room = Math.max(0, trigger.getBoundingClientRect().top - 10 - top);
    details.style.setProperty('--model-menu-room', `${room}px`);
    positionMenu(details, boundary);
  }
  function close(restoreFocus = false) {
    const wasOpen = details.open;
    details.open = false;
    trigger.setAttribute('aria-expanded', 'false');
    if (restoreFocus && wasOpen && trigger.isConnected) trigger.focus({preventScroll:true});
  }
  function buttons() {return [...list.querySelectorAll('[data-model-value]')].filter(n=>!n.disabled);}
  function focusOption(index) {
    const options = buttons();
    const node = options[Math.max(0, Math.min(options.length - 1, index))];
    if (node) {node.focus({preventScroll:true});node.scrollIntoView({block:'nearest'});}
  }
  function selectedIndex() {return Math.max(0, buttons().findIndex(n=>n.dataset.modelValue === select.value));}
  function paintList() {
    const needle = query.trim().toLocaleLowerCase();
    const visible = rows.filter(row=>!needle || `${row.label} ${row.value}`.toLocaleLowerCase().includes(needle));
    const focused = list.contains(doc.activeElement) ? doc.activeElement?.dataset.modelValue : undefined;
    const html = visible.map(row=>`<button type="button" class="model-picker-option${row.inherited?' picker-option-inherited':''}" title="${h(row.description?`${row.label} · ${row.description}`:row.label)}" role="option" data-model-value="${h(row.value)}" aria-selected="${row.value===select.value}" ${row.disabled?'disabled':''}>${row.symbol?`<span class="picker-option-icon">${icon(row.symbol)}</span>`:''}<span class="picker-option-copy"><span class="model-picker-option-label">${h(row.label)}</span>${row.description?`<span class="picker-option-description">${h(row.description)}</span>`:''}</span><span class="model-picker-check">${row.value===select.value?icon('check'):''}</span></button>`).join('');
    if (html !== lastList) {
      list.innerHTML = html;
      lastList = html;
      if (focused !== undefined) {
        const replacement = buttons().find(node=>node.dataset.modelValue === focused);
        if (replacement) replacement.focus({preventScroll:true});
        else if (!searchLabel.hidden) search.focus({preventScroll:true});
        else if (details.open) trigger.focus({preventScroll:true});
      }
    }
    empty.hidden = visible.length > 0;
    reposition();
  }
  function render(values = {}) {
    if (destroyed) return;
    if (Array.isArray(values.models)) models = values.models;
    if (Array.isArray(values.choices)) choices = values.choices;
    const presentations = new Map(choices.map(choice=>[choice.id,choice]));
    const labels = new Map(models.filter(m=>m && typeof m.id==='string').map(m=>[m.id, m.label || m.name || m.id]));
    rows = [...select.options].map(option=>({...presentations.get(option.value),value:option.value,label:presentations.get(option.value)?.label || (option.value ? String(labels.get(option.value)||option.label||option.textContent||option.value) : defaultLabel),disabled:option.disabled || option.parentElement?.tagName==='OPTGROUP' && option.parentElement.disabled}));
    const selected = rows.find(row=>row.value===select.value);
    caption.textContent = selected?.caption || selected?.label || heading;
    trigger.querySelector(".ui-icon").outerHTML = icon(selected?.triggerSymbol || selected?.symbol || symbol);
    trigger.setAttribute('aria-label', `${label}：${caption.textContent}`);
    trigger.setAttribute('aria-disabled', String(select.disabled));
    trigger.title = `${caption.textContent} · ${select.disabled ? (select.title || '当前会话配置已确定，新建会话后可切换。') : (selected?.description || hint)}`;
    const needsSearch = searchable && rows.length > 6;
    if (!needsSearch && query) {query='';search.value='';}
    if (!needsSearch && doc.activeElement===search) trigger.focus({preventScroll:true});
    searchLabel.hidden = !needsSearch;
    if (select.disabled) close(details.contains(doc.activeElement));
    paintList();
  }
  function open(position = 'selected') {
    if (destroyed || select.disabled) return;
    query='';search.value='';render();
    details.open = true;
    trigger.setAttribute('aria-expanded', 'true');
    reposition();
    if (position==='search' && !searchLabel.hidden) search.focus({preventScroll:true});
    else focusOption(position==='last' ? buttons().length-1 : position==='first' ? 0 : selectedIndex());
  }
  function choose(value) {
    if (select.disabled) return;
    const option = [...select.options].find(row=>row.value===value);
    if (!option || option.disabled || option.parentElement?.tagName==='OPTGROUP' && option.parentElement.disabled) return;
    if (select.value !== value || activateOnReselect) {
      select.value = value;
      select.dispatchEvent(new win.Event('change', {bubbles:true}));
    }
    render();close(true);
  }
  function onKey(event) {
    if (event.isComposing) return;
    const target = event.target, key = event.key;
    if (key==='Escape' && details.open) {event.preventDefault();event.stopPropagation();close(true);return;}
    if (target===trigger) {
      if (['ArrowDown','ArrowUp','Home','End','Enter',' '].includes(key)) {
        event.preventDefault();
        if (details.open && ['Enter',' '].includes(key)) close(true);
        else open(key==='Home'?'first':key==='End'||key==='ArrowUp'?'last':['Enter',' '].includes(key)?'search':'selected');
      }
      return;
    }
    if (!details.open) return;
    const options = buttons(), index = options.indexOf(target);
    if (key==='ArrowDown' || key==='ArrowUp') {
      event.preventDefault();focusOption(index<0 ? (key==='ArrowDown'?0:options.length-1) : index+(key==='ArrowDown'?1:-1));
    } else if (index>=0 && (key==='Home' || key==='End')) {
      event.preventDefault();focusOption(key==='Home'?0:options.length-1);
    } else if (index>=0 && (key==='Enter' || key===' ')) {
      event.preventDefault();choose(target.dataset.modelValue);
    } else if (key==='Enter' && target===search) {
      event.preventDefault();if (options.length) choose(options[0].dataset.modelValue);
    }
  }
  try {
    root.append(details);
    listen(trigger, 'click', event=>{event.preventDefault();if (select.disabled) return;if (details.open) close(true);else open('search');});
    listen(details, 'keydown', onKey);
    // Keep focus inside the list until a pointer selection commits in the native web view.
    listen(list, 'pointerdown', event=>{if(event.button===0 && event.target.closest('[data-model-value]'))event.preventDefault();});
    listen(list, 'click', event=>{const node=event.target.closest('[data-model-value]');if (node && list.contains(node)) choose(node.dataset.modelValue);});
    listen(search, 'input', ()=>{query=search.value;paintList();});
    listen(details, 'toggle', ()=>{if (details.open && select.disabled) close();trigger.setAttribute('aria-expanded', String(details.open));reposition();});
    listen(details, 'focusout', ()=>{queueMicrotask(()=>{if (!destroyed && details.open && !details.contains(doc.activeElement)) close();});});
    listen(doc, 'pointerdown', event=>{if (details.open && !details.contains(event.target)) close();});
    listen(win, 'resize', reposition);
    listen(select, 'change', ()=>render());
    if (win.ResizeObserver) {
      const observer = new win.ResizeObserver(reposition);
      observer.observe(doc.getElementById('workspace') || root);
      removers.push(()=>observer.disconnect());
    }
    render();
    select.hidden = true;
  } catch (error) {
    removers.forEach(remove=>remove());details.remove();select.hidden=originalHidden;throw error;
  }
  return {render,close,destroy(){if(destroyed)return;destroyed=true;removers.forEach(remove=>remove());details.remove();select.hidden=originalHidden;}};
}
