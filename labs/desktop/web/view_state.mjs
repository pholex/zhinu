import {LocalState,Outbox} from "./client.mjs";
// View preferences and drafts never contain executable restoration requests.
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const pages = new Set(['workbench','project','cli','settings','connections','automations','library','spaces','pages','search','content-project','capabilities','archived']);
export const validViewID = value => typeof value === 'string' && uuid.test(value);
export function viewState(session_id, page) {
  return {session_id:typeof session_id === 'string' && /^[A-Za-z0-9_-]{1,200}$/.test(session_id) ? session_id : '',page:pages.has(page) ? page : 'workbench'};
}
export function actionBelongsToView(action, viewID) {
  return !action?.view_id || !validViewID(viewID) || action.view_id.toLowerCase() === viewID.toLowerCase();
}
// Explicitly named side conversations and receipt indexes remain workspace-owned.
const sharedDraft = key => /^(side-conversation:|fleet-receipts:|weave-receipts:)/.test(key);
// Claim legacy UI state once using the server's compare-and-swap revision.
// Losing windows reload rather than merge a competing ownership decision.
export async function claimLegacyDrafts(state, viewID) {
  if (!state || !validViewID(viewID)) return state;
  const owner=viewID.toLowerCase(), marker='view:legacy-owner', prefix=`view:${owner}:`;
  for(let attempt=0;attempt<6;attempt++) {
    if(Object.hasOwn(state.drafts,marker)) {
      if(!validViewID(state.drafts[marker]))throw new Error('旧版草稿的迁移归属不可识别，原始数据未覆盖。');
      return state;
    }
    const drafts=structuredClone(state.drafts),outbox=structuredClone(state.outbox);
    for(const [key,text] of Object.entries(drafts)) {
      if(key.startsWith('view:')||sharedDraft(key))continue;
      const target=prefix+key;
      if(Object.hasOwn(drafts,target)&&drafts[target]!==text)throw new Error('旧版草稿与当前窗口内容冲突，原始数据未覆盖。');
      Object.defineProperty(drafts,target,{value:text,enumerable:true,writable:true,configurable:true});
      delete drafts[key];
    }
    for(const [scope,item] of Object.entries(outbox))if(!scope.startsWith('side:')&&!item.view_id)item.view_id=owner;
    drafts[marker]=owner;
    try {
      const result=await state.client.post('/desktop/state',{revision:state.revision,drafts,outbox},{signal:state.signal});
      if(!Number.isInteger(result.revision)||result.revision<=state.revision)throw new Error('旧版草稿保存版本异常，请重新打开核对。');
      return new LocalState(state.client,{revision:result.revision,drafts,outbox},state.signal);
    } catch(error) {
      if(error.status!==409||attempt===5)throw error;
      state=await LocalState.load(state.client,state.signal);
    }
  }
}
// Pull other views' receipt changes only between sends. This never dispatches requests.
export async function syncViewReceipts(state, outbox) {
  if(!state||!outbox||outbox.running.size)return null;
  await state.flush();
  const generation=state.generation;
  const remote=await state.client.get('/desktop/state',state.signal);
  if(outbox.running.size||state.generation!==generation)return null;
  state.mergeRemote(remote);
  return new Outbox(state.storage,'native');
}
export function scopeViewDrafts(state, viewID) {
  if (!state || !validViewID(viewID)) return state;
  const prefix = `view:${viewID.toLowerCase()}:`;
  const physical = key => sharedDraft(key) ? key : prefix + key;
  const visible = key => key.startsWith(prefix) ? key.slice(prefix.length) : sharedDraft(key) ? key : null;
  return new Proxy(state, {
    get(target, key) {
      if (key === 'getDraft') return name => target.getDraft(physical(name));
      if (key === 'setDraft') return (name,text) => target.setDraft(physical(name),text);
      if (key === 'deleteDraft') return name => target.deleteDraft(physical(name));
      if (key === 'drafts') return Object.fromEntries(Object.entries(target.drafts).flatMap(([name,text]) => visible(name) === null ? [] : [[visible(name),text]]));
      if (key === 'conflicts') return target.conflicts.map(c => c.group === 'drafts' && visible(c.key) !== null ? {...c,key:visible(c.key)} : c);
      if (key === 'resolveConflict') return (group,name,choice) => target.resolveConflict(group,group === 'drafts' ? physical(name) : name,choice);
      const value = Reflect.get(target,key);
      return typeof value === 'function' ? value.bind(target) : value;
    },
    set(target,key,value) { return Reflect.set(target,key,value); },
  });
}
export function workStatus(sessions, healthy) {
  if (!healthy || !Array.isArray(sessions)) return null;
  return {running:sessions.filter(s => s.busy).length,pending:sessions.reduce((count,s) => count + (s.pending_approvals?.length || 0) + (s.questionItems || []).filter(q => ['open','pending'].includes(q.state)).length,0)};
}

// Each service owns one workspace; paths also separate project folders within it.
// The surrounding view scope prevents two windows from overwriting each other's input.
export function newConversationInput(state, workspace, text) {
  if (!state || !workspace) return "";
  const key="new-conversation-inputs", raw=state.getDraft(key);
  const entries=raw ? JSON.parse(raw) : {};
  if (!entries || Array.isArray(entries) || typeof entries!=="object" || Object.values(entries).some(v=>typeof v!=="string")) throw new Error("输入缓存格式不正确，原内容已保留。");
  if (text!==undefined) {
    if (text) Object.defineProperty(entries,workspace,{value:text,enumerable:true,configurable:true,writable:true});
    else delete entries[workspace];
    state.setDraft(key,Object.keys(entries).length?JSON.stringify(entries):"");
  }
  return Object.hasOwn(entries,workspace)?entries[workspace]:"";
}
