import test from 'node:test';
import assert from 'node:assert/strict';
import {LocalState,Outbox} from '../web/client.mjs';
import {renderLocalConflicts,resolveLocalConflict} from '../web/state_conflicts.mjs';
function harness(){let disk={revision:0,drafts:{},outbox:{}},lose=false;const sent=[];
 const client={get:async()=>structuredClone(disk),post:async(path,body)=>{if(path!=='/desktop/state'){sent.push(body);return {session_id:'s'};}
  if(body.revision!==disk.revision)throw Object.assign(new Error('CAS'),{status:409});disk={...structuredClone(body),revision:disk.revision+1};if(lose){lose=false;throw new Error('lost');}return {revision:disk.revision};}};
 return {client,sent,disk:()=>disk,lose:()=>{lose=true;},load:()=>LocalState.load(client)};
}
const stop=(...locals)=>locals.forEach(s=>clearTimeout(s.timer));
test('two windows merge unrelated draft keys including deletes without erasing other edits',async()=>{
 const h=harness(),a=await h.load(),b=await h.load();a.setDraft('one','A');await a.flush();b.setDraft('two','B');await b.flush();assert.deepEqual(h.disk().drafts,{one:'A',two:'B'});
 a.deleteDraft('one');await a.flush();assert.deepEqual(h.disk().drafts,{two:'B'});stop(a,b);
});
test('same draft conflict preserves both values and only explicit choice unblocks persistence',async()=>{
 const h=harness(),a=await h.load(),b=await h.load();a.setDraft('s','<remote>');await a.flush();b.setDraft('s','<local>');await assert.rejects(b.flush(),/冲突/);
 assert.equal(b.getDraft('s'),'<local>');assert.equal(h.disk().drafts.s,'<remote>');assert.equal(b.conflicts[0].remote,'<remote>');assert.match(renderLocalConflicts(b),/&lt;remote&gt;/);assert.doesNotMatch(renderLocalConflicts(b),/<remote>/);
 await resolveLocalConflict(b,'drafts','s','local');assert.equal(h.disk().drafts.s,'<local>');assert.equal(b.conflicts.length,0);stop(a,b);
});
test('adopting remote deletion is explicit and does not recreate deleted draft',async()=>{
 const h=harness(),seed=await h.load();seed.setDraft('s','base');await seed.flush();const a=await h.load(),b=await h.load();a.deleteDraft('s');await a.flush();b.setDraft('s','local');await assert.rejects(b.flush());
 const result=await resolveLocalConflict(b,'drafts','s','remote');assert.equal(result.reloadDraft,true);assert.equal(b.getDraft('s'),'');assert.equal(h.disk().drafts.s,undefined);stop(seed,a,b);
});
test('lost state reply is reconciled without a second mutation or losing later text',async()=>{
 const h=harness(),a=await h.load();a.setDraft('s','first');h.lose();await assert.rejects(a.flush(),/lost/);a.setDraft('other','new');await a.flush();assert.deepEqual(h.disk().drafts,{s:'first',other:'new'});assert.equal(h.disk().revision,2);stop(a);
});
test('unrelated receipt merge survives subsequent writes from an older Outbox snapshot',async()=>{
 const h=harness(),a=await h.load(),b=await h.load(),aa=new Outbox(a.storage,'native'),bb=new Outbox(b.storage,'native');
 aa.prepare('A',{kind:'prompt',path:'/session/a/prompt_async',body:{text:'a'}});await a.flush();bb.prepare('B',{kind:'prompt',path:'/session/b/prompt_async',body:{text:'b'}});await b.flush();
 assert.deepEqual(Object.keys(h.disk().outbox).sort(),['A','B']);bb.forget('B');await b.flush();assert.deepEqual(Object.keys(h.disk().outbox),['A']);stop(a,b);
});
test('same submission scope from two windows cannot dispatch a second key',async()=>{
 const h=harness(),a=await h.load(),b=await h.load(),aa=new Outbox(a.storage,'native'),bb=new Outbox(b.storage,'native');
 aa.prepare('s',{kind:'prompt',path:'/session/s/prompt_async',body:{text:'one'}});await a.flush();bb.prepare('s',{kind:'prompt',path:'/session/s/prompt_async',body:{text:'two'}});
 await assert.rejects(bb.send('s',h.client),/尚未发送/);assert.equal(h.sent.length,0);assert.equal(b.conflicts[0].group,'outbox');assert.throws(()=>b.resolveConflict('outbox','s','local'),/不能用新请求覆盖/);
 const result=await resolveLocalConflict(b,'outbox','s','remote');assert.equal(result.rebuildOutbox,true);const rebuilt=new Outbox(b.storage,'native');assert.equal(rebuilt.items.s.key,aa.items.s.key);stop(a,b);
});
test('stale Outbox cannot overwrite foreign scope learned during an unrelated draft save',async()=>{
 const h=harness(),a=await h.load(),b=await h.load(),aa=new Outbox(a.storage,'native'),bb=new Outbox(b.storage,'native');
 aa.prepare('s',{kind:'prompt',path:'/session/s/prompt_async',body:{text:'one'}});await a.flush();b.setDraft('another','draft');await b.flush();
 bb.prepare('s',{kind:'prompt',path:'/session/s/prompt_async',body:{text:'two'}});await assert.rejects(bb.send('s',h.client),/尚未发送/);assert.equal(h.sent.length,0);assert.equal(h.disk().outbox.s.key,aa.items.s.key);stop(a,b);
});
test('typing during a conflict fetch preserves latest local text and subsequent merge',async()=>{
 const h=harness(),a=await h.load(),b=await h.load();a.setDraft('a','one');await a.flush();let release;const original=b.client;b.client={...original,get:async()=>{await new Promise(r=>release=r);return original.get();}};
 b.setDraft('b','before');const flushing=b.flush();await new Promise(r=>setTimeout(r,0));b.setDraft('b','while reading');release();await flushing;assert.deepEqual(h.disk().drafts,{a:'one',b:'while reading'});stop(a,b);
});
