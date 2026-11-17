// GUI organization never changes execution ownership or history.
export function orderedSessions(sessions,ui={},meta=()=>({}),title=s=>s.title||s.session_id){
  return [...sessions].sort((a,b)=>Number(!!meta(b.session_id).pinned)-Number(!!meta(a.session_id).pinned)||(ui.session_sort==='title'?title(a).localeCompare(title(b),'zh-CN'):Number(b[ui.session_sort==='created'?'created_at':'updated_at']||0)-Number(a[ui.session_sort==='created'?'created_at':'updated_at']||0))||a.session_id.localeCompare(b.session_id));
}
export function sessionUnread(session,ui={}){return Number(session.turns)>0&&Number(session.next_seq||0)>Number(ui.session_meta?.[session.session_id]?.read_seq||0);}
export function readMetadata(session,ui={}){const values={...(ui.session_meta||{})},old=values[session.session_id];if(old?.read_seq>=session.next_seq)return null;if(!old&&Object.keys(values).length>=500)return null;values[session.session_id]={group:old?.group||'',read_seq:Math.max(0,Number(session.next_seq)||0)};return values;}

// An allocated session or a recovered empty shell is not conversation activity.
export function hasRecentActivity(session={}){
  return Number(session.turns)>0 || session.busy===true || ['running','error'].includes(session.status)
    || !!(session.in_flight?.turn || session.lost_turn || session.last_result || session.fork_origin || session.history_warning)
    || !!session.pending_approvals?.length || !!session.pending_notifications?.length
    || !!session.questionItems?.some(q=>['open','pending','queued'].includes(q.state));
}
