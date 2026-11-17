import test from 'node:test';import assert from 'node:assert/strict';
import {orderedSessions,sessionUnread,readMetadata,hasRecentActivity} from '../web/organization.mjs';
test('organization preserves pinned priority and deterministic sorting without mutating service records',()=>{const rows=[{session_id:'a',title:'Z',created_at:3,updated_at:1},{session_id:'b',title:'A',created_at:2,updated_at:4}];assert.equal(orderedSessions(rows,{session_sort:'title'})[0].session_id,'b');assert.equal(orderedSessions(rows,{session_sort:'created'})[0].session_id,'a');assert.equal(orderedSessions(rows,{},sid=>({pinned:sid==='a'}))[0].session_id,'a');assert.equal(rows[0].session_id,'a');});
test('read watermark follows service events and preserves user group',()=>{const s={session_id:'a',turns:1,next_seq:9},ui={session_meta:{a:{group:'报告',read_seq:4}}};assert.equal(sessionUnread(s,ui),true);const metadata=readMetadata(s,ui);assert.deepEqual(metadata.a,{group:'报告',read_seq:9});assert.equal(sessionUnread(s,{session_meta:metadata}),false);assert.equal(readMetadata(s,{session_meta:metadata}),null);assert.equal(ui.session_meta.a.read_seq,4);});

test('recent excludes never-started and recovered empty sessions without relying on their title',()=>{
  for(const detail of ['idle','recovered',''])assert.equal(hasRecentActivity({session_id:'empty',title:'新任务',turns:0,status:'idle',detail,next_seq:12,updated_at:100}),false);
  assert.equal(hasRecentActivity({title:'已经改名的草稿',turns:0,status:'idle'}),false);
  assert.equal(hasRecentActivity({title:'新任务',turns:1,status:'idle'}),true);
});
test('first active turn, interrupted work, inherited history and actionable states remain recent',()=>{
  for(const state of [{busy:true},{status:'running'},{status:'error'},{history_warning:'receipt unavailable'},{in_flight:{turn:1}},{lost_turn:{turn:1}},{last_result:{stopped:'done'}},{fork_origin:'prior'},{pending_approvals:[{}]},{pending_notifications:[{wake:true}]},{questionItems:[{state:'open'}]}])assert.equal(hasRecentActivity({turns:0,...state}),true,JSON.stringify(state));
});
