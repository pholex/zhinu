/** Desktop wording only; available modes and authorization remain service-owned. */
const PRESENTATION = {
  default: {label:"请求批准", description:"修改文件、执行命令前询问，已授权操作除外", symbol:"approval"},
  auto: {label:"自动执行", description:"自动处理允许的操作，需要额外权限时询问", symbol:"shield"},
  plan: {label:"先做计划", description:"先调研并给出计划，经你批准后再执行", symbol:"document"},
};
export function executionModePresentation(mode) {
  const id=typeof mode==="string"?mode:mode?.id;
  return PRESENTATION[id] || {label:mode?.label||"权限待确认",description:mode?.description||"请查看当前服务的权限设置",symbol:"settings"};
}
export function executionPermissionChoices(modes=[],effective="") {
  const active=modes.find(mode=>mode.id===effective);
  const current=active?executionModePresentation(active):null;
  return [...modes.map(mode=>({id:mode.id,...executionModePresentation(mode)})),
    {id:"",label:"使用默认设置",description:current?`当前：${current.label}`:"按当前默认设置执行",
      caption:current?.label||"使用默认设置",symbol:"settings",triggerSymbol:current?.symbol||"settings",inherited:true}];
}
