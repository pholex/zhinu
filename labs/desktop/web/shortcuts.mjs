// Desktop actions only. The terminal's key table remains owned by the core UI.
export const DEFAULT_SHORTCUTS = {"new_task":"mod+n","search":"mod+k","settings":"mod+,","sidebar":"mod+b","panel":"mod+shift+b"};
export const SHORTCUTS = Object.freeze([
  {id:"new_task",label:"新建任务",inInput:true},
  {id:"search",label:"搜索",inInput:true},
  {id:"settings",label:"打开设置",inInput:true},
  {id:"sidebar",label:"展开或收起侧栏",inInput:false},
  {id:"panel",label:"展开或收起工具面板",inInput:false},
]);
const reservedKeys=new Set("acvxyzqwhmrltsfp");
const h=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
export function validateShortcuts(value){
  if(!value||typeof value!=="object"||Array.isArray(value)||Object.keys(value).sort().join(",")!==Object.keys(DEFAULT_SHORTCUTS).sort().join(","))throw new Error("快捷键动作列表不正确");
  const result={},seen=new Map();
  for(const item of SHORTCUTS){let binding=value[item.id];if(typeof binding!=="string"||binding.length>40)throw new Error("快捷键格式不正确");binding=binding.trim().toLowerCase();if(binding){if(!/^mod\+(?:shift\+)?[a-z0-9,./]$/.test(binding))throw new Error("请输入 Mod(+Shift)+字母、数字、逗号、句点或斜杠，留空关闭此项");if(reservedKeys.has(binding.at(-1)))throw new Error("此组合由系统或文本编辑保留，请选择其他快捷键");if(seen.has(binding))throw new Error(`“${item.label}”与“${seen.get(binding)}”使用相同快捷键`);seen.set(binding,item.label);}result[item.id]=binding;}
  return result;
}
export function shortcutAction(event, bindings=DEFAULT_SHORTCUTS){
  if(event.defaultPrevented||event.isComposing||event.repeat||event.altKey||(!event.metaKey&&!event.ctrlKey)||(event.metaKey&&event.ctrlKey))return null;
  let map;try{map=validateShortcuts(bindings||DEFAULT_SHORTCUTS);}catch{return null;}
  const key=String(event.key||"").toLowerCase();
  // key reports the shifted symbol for punctuation; code identifies the accepted physical punctuation.
  const punctuation={Comma:",",Period:".",Slash:"/"};
  const plain=punctuation[event.code]||key;
  const binding=`mod+${event.shiftKey?"shift+":""}${plain}`;
  const target=event.target;const editing=!!(target?.isContentEditable||["INPUT","TEXTAREA","SELECT"].includes(target?.tagName)||target?.closest?.('[contenteditable="true"]'));
  return SHORTCUTS.find(item=>map[item.id]===binding&&(item.inInput||!editing))?.id||null;
}
export function createShortcutsController({values,onChange,render}){
  let draft=null,error="",changed=false;
  const read=()=>draft??{...DEFAULT_SHORTCUTS,...(values?.()||{})};
  function html(){const data=read();return `<section class="shortcut-settings"><p class="note">Mod 表示 macOS 的 ⌘，其他系统的 Ctrl。留空可关闭单项；系统编辑、关闭与发送按键保留。</p>${error?`<p role="alert">${h(error)}</p>`:""}<div>${SHORTCUTS.map(item=>`<label>${h(item.label)}<input data-shortcut="${h(item.id)}" value="${h(data[item.id])}" spellcheck="false" autocomplete="off" placeholder="留空关闭"></label>`).join("")}</div><div class="actions"><button type="button" data-shortcut-action="apply" ${changed?"":"disabled"}>应用快捷键</button><button type="button" data-shortcut-action="defaults">恢复默认快捷键</button></div></section>`;}
  function input(e){const id=e.target.dataset?.shortcut;if(!SHORTCUTS.some(s=>s.id===id))return false;draft={...read(),[id]:e.target.value};changed=true;try{validateShortcuts(draft);error="";}catch(e){error=e.message;}render?.();return true;}
  async function apply(value){try{const valid=validateShortcuts(value);await onChange?.(valid);draft=valid;changed=false;error="";}catch(e){error=e.message;changed=true;}render?.();}
  function click(e){const node=e.target.closest('[data-shortcut-action]');if(!node)return false;if(node.dataset.shortcutAction==='defaults')apply({...DEFAULT_SHORTCUTS});else if(node.dataset.shortcutAction==='apply')apply(read());return true;}
  return {render:html,input,click,reset(){draft=null;error="";changed=false;}};
}
