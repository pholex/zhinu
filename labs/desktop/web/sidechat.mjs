import {escapeHTML as h,renderText,historyConversation,stateOf} from "./client.mjs";

const validId=value=>typeof value==='string'&&/^[A-Za-z0-9_-]{1,100}$/.test(value);
const prefix='side-conversation:';
function savedDraft(raw){const value=JSON.parse(raw);if(!value||typeof value!=='object'||Array.isArray(value)||typeof value.sid!=='string'||value.sid&&!validId(value.sid)||typeof value.text!=='string'||value.text.length>60000)throw new Error('侧边会话草稿无法读取，原数据保留。');return value;}
// IDs survive tab closure and process restart. Listing never sends or retries a task.
export function sideConversationEntries(localState){
  const ids=new Set(Object.keys(localState?.drafts||{}).filter(k=>k.startsWith(prefix)).map(k=>k.slice(prefix.length)));
  for(const key of Object.keys(localState?.outbox||{})){const match=key.match(/^side:([A-Za-z0-9_-]{1,100}):(create|prompt|abort)$/);if(match)ids.add(match[1]);}
  return [...ids].filter(validId).map(id=>{let saved={sid:'',text:''},invalid=false;try{const raw=localState.getDraft(prefix+id);if(raw)saved=savedDraft(raw);}catch{invalid=true;}
    const actions=Object.entries(localState.outbox||{}).filter(([scope])=>scope.startsWith(`side:${id}:`)).map(([,item])=>item);
    return{id,sid:saved.sid||actions.find(a=>a.kind==='create'&&a.state==='accepted')?.result?.session_id||'',text_preview:saved.text.slice(0,120),pending:!!saved.pending||actions.length>0,invalid};});
}
export async function sideHistory(client,sid,signal){
  const base=`/session/${encodeURIComponent(sid)}/history`,first=await client.get(`${base}?cursor=0&limit=100`,signal);
  if(!Array.isArray(first.messages)||!Number.isInteger(first.total))throw new Error('历史响应不完整，已保留先前内容。');
  // Keep the latest bounded window, with an explicit note rather than silently showing only the first page.
  const start=Math.max(0,first.total-2000),messages=start?[]:[...first.messages],warnings=new Set(first.warning?[first.warning]:[]);
  let cursor=start||first.next_cursor,previous=-1,pages=0;
  while(cursor!==null&&cursor!==undefined){if(!Number.isInteger(cursor)||cursor<=previous||++pages>25)throw new Error('历史分页游标异常，已保留先前内容。');previous=cursor;
    const page=await client.get(`${base}?cursor=${cursor}&limit=100`,signal);if(!Array.isArray(page.messages))throw new Error('历史分页响应不完整。');messages.push(...page.messages);if(page.warning)warnings.add(page.warning);cursor=page.next_cursor;}
  const seen=new Set();return{messages:messages.filter(m=>{if(typeof m.id!=='string'||seen.has(m.id))return false;seen.add(m.id);return true;}),warning:[start?'侧边视图仅显示最近 2000 条历史，请打开完整会话查阅更早内容。':'',...warnings].filter(Boolean).join(' ')};
}

// Separate session, shared durable Outbox. Main controllers must leave side:* scopes to this controller.
export function createSideConversation({context,openMain,id=crypto.randomUUID()}) {
  if(!validId(id))throw new Error('侧边会话标识无效。');
  const node=document.createElement('section');node.className='side-conversation';const key=prefix+id,scope=kind=>`side:${id}:${kind}`;
  let bound=context().conn,sid='',text='',error='',notice='',status=null,questions=[],questionError='',history=[],historyWarning='',busy=false,reading=false,disposed=false,timer=null,hydrated=false,corrupt=false,lastRaw='';
  function connection(){const conn=context().conn;if(!bound&&conn)bound=conn;if(!conn||conn!==bound)throw new Error('连接已变化，请从恢复列表重新打开侧边会话。');if(!conn.localState||!conn.outbox)throw new Error('草稿与回执尚未恢复，请等待连接完成。');return conn;}
  function action(kind){return bound?.outbox?.items[scope(kind)];}
  function pending(){return ['create','prompt'].map(action).find(Boolean);}
  function hydrate(){if(hydrated)return;const conn=connection();lastRaw=conn.localState.getDraft(key);if(lastRaw){try{const saved=savedDraft(lastRaw);sid=saved.sid;text=saved.text;
      if(saved.pending){const old=saved.pending;if(!['create','prompt'].includes(old.kind)||typeof old.key!=='string'||!old.key||!old.body||typeof old.body!=='object'||Array.isArray(old.body)||old.kind==='prompt'&&(!validId(sid)||typeof old.body.text!=='string')||old.kind==='create'&&typeof old.body.workspace!=='string')throw new Error('旧回执格式不正确');const target=scope(old.kind);if(!conn.outbox.items[target]){conn.outbox.items[target]={kind:old.kind,key:old.key,body:old.body,path:old.kind==='create'?'/session':`/session/${sid}/prompt_async`,sid,instance:conn.health.instance_id,state:'unknown'};conn.outbox.save();}}
    }catch{corrupt=true;throw new Error('侧边会话草稿或旧回执无法读取，原数据保留。');}}
    for(const kind of ['prompt','abort']){const item=action(kind);if(item){if(!validId(item.sid)||sid&&sid!==item.sid)throw new Error('侧边会话与待提交回执来源不一致，原数据保留。');sid=item.sid;if(kind==='prompt'&&!text&&typeof item.body?.text==='string')text=item.body.text;}}
    hydrated=true;
  }
  async function persist(){const conn=connection();if(corrupt)throw new Error('原草稿损坏，未覆盖或提交。');if(conn.localState.getDraft(key)!==lastRaw)throw new Error('另一窗口已修改此侧边会话，请从恢复列表重新打开；未覆盖原数据。');lastRaw=JSON.stringify({sid,text});conn.localState.setDraft(key,lastRaw);await conn.localState.flush();}
  async function accepted(){const conn=connection();for(const kind of ['create','prompt','abort']){const item=action(kind);if(item?.state!=='accepted')continue;
      if(kind==='create'){if(!validId(item.result?.session_id))throw new Error('创建回执缺少有效会话标识，原请求保留。');sid=item.result.session_id;}
      if(kind==='prompt'&&item.body?.text===text)text='';
      await persist();conn.outbox.forget(scope(kind));await conn.localState.flush();
    }}
  function draw(){const focus=node.contains(document.activeElement),input=node.querySelector('textarea'),start=input?.selectionStart,end=input?.selectionEnd,item=pending(),blocked=busy||corrupt||!hydrated||context().conn!==bound,unknown=item&&['unknown','ready'].includes(item.state),waiting=questions.some(q=>['open','pending'].includes(q.state)),queued=questions.some(q=>q.state==='queued');
    node.innerHTML=`<p class="note">独立讨论 · 与主会话共用执行服务${sid?` · ${h(sid)}`:''}</p><div class="side-chat-history">${historyConversation(history).map(m=>`<section class="message ${m.kind}">${m.kind==='user'?h(m.text):renderText(m.text)}</section>`).join('')}</div>${historyWarning?`<p class="note">${h(historyWarning)}</p>`:''}${error?`<p class="form-error" role="alert">${h(error)}</p>`:''}${notice?`<p class="note">${h(notice)}</p>`:''}${questionError?`<p class="form-error">问答状态未知：${h(questionError)}</p>`:''}${status?.pending_approvals?.length||waiting?'<p class="note">此会话需要审批或回答，请打开完整会话处理。</p>':''}${queued?'<p class="note">答案已排队，尚未被模型接纳。</p>':''}${item?`<p class="note">待核对请求：${h(item.kind)} · ${h(item.state)}。${h(item.message||'')}</p>`:''}<form><label class="sr-only">侧边会话输入</label><textarea maxlength="60000" rows="4" placeholder="在这里讨论另一个问题…" ${blocked||item?'readonly':''}>${h(text)}</textarea><div class="actions"><button type="submit" ${blocked||(!item&&!text.trim())||(item&&!unknown)||(!item&&status?.busy)?'disabled':''}>${item?'使用原请求核对并重试':'发送'}</button>${item&&['rejected','recorded'].includes(item.state)?'<button type="button" data-side-reconcile>已核对，保留草稿并结束回执</button>':''}<button type="button" data-side-refresh>刷新状态</button>${sid?`<button type="button" data-side-open>打开完整会话</button><button type="button" data-side-stop ${(!status?.in_flight?.turn&&!action('abort'))||blocked?'disabled':''}>${action('abort')?'使用原停止请求核对':'停止'}</button>`:''}</div></form><p class="note">${status?h(stateOf({...status,questionItems:questions,questionError}).label):'讨论内容保存在独立会话中。'}${status?.busy?'；可打开完整会话插话。':''}</p>`;
    if(focus){const next=node.querySelector('textarea');next.focus({preventScroll:true});next.setSelectionRange(start??0,end??0);}}
  async function refresh(){if(disposed||reading||busy)return;reading=true;try{hydrate();await accepted();const conn=connection();if(sid){const target=sid;const results=await Promise.allSettled([conn.client.get(`/session/${sid}/status`,conn.controller.signal),sideHistory(conn.client,sid,conn.controller.signal),conn.client.get(`/session/${sid}/questions`,conn.controller.signal)]);if(disposed||context().conn!==conn||target!==sid)return;
        const [s,r,q]=results;if(s.status==='fulfilled')status=s.value;else throw s.reason;if(r.status==='fulfilled'){history=r.value.messages;historyWarning=r.value.warning;}else error=r.reason.message;
        if(q.status==='fulfilled'&&Array.isArray(q.value.questions)){questions=q.value.questions;questionError='';}else questionError=q.reason?.message||'响应不完整';}
    }catch(e){error=e.message;}finally{reading=false;if(!disposed)draw();}}
  async function dispatch(kind,body){const conn=connection(),target=scope(kind);if(!action(kind)){conn.outbox.prepare(target,{kind,sid,path:kind==='create'?'/session':`/session/${sid}/${kind==='prompt'?'prompt_async':'abort'}`,body,instance:conn.health.instance_id});}const item=action(kind);
    if(item.kind!==kind||item.path!==(kind==='create'?'/session':`/session/${sid}/${kind==='prompt'?'prompt_async':'abort'}`)||kind!=='create'&&item.sid!==sid)throw new Error('待提交回执来源不一致，未执行。');
    if(item.state==='accepted'){await accepted();return null;}if(!['ready','unknown'].includes(item.state))throw new Error('此回执不能重试，请查看完整会话并明确结束回执。');await persist();const result=await conn.outbox.send(target,conn.client,conn.controller.signal);if(result===null)throw new Error('原请求仍在另一处提交，请等待回执。');await accepted();return result;}
  async function send(){if(busy||disposed)return;busy=true;error='';notice='';try{hydrate();await accepted();if(!pending()&&!text.trim())return;draw();
      if(!sid){await dispatch('create',{...(context().projectId?{project_id:context().projectId}:{workspace:context().workspace||connection().health.root}),questions:{foreground_timeout_seconds:60}});}
      if(action('prompt')||text.trim())await dispatch('prompt',{text});
    }catch(e){error=`提交未确认：${e.message}。原文与原请求已保留；不会自动重发。`;}finally{busy=false;draw();await refresh();}}
  async function stop(){if(busy||disposed)return;busy=true;error='';try{hydrate();const old=action('abort');if(!old&&!status?.in_flight?.turn)throw new Error('当前没有可精确停止的轮次。');const result=await dispatch('abort',{turn:status?.in_flight?.turn});notice=result?.aborted===false?'目标轮次已结束或服务实例已变化，未停止其他轮次。':'停止请求已返回，请以最新执行状态为准。';}catch(e){error=`停止回执未确认：${e.message}`;}finally{busy=false;draw();await refresh();}}
  async function reconcile(){const item=pending();if(!item||!['rejected','recorded'].includes(item.state)||busy)return;try{await persist();connection().outbox.forget(scope(item.kind));await connection().localState.flush();notice='已结束该回执，草稿保留；再次发送是新的显式提交。';error='';}catch(e){error=e.message;}draw();}
  node.addEventListener('input',e=>{if(e.target.tagName==='TEXTAREA'&&!pending()&&!busy&&!corrupt){text=e.target.value.slice(0,60000);persist().catch(e=>{error=e.message;draw();});const button=node.querySelector('[type="submit"]');if(button)button.disabled=!text.trim()||!!status?.busy;}});
  node.addEventListener('submit',e=>{e.preventDefault();send();});
  node.addEventListener('click',e=>{if(e.target.closest('[data-side-open]')&&sid)Promise.resolve(openMain(sid)).catch(e=>{error=e.message;draw();});if(e.target.closest('[data-side-stop]'))stop();if(e.target.closest('[data-side-refresh]'))refresh();if(e.target.closest('[data-side-reconcile]'))reconcile();});
  function tick(){if(disposed)return;if(node.isConnected)refresh();timer=setTimeout(tick,3000);}
  const ready=refresh();tick();return{node,id,ready,refresh,send,stop,reconcile,dispose(){disposed=true;clearTimeout(timer);},session:()=>sid};
}
