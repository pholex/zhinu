import test from 'node:test';
import assert from 'node:assert/strict';
import {LocalState,Outbox,reconcileAcceptedDraft} from '../web/client.mjs';
import {scopeViewDrafts,claimLegacyDrafts,syncViewReceipts,validViewID,viewState,actionBelongsToView,workStatus} from '../web/view_state.mjs';
const a='11111111-1111-4111-8111-111111111111',b='22222222-2222-4222-8222-222222222222';
function server() {
  let state={revision:0,drafts:{},outbox:{}};let posts=0;
  return {get:async()=>structuredClone(state),post:async(path,body)=>{
    assert.equal(path,'/desktop/state');posts++;
    if(body.revision!==state.revision)throw Object.assign(new Error('conflict'),{status:409});
    state={...structuredClone(body),revision:body.revision+1};return {revision:state.revision};
  },state:()=>structuredClone(state),posts:()=>posts};
}
test('concurrent views merge separate composer, editor and form drafts across stale revisions',async()=>{
  const client=server(),one=scopeViewDrafts(await LocalState.load(client),a),two=scopeViewDrafts(await LocalState.load(client),b);
  for(const key of ['new-task','session','content:page','weave:session','workspace-layout']) {one.setDraft(key,'A');two.setDraft(key,'B');}
  await one.flush();await two.flush();assert.equal(two.conflicts.length,0);
  const restoredA=scopeViewDrafts(await LocalState.load(client),a),restoredB=scopeViewDrafts(await LocalState.load(client),b);
  assert.equal(restoredA.getDraft('session'),'A');assert.equal(restoredB.getDraft('session'),'B');
  assert.equal(Object.keys(restoredA.drafts).length,5);assert.equal(Object.keys(client.state().drafts).length,10);
  assert.equal(scopeViewDrafts(await LocalState.load(client),'33333333-3333-4333-8333-333333333333').getDraft('session'),'');
});
test('shared receipts keep original scopes and request keys, with explicit view ownership',async()=>{
  const client=server(),one=scopeViewDrafts(await LocalState.load(client),a),outbox=new Outbox(one.storage,'native');
  const action=outbox.prepare('session:prompt:',{kind:'prompt',sid:'session',body:{text:'same'},view_id:a});
  one.setDraft('session','same');await one.flush();
  const two=scopeViewDrafts(await LocalState.load(client),b);two.setDraft('session','same');await two.flush();
  const copy=new Outbox(two.storage,'native');assert.equal(copy.items['session:prompt:'].key,action.key);
  assert.equal(actionBelongsToView(action,a),true);assert.equal(actionBelongsToView(action,b),false);
  assert.equal(actionBelongsToView({...action,view_id:undefined},b),true);
  action.state='accepted';reconcileAcceptedDraft(one,action);await one.flush();
  assert.equal(scopeViewDrafts(await LocalState.load(client),b).getDraft('session'),'same');
});
test('legacy drafts are preserved without copying into every new view; side conversations remain explicit shared identities',async()=>{
  const client=server(),raw=await LocalState.load(client);raw.setDraft('session','legacy');raw.setDraft('side-conversation:abc','side');raw.setDraft('weave-receipts:s:b:i','receipt');await raw.flush();
  const scoped=scopeViewDrafts(raw,a);assert.equal(scoped.getDraft('session'),'');assert.equal(scoped.getDraft('side-conversation:abc'),'side');
  assert.equal(scoped.getDraft('weave-receipts:s:b:i'),'receipt');assert.equal(raw.getDraft('session'),'legacy');
  assert.equal(scopeViewDrafts(raw,''),raw);assert.equal(validViewID('bad:path'),false);
});
test('conflict resolution translates current-view draft keys without changing another view',async()=>{
  const client=server(),one=scopeViewDrafts(await LocalState.load(client),a),two=scopeViewDrafts(await LocalState.load(client),a);
  one.setDraft('session','first');two.setDraft('session','second');await one.flush();await assert.rejects(two.flush());
  assert.equal(two.conflicts[0].key,'session');two.resolveConflict('drafts','session','remote');await two.flush();assert.equal(two.getDraft('session'),'first');
});
test('view restoration exposes identifiers only and never sends tasks',()=>{
  assert.deepEqual(viewState('sid_1','settings'),{session_id:'sid_1',page:'settings'});
  assert.deepEqual(viewState({token:'secret'},'execute'),{session_id:'',page:'workbench'});
  assert.deepEqual(workStatus([{busy:true,pending_approvals:[{}],questionItems:[{state:'open'},{state:'answered'}]},{busy:false,questionItems:[{state:'pending'}]}],true),{running:1,pending:3});
  assert.equal(workStatus([],false),null);
});

test('legacy claim is atomic across two simultaneous new views and preserves exact original receipt',async()=>{
  const client=server(),legacy=await LocalState.load(client);
  legacy.setDraft('session','legacy draft');legacy.setDraft('new-task','new draft');legacy.setDraft('side-conversation:abc','shared side');
  const old=new Outbox(legacy.storage,'native');const original=old.prepare('session:prompt:',{kind:'prompt',sid:'session',body:{text:'legacy draft'}});original.state='unknown';old.save();await legacy.flush();
  const first=await LocalState.load(client),second=await LocalState.load(client);
  const [owned,other]=await Promise.all([claimLegacyDrafts(first,a),claimLegacyDrafts(second,b)]);
  assert.equal(scopeViewDrafts(owned,a).getDraft('session'),'legacy draft');assert.equal(scopeViewDrafts(other,b).getDraft('session'),'');
  const stored=client.state();assert.equal(stored.drafts['view:legacy-owner'],a);assert.equal(stored.drafts['side-conversation:abc'],'shared side');assert.equal(stored.drafts.session,undefined);
  assert.deepEqual(stored.outbox['session:prompt:'],{...original,view_id:a});
  const before=client.posts();await claimLegacyDrafts(await LocalState.load(client),b);assert.equal(client.posts(),before);
});
test('legacy claim failure preserves original state and can safely retry later',async()=>{
  const client=server(),raw=await LocalState.load(client);raw.setDraft('session','keep me');await raw.flush();
  const original=client.state(),realPost=client.post;client.post=async()=>{throw new Error('disk full');};
  await assert.rejects(claimLegacyDrafts(await LocalState.load(client),a),/disk full/);assert.deepEqual(client.state(),original);
  client.post=realPost;const claimed=await claimLegacyDrafts(await LocalState.load(client),a);assert.equal(scopeViewDrafts(claimed,a).getDraft('session'),'keep me');
});
test('other views refresh resolved receipts without replay and preserve original key for unknown receipts',async()=>{
  const client=server(),one=scopeViewDrafts(await claimLegacyDrafts(await LocalState.load(client),a),a);
  const box=new Outbox(one.storage,'native'),item=box.prepare('session:prompt:',{kind:'prompt',sid:'session',body:{text:'task'},view_id:a});item.state='sending';box.save();await one.flush();
  const two=scopeViewDrafts(await LocalState.load(client),b),other=new Outbox(two.storage,'native');assert.equal(other.items['session:prompt:'].state,'unknown');
  const key=other.items['session:prompt:'].key;box.forget('session:prompt:');await one.flush();
  const synced=await syncViewReceipts(two,other);assert.equal(synced.items['session:prompt:'],undefined);assert.equal(key,item.key);
  assert.equal(two.getDraft('session'),'');
});
test('receipt refresh does not replace an in-flight send or a concurrently edited draft',async()=>{
  const client=server(),state=scopeViewDrafts(await LocalState.load(client),a),box=new Outbox(state.storage,'native');
  box.running.add('session:prompt:');assert.equal(await syncViewReceipts(state,box),null);box.running.clear();
  const get=client.get;client.get=async()=>{state.setDraft('session','new typing');return get();};
  assert.equal(await syncViewReceipts(state,box),null);await state.flush();assert.equal(state.getDraft('session'),'new typing');
});

test('new conversation input is isolated by folder, service and window, including reload and clearing',async()=>{
  const {newConversationInput:input}=await import('../web/view_state.mjs');
  const client=server(),state=scopeViewDrafts(await LocalState.load(client),a);
  input(state,'/projects/A','A 的草稿\n第二行');input(state,'/projects/B','B 的草稿');await state.flush();
  const restored=scopeViewDrafts(await LocalState.load(client),a);
  assert.equal(input(restored,'/projects/A'),'A 的草稿\n第二行');assert.equal(input(restored,'/projects/B'),'B 的草稿');
  assert.equal(input(scopeViewDrafts(await LocalState.load(client),b),'/projects/A'),'');
  assert.equal(input(scopeViewDrafts(await LocalState.load(server()),a),'/projects/A'),'');
  input(restored,'/projects/A','');await restored.flush();
  assert.equal(input(restored,'/projects/A'),'');assert.equal(input(restored,'/projects/B'),'B 的草稿');
  assert.deepEqual(client.state().outbox,{});
});
