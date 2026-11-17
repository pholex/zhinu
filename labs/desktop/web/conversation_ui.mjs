import {escapeHTML as h,renderText,historyConversation} from "./client.mjs";
import {icon} from "./ui.mjs";

/** Presentation only. Submission and stopping remain owned by the session. */
export function composerControls({busy=false,hasText=false,pending=false,stopPending=false,directCollaboration=false}={}) {
  return {
    sendHidden:busy && (!hasText || directCollaboration),
    sendLabel:pending?"正在提交":busy?"发送补充":"发送",
    sendSymbol:pending?"loading":"arrow",
    stopHidden:!busy,
    stopLabel:stopPending?"正在停止":"停止生成",
    stopSymbol:stopPending?"loading":"stop",
  };
}

export function conversationMessage(message,key,{copyable=true}={}) {
  const user=message.kind==="user",text=message.text||"";
  const body=user?h(text):renderText(text);
  return `<section class="message ${user?"user":"assistant"}" data-message-id="${h(key)}"><div class="message-body">${body}</div>${message.clipped?'<p class="note">这一段正文被服务截断。</p>':""}${!user && text && copyable?`<div class="message-actions"><button type="button" class="quiet icon-button" data-copy-message="${h(key)}" title="复制回答" aria-label="复制回答">${icon("copy")}</button></div>`:""}</section>`;
}

export function composerEnterAction(event,sendKey="enter") {
  if(event.key!=="Enter" || event.isComposing || event.keyCode===229)return null;
  if(event.shiftKey || event.altKey)return "newline";
  return event.metaKey || event.ctrlKey || sendKey==="enter" ? "send" : "newline";
}

export function historyTimeline(messages) {
  return messages.map(m=>m.role==='tool'?{...m,kind:'tool',text:m.content||'',status:m.ok===false?'failed':'completed'}:m.role==='model'?{...m,kind:'model'}:{...m,...historyConversation([m])[0]});
}

export function liveTimeline(events) {
  const blocks=[];let reply=null,tool=null;
  for(const e of events){
    if(e.kind==='run.started'||e.kind==='steer.accepted'){
      reply=null;tool=null;
      if(e.kind==='run.started'&&e.previous_model&&e.model&&e.previous_model!==e.model)blocks.push({kind:'model',id:`model-${e.seq}`,from_model:e.previous_model,model:e.model});
      blocks.push({kind:'user',id:`live-${e.seq}`,text:e.prompt??e.text??'',clipped:e.prompt_truncated||e.text_truncated});
    }else if(e.kind==='text.delta'){
      if(!reply){reply={kind:'assistant',id:`live-${e.seq}`,text:''};blocks.push(reply);}
      reply.text+=e.text||'';reply.clipped ||= e.text_truncated;
    }else if(['tool.pending','tool.running','tool.completed','tool.denied','tool.progress'].includes(e.kind)){
      reply=null;
      if(!tool || tool.name!==e.name || ['completed','failed','denied'].includes(tool.status)){
        tool={kind:'tool',id:`tool-${e.seq}`,name:e.name,args:{},text:'',status:'pending'};blocks.push(tool);
      }
      if(e.args)tool.args=e.args;
      if(e.kind==='tool.running')tool.status='running';
      if(e.kind==='tool.progress')tool.progress=e.message||'';
      if(e.kind==='tool.completed'){tool.status=e.ok===false?'failed':'completed';tool.text=e.output||'';tool.clipped=e.output_truncated;tool.seconds=e.seconds;}
      if(e.kind==='tool.denied')tool.status='denied';
    }else if(e.kind==='text.end'||e.kind.startsWith('run.'))reply=null;
  }
  return blocks;
}

export function activityHTML(block) {
  if(block.kind==='model')return `<div class="model-change" role="note">${icon('cpu')}<span>模型已从 ${h(block.from_model)} 切换为 ${h(block.model)}</span></div>`;
  const labels={pending:'等待执行',running:'正在执行',completed:'',failed:'执行失败',denied:'未获批准'};
  let output=block.text||'',language='文本';
  try {if(output.trim()){output=JSON.stringify(JSON.parse(output),null,2);language='JSON';}} catch {}
  return `<details class="tool-activity ${h(block.status)}" data-key="activity-${h(block.id)}"><summary>${icon(block.name==='get_runtime_status'?'info':'terminal')}<span>${h(block.name==='get_runtime_status'?'查看运行状态':block.name||'工具调用')}</span><span class="tool-status">${h(labels[block.status]||'')}</span>${icon('chevron')}</summary><div class="tool-content">${block.args&&Object.keys(block.args).length?`<details><summary>调用参数</summary><pre>${h(JSON.stringify(block.args,null,2))}</pre></details>`:''}${output?`<div class="tool-output"><span>${language}</span><pre>${h(output)}</pre></div>`:`<p class="note">${h(block.progress||labels[block.status]||'没有返回文本')}</p>`}${block.clipped?'<p class="note">结果已截断。</p>':''}</div></details>`;
}

export function timelineHTML(blocks,renderMessage,{runs=[],busy=false}={}) {
  let html='',turn=[];
  function flush(){
    if(!turn.length)return;
    const tools=turn.filter(b=>b.kind==='tool');
    if(!tools.length){html+=turn.map(b=>renderMessage(b,String(b.id))).join('');turn=[];return;}
    const first=turn[0],last=turn.at(-1),final=last.kind==='assistant'?last:null;
    const run=runs.find(r=>r.id===first.run_id),seconds=run?.finished_at>run?.started_at?Math.round(run.finished_at-run.started_at):null;
    const active=busy && !run?.finished_at;
    const summary=tools.some(t=>t.status==='failed'||t.status==='denied')?'部分工具未完成，请展开查看':active?'正在处理':seconds===null?`使用了 ${tools.length} 个工具`:`用时 ${Math.max(1,seconds)} 秒`;
    html+=`<details class="turn-activity" data-key="turn-${h(first.run_id||first.id)}" ${active?'open':''}><summary>${h(summary)}${icon('chevron')}</summary><div>${turn.filter(b=>b!==final).map(b=>b.kind==='tool'?activityHTML(b):renderMessage(b,String(b.id))).join('')}</div></details>`;
    if(final)html+=renderMessage(final,String(final.id));turn=[];
  }
  for(const b of blocks){
    if(b.kind==='user'||b.kind==='model'){flush();html+=b.kind==='model'?activityHTML(b):renderMessage(b,String(b.id));}
    else turn.push(b);
  }
  flush();return html;
}

// 正常发送由输入框反馈；待恢复、失败与结果不确定的回执仍需用户处理。
export function showSubmissionReceipt(action,{owned=true}={}) {
  const conversationAction=['prompt','steer'].includes(action.kind)
    || action.kind==='create'&&!action.workflow&&!action.body?.fork_from;
  return !(owned&&conversationAction&&['sending','accepted'].includes(action.state));
}
