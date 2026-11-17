import test from 'node:test';import assert from 'node:assert/strict';
import {terminalRows,mergeRecent,terminalLabel,isContinued,takeover} from '../web/terminal_sessions.mjs';

test('one list: terminal rows interleave by time and a continued journal is represented by its session only',()=>{
  const sessions=[{session_id:'s1',updated_at:50},{session_id:'s2',updated_at:10,fork_origin:{kind:'cli_adopted'}}];
  const items=[{catalog_id:'cli-a',title:'旧任务',updated_at:30,session_id:''},{catalog_id:'cli-b',title:'已继续',updated_at:99,session_id:'s2'},{catalog_id:'cli-c',title:'别处的',updated_at:40,session_id:'gone'}];
  const rows=terminalRows(items,sessions);
  assert.deepEqual(rows.map(r=>r.catalog_id),['cli-c','cli-a']);
  assert.deepEqual(mergeRecent(sessions,rows).map(e=>`${e.kind}:${e.id}`),['session:s1','terminal:cli-c','terminal:cli-a','session:s2']);
  assert.deepEqual(terminalRows(items,sessions,'旧').map(r=>r.catalog_id),['cli-a']);
  assert.equal(isContinued(sessions[1]),true);assert.equal(isContinued(sessions[0]),false);
});

test('labels name the source and who holds it',()=>{
  assert.equal(terminalLabel({}),'终端');
  assert.equal(terminalLabel({locked:true}),'终端 · 使用中');
  assert.equal(terminalLabel({session_id:'s'}),'终端 · 已在此继续');
});

test('a held journal stays readable, offers retry, and forks only from a settled boundary',()=>{
  const base={state:'ready',revision:'r',locked:true,canContinue:false};
  const resting=takeover({...base,canFork:true});
  assert.equal(resting.kind,'conflict');assert.equal(resting.title,'已在终端中打开');
  assert.deepEqual([resting.canContinue,resting.canRetry,resting.canFork],[false,true,true]);
  const midTurn=takeover({...base,canFork:false,forkReason:'来源会话正在执行一轮'});
  assert.deepEqual([midTurn.canFork,midTurn.note],[false,'来源会话正在执行一轮']);
  assert.equal(takeover({...base,state:'loading',canFork:true}).canRetry,false,'no double retry while one is in flight');
});

test('a free, cleanly closed journal can be continued in place; pending or read-only windows cannot act',()=>{
  const view={state:'ready',revision:'r',locked:false,canContinue:true,canFork:true};
  assert.deepEqual((({kind,canContinue,canFork})=>({kind,canContinue,canFork}))(takeover(view)),{kind:'ready',canContinue:true,canFork:true});
  for(const options of [{writable:false},{pending:true}]){const t=takeover(view,options);assert.deepEqual([t.canContinue,t.canFork],[false,false]);}
});

test('unsafe, loading and already-continued journals never offer a write',()=>{
  const blocked=takeover({state:'ready',revision:'r',locked:false,canContinue:false,canFork:false,continueReason:'来源没有正常结束标记'});
  assert.deepEqual([blocked.kind,blocked.detail,blocked.canContinue,blocked.canFork],['blocked','来源没有正常结束标记',false,false]);
  assert.equal(takeover(undefined).kind,'loading');assert.equal(takeover({state:'loading'}).kind,'loading');
  const held=takeover({state:'ready',revision:'r',sessionId:'s9',locked:false});
  assert.deepEqual([held.kind,held.sessionId,held.canContinue,held.canFork],['held','s9',false,false]);
});

test('a journal from another workspace is readable but only offers a jump to its own workbench',()=>{
  const view={state:'ready',revision:'r',inScope:false,workspace:'/work/other',canContinue:false,canFork:false,locked:false};
  const web=takeover(view),app=takeover(view,{native:true});
  assert.deepEqual([web.kind,web.detail,web.canOpen,web.canContinue,web.canFork],['elsewhere','/work/other',false,false,false]);
  assert.equal(app.canOpen,true);assert.equal(app.note,'');
});
