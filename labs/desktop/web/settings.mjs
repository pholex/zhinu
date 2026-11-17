import {executionModePresentation} from "./execution_modes.mjs";
import {DEFAULT_SHORTCUTS} from "./shortcuts.mjs";
import {escapeHTML as h} from "./client.mjs";
import {icon} from "./ui.mjs";

export const SETTINGS_SECTIONS = [
  ["general","通用","发送、会话排序与窗口退出"],
  ["appearance","外观与布局","主题、字号与动画"],
  ["models","模型与执行","新会话模型、权限和配置来源"],
  ["notifications","通知与日程","任务提醒和时区"],
  ["data","数据与导入","项目、作品、历史与归档"],
  ["integrations","插件","技能与连接器"],
  ["keyboard","键盘快捷键","导航与任务操作"],
  ["workspace","浏览器与终端","网页隔离、shell、布局和进程生命周期"],
  ["usage","用量","实际调用、输入输出token与模型路由"],
  ["diagnostics","关于与诊断","版本、服务状态和诊断导出"],
];
const SECTION_ICONS = {general:"settings",appearance:"palette",models:"cpu",notifications:"bell",data:"archive",integrations:"capabilities",keyboard:"keyboard",workspace:"terminal",usage:"chart",diagnostics:"info"};
const SECTION_DESTINATIONS = {integrations:"capabilities"};
const DESTINATION_ICONS = {connection:"connection",project:"folder",library:"library",cli:"terminal",archived:"archive",capabilities:"capabilities",connections:"connection",panes:"monitor"};
export const DEFAULT_UI = {theme:"system",font_size:14,send_key:"enter",reduced_motion:false,show_planned:true,sidebar_hidden:false,panel_width:420,nav_order:["projects","library","capabilities","spaces","automations"],hidden_nav:[],default_workflow:"auto",session_sort:"updated",session_meta:{},shortcuts:{...DEFAULT_SHORTCUTS}};
export function matchingSections(query) {
  const q=String(query).trim().toLowerCase();
  const aliases={general:"常规 默认 工作目录 发送按键 窗口 退出 关闭 服务 连接 会话排序",appearance:"主题 浅色 深色 系统 字号 字体 减少动画",models:"模型 执行 权限 提供方 密钥 端点 来源 默认",notifications:"完成 失败 审批 回答 自动化 时区 系统权限",data:"存储 占用 备份 导入 导出 归档 索引 清理",integrations:"技能 插件 MCP 授权 连接",keyboard:"快捷键 修改 冲突 恢复默认",workspace:"浏览器 终端 shell zsh bash 布局 面板宽度",usage:"用量 token 费用 调用",diagnostics:"版本 更新 诊断"};
  return SETTINGS_SECTIONS.filter(row=>(row.join(" ")+" "+aliases[row[0]]).toLowerCase().includes(q));
}
export function applyAppearance(values={}, doc=globalThis.document) {
  if(!doc)return;
  const ui={...DEFAULT_UI,...values.ui};
  doc.documentElement.dataset.theme=ui.theme;
  doc.documentElement.style.setProperty("--ui-font-size",`${ui.font_size}px`);
  doc.documentElement.classList.toggle("reduce-motion",ui.reduced_motion);
}
const options=(rows,current)=>rows.map(([value,label])=>`<option value="${h(value)}" ${String(current)===String(value)?"selected":""}>${h(label)}</option>`).join("");
export function createSettingsController({context,render,navigate,exportFile,version="开发模式",requestNotifications=null,onApplied=()=>{},extra=()=>""}) {
  let notificationMessage="",uiWrites=Promise.resolve();
  let snapshot=null,draft=null,section="general",query="",error="",message="",loading=false,saving=false,epoch=0,dirty=false;
  const paint=()=>render?.();
  function adopt(data){if(!Number.isInteger(data?.revision)||!data.values)throw new Error("设置响应无法识别");snapshot=data;draft=structuredClone(data.values);draft.ui={...DEFAULT_UI,...draft.ui};dirty=false;applyAppearance(draft);onApplied(draft);}
  async function load(conn=context().conn){if(!conn||loading)return;const generation=epoch;loading=true;paint();try{const data=await conn.client.get("/desktop/preferences",conn.controller.signal);if(generation!==epoch||conn!==context().conn)return;if(!dirty)adopt(data);error=dirty&&snapshot?.revision!==data.revision?"其他窗口已有新设置；当前草稿保留，保存前请核对。":"";}catch(e){if(generation===epoch)error=e.message;}finally{if(generation===epoch){loading=false;paint();}}}
  async function save(){if(!snapshot||saving||!context().writable)return;const conn=context().conn,generation=epoch,submitted=structuredClone(draft);saving=true;error="";message="";paint();try{const data=await conn.client.post("/desktop/preferences",{revision:snapshot.revision,values:submitted},{signal:conn.controller.signal});if(generation!==epoch||conn!==context().conn)return;adopt(data);message="设置已保存。模型和执行模式用于新会话，已有任务保持当前配置。";}catch(e){if(generation===epoch)error=e.status===409?"设置已在其他窗口修改。你的输入已保留，请重新读取并核对后保存。":`保存未确认：${e.message}。输入已保留。`;}finally{if(generation===epoch){saving=false;paint();}}}
  function row(label,control,description=""){return `<div class="setting-row${control?"":" setting-row-information"}"><div class="setting-label"><strong>${h(label)}</strong>${description?`<p>${h(description)}</p>`:""}</div>${control}</div>`;}
  function select(key,label,rows,value,description=""){return row(label,`<select data-setting="${key}" aria-label="${h(label)}" ${saving?"disabled":""}>${options(rows,value)}</select>`,description);}
  function toggle(key,label,value,description=""){return row(label,`<input class="setting-switch" type="checkbox" role="switch" data-setting="${key}" aria-label="${h(label)}" ${value?"checked":""} ${saving?"disabled":""}>`,description);}
  function link(label,target,description=""){return row(label,`<button type="button" class="settings-destination" data-settings-navigate="${target}" aria-label="打开${h(label)}">${icon(DESTINATION_ICONS[target]||"arrowRight")}<span>打开</span></button>`,description);}
  function body(){const v=draft??{ui:DEFAULT_UI,notifications:{}},ui=v.ui,conn=context().conn;switch(section){
    case "general":return select("ui.send_key","发送按键",[["mod-enter","⌘ / Ctrl + Enter"],["enter","Enter（Shift / Alt + Enter 换行）"]],ui.send_key)+select("ui.session_sort","会话排序",[["updated","最近更新"],["created","最近创建"],["title","标题"]],ui.session_sort)+link("本机服务连接","connection","查看当前工作目录与服务状态")+row("窗口与退出","", context().desktop ? "同一工作台的多个窗口共用服务。关闭此工作台的最后一个窗口会停止其服务与任务；⌘Q 退出全部工作台。" : "关闭此页面不会停止服务中的任务。");
    case "appearance":return select("ui.theme","主题",[["system","跟随系统"],["light","浅色"],["dark","深色"]],ui.theme)+row("界面字号",`<input data-setting="ui.font_size" type="number" min="12" max="20" value="${ui.font_size}" aria-label="界面字号">`)+toggle("ui.reduced_motion","减少动画",ui.reduced_motion)+toggle("ui.show_planned","显示规划中的入口",ui.show_planned);
    case "models":return select("default_model","新会话模型",[["","跟随服务"],...(snapshot?.options?.models||[]).map(m=>[m.id,m.id])],v.default_model??"")+select("default_mode","新会话执行模式",[["","跟随服务"],...(snapshot?.options?.modes||[]).map(m=>[m.id,executionModePresentation(m).label])],v.default_mode??"")+row("当前有效默认值",`<dl><dt>模型</dt><dd>${h(snapshot?.effective?.model??"未知")}</dd><dt>来源</dt><dd>${h(snapshot?.effective?.model_source??"未知")}</dd><dt>执行模式</dt><dd>${h(executionModePresentation(snapshot?.effective?.mode).label)}</dd></dl>`,"默认设置不改变正在执行的会话；一次性审批不会被保存为默认授权。")+(snapshot?.options?.modes||[]).map(m=>row(executionModePresentation(m).label,"",m.description)).join("");
    case "notifications":return row("系统通知",`<button type="button" data-notification-permission>检查并允许系统通知</button>`,notificationMessage||"系统未授权时保留应用内状态提示；仅点击此按钮才申请权限。")+Object.entries({completed:"任务完成",failed:"任务失败",approval:"等待审批",question:"需要回答",automation:"自动化运行"}).map(([k,label])=>toggle(`notifications.${k}`,label,v.notifications?.[k]??true)).join("")+row("日程时区",`<input data-setting="timezone" value="${h(v.timezone||"UTC")}" placeholder="Asia/Tokyo" aria-label="日程时区">`,"使用 IANA 时区名称；已有日程保留自身的时区设置。");
    case "data":return link("项目与资料","project")+link("作品与导出","library")+link("Xiaoyu CLI 会话","cli")+link("已归档会话","archived")+row("清理范围","","归档不会删除文件或停止任务。项目文件与会话记录分别管理。");
    case "keyboard":return row("发送",`<kbd>${ui.send_key==="enter"?"Enter":"⌘ / Ctrl + Enter"}</kbd>`,"发送按键在通用设置中调整；Shift + Enter 或 Alt + Enter 换行。")+row("关闭菜单","<kbd>Escape</kbd>");
    case "workspace":return link("工作区与 shell 选择","panes","工作区支持网页、文件、页面和终端；新终端可选择 zsh 或 bash。")+row("网页隔离","","每个浏览容器使用非持久浏览数据，不获得应用管理接口。退出后不保留登录状态，暂不接收下载；模型读取和操作需要对应工具能力。")+row("终端进程","","关闭标签可保留进程或明确停止；应用退出时停止全部所属终端。用户键入与模型命令分别管理。")+row("面板宽度",`<input type="number" data-setting="ui.panel_width" min="280" max="1000" value="${ui.panel_width}" aria-label="面板宽度">`);
    case "usage":return "";
    case "diagnostics":return row("羽案",`<span>${h(version)}</span>`)+row("服务连接",`<span>${conn?.healthy?"已连接":"未连接"}</span>`)+row("工作目录",`<code>${h(conn?.health?.root||"尚未连接")}</code>`)+row("服务版本",`<span>${h(conn?.health?.version||"服务未提供")}</span>`)+row("诊断摘要",`<button type="button" data-settings-export>导出</button>`,"仅包含版本、能力状态和错误摘要，不含令牌或对话正文。")+row("应用更新","<span>规划中 · 0.13.0</span>","当前使用本机开发构建。");
    default:return "";}}
  function html(){
    const matches=matchingSections(query),current=SETTINGS_SECTIONS.find(r=>r[0]===section);
    const groups=[["偏好设置",["general","appearance","models","notifications"]],["工作空间",["data","integrations","keyboard","workspace"]],["应用",["usage","diagnostics"]]];
    const navigation=groups.map(([label,ids])=>{
      const items=matches.filter(([id])=>ids.includes(id));
      if(!items.length)return "";
      return `<div class="settings-nav-group"><p class="settings-nav-label">${h(label)}</p>${items.map(([id,label,desc])=>`<button type="button" ${SECTION_DESTINATIONS[id]?`data-settings-navigate="${SECTION_DESTINATIONS[id]}"`:`data-settings-section="${id}"`} aria-current="${section===id?"page":"false"}" title="${h(desc)}">${icon(SECTION_ICONS[id])}<span>${h(label)}</span></button>`).join("")}</div>`;
    }).join("");
    return `<section class="settings-layout"><nav class="settings-nav" aria-label="设置分类"><label class="settings-section-picker"><span class="sr-only">设置分类</span><select id="settings-section-select" aria-label="设置分类">${options(SETTINGS_SECTIONS.map(([id,label])=>[id,label]),section)}</select></label><input id="settings-search" type="search" value="${h(query)}" placeholder="搜索设置…" aria-label="搜索设置">${navigation}${matches.length?"":"<p class=note>没有匹配的设置。</p>"}</nav><div class="settings-content"><header class="settings-page-heading"><h2>${h(current?.[1]||"设置")}</h2><p>${h(current?.[2]||"")}</p></header>${loading?'<p class="settings-status" role=status>正在读取设置…</p>':""}${error?`<p class="form-error" role="alert">${h(error)}</p>`:""}${message?`<p class="note settings-status" role="status">${h(message)}</p>`:""}${snapshot?.warning?`<p class="note settings-status">${h(snapshot.warning)}</p>`:""}<form id="preferences-form"><fieldset class="preferences-fields" ${saving?"disabled":""}>${body()}<div class="settings-save" ${dirty||["general","appearance","models","notifications","workspace"].includes(section)?"":"hidden"}><div class="settings-save-actions"><button type="submit" class="primary" ${!snapshot||saving||!dirty||!context().writable?"disabled":""}>${saving?"保存中…":"保存更改"}</button><button type="button" data-settings-reload ${saving?"disabled":""}>${dirty?"放弃未保存更改并重新读取":"重新读取"}</button><button type="button" data-settings-reset ${section==="appearance"?"":"hidden"} ${!snapshot||saving?"disabled":""}>恢复外观默认值</button></div>${dirty?'<span class="settings-unsaved" role="status">有尚未保存的更改</span>':""}</div></fieldset></form>${extra(section)}</div></section>`;
  }
  function input(e){const n=e.target;if(n.id==="settings-section-select"){if(SECTION_DESTINATIONS[n.value]){const target=SECTION_DESTINATIONS[n.value];n.value=section;navigate(target);}else if(SETTINGS_SECTIONS.some(([id])=>id===n.value)){section=n.value;query="";paint();}return true;}if(saving)return !!n.dataset.setting;if(n.id==="settings-search"){query=n.value;const matches=matchingSections(query).filter(([id])=>!SECTION_DESTINATIONS[id]);if(matches.length&&!matches.some(row=>row[0]===section))section=matches[0][0];paint();return true;}if(!draft)return false;if(n.dataset.setting){const parts=n.dataset.setting.split("."),target=parts.length===2?draft[parts[0]]:draft;target[parts.at(-1)]=n.type==="checkbox"?n.checked:n.type==="number"?Number(n.value):n.value;dirty=true;message="";paint();return true;}if(n.dataset.navHidden){draft.ui.hidden_nav=draft.ui.hidden_nav.filter(k=>k!==n.dataset.navHidden);if(!n.checked)draft.ui.hidden_nav.push(n.dataset.navHidden);dirty=true;paint();return true;}return false;}
  function click(e){const n=e.target.closest("button");if(!n)return false;if(n.dataset.settingsSection){section=n.dataset.settingsSection;paint();return true;}if(n.dataset.settingsNavigate){navigate(n.dataset.settingsNavigate);return true;}if(n.hasAttribute("data-notification-permission")){Promise.resolve(requestNotifications?.()).then(result=>{notificationMessage=result?.enabled?"系统通知已允许。":"系统通知未获允许，可在系统设置中调整。";paint();}).catch(e=>{notificationMessage=e.message;paint();});return true;}if(n.hasAttribute("data-settings-reload")){dirty=false;load();return true;}if(n.hasAttribute("data-settings-reset")&&draft){draft.ui=structuredClone(DEFAULT_UI);dirty=true;paint();return true;}if(n.dataset.navMove&&draft){const list=draft.ui.nav_order,i=list.indexOf(n.dataset.navMove),j=i+Number(n.dataset.direction);if(i>=0&&j>=0&&j<list.length){[list[i],list[j]]=[list[j],list[i]];dirty=true;paint();}return true;}if(n.hasAttribute("data-settings-export")){exportFile?.({filename:"羽案诊断.json",mime_type:"application/json",text:JSON.stringify({app_version:version,connected:!!context().conn?.healthy,service_version:context().conn?.health?.version??null,persist:!!context().conn?.health?.persist,error},null,2)});return true;}return false;}
  function saveDefaultModel(model) {
    const generation=epoch, conn=context().conn;
    const operation=async()=>{
      if(generation!==epoch||conn!==context().conn)throw new Error("连接已变化，请重新选择模型。");
      if(!draft||!context().writable)throw new Error("设置尚未就绪。");
      if(saving||dirty)throw new Error("设置有未保存的更改，请先在设置页处理。");
      draft.default_model=model;dirty=true;await save();
      if(generation!==epoch||conn!==context().conn)throw new Error("连接已变化，请重新核对模型。");
      if(dirty)throw new Error(error||"设置未保存");
    };
    const result=uiWrites.then(operation);uiWrites=result.catch(()=>{});return result;
  }
  return {saveDefaultModel,load,render:html,input,click,onSubmit(e){if(e.target.id!=="preferences-form")return false;e.preventDefault();save();return true;},values:()=>snapshot?.values??{ui:DEFAULT_UI},snapshot:()=>snapshot,reset(){epoch++;snapshot=draft=null;loading=saving=dirty=false;error=message="";},saveUI(patch){const operation=async()=>{if(!draft)return;if(saving||dirty)throw new Error("设置有尚未保存的更改，请先在设置页处理后再保存布局或分组。");draft.ui={...draft.ui,...patch};dirty=true;await save();if(dirty)throw new Error(error||"设置未保存");};const result=uiWrites.then(operation);uiWrites=result.catch(()=>{});return result;}};
}
