import test from 'node:test';
import assert from 'node:assert/strict';
import {parseMetadataBackup,createDataController} from '../web/data.mjs';
const tick=()=>new Promise(r=>setTimeout(r,0));
test('metadata import is previewed and explicitly confirmed; no automatic execution or retry',async()=>{
  const sent=[],saved=[];const ctx={writable:true,conn:{client:{get:async path=>path==='/desktop/usage'?{sessions:[],by_model:{},warning:'actual only'}:{storage:[],revisions:{preferences:2,workspace:3},can_clear_index:true},post:async(path,body)=>{sent.push({path,body});return {note:'restored'};}}}};
  const c=createDataController({context:()=>ctx,exportFile:f=>saved.push(f)});await c.load();
  const value={schema:'yuan-metadata-v1',kind:'workspace',values:{projects:{},sessions:{}}};
  c.input({target:{dataset:{dataText:''},value:JSON.stringify(value)}});
  const click=a=>c.click({target:{closest:()=>({dataset:{dataAction:a}})}});
  click('preview');assert.equal(sent.length,0);c.onSubmit({target:{hasAttribute:()=>true},preventDefault(){}});await tick();assert.equal(sent.length,0);
  c.input({target:{dataset:{dataConfirm:''},checked:true}});c.onSubmit({target:{hasAttribute:()=>true},preventDefault(){}});await tick();assert.equal(sent.length,1);assert.equal(sent[0].path,'/desktop/data/import');assert.equal(sent[0].body.revision,3);assert.deepEqual(sent[0].body.backup,value);
  assert.throws(()=>parseMetadataBackup(JSON.stringify({...value,outbox:{}})));assert.throws(()=>parseMetadataBackup(JSON.stringify({...value,kind:'sessions'})));
});
