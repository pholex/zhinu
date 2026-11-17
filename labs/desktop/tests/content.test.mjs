import test from 'node:test';
import assert from 'node:assert/strict';
import {createContentController,safePagePreview,staticHTML,contentReference,mergeContentDraft} from '../web/content.mjs';
const tick=()=>new Promise(r=>setTimeout(r,0));
function harness(uiOptions={}){
 const state={revision:0,pages:{},spaces:{},projects:{},favorites:{}},saved=new Map(),sent=[],receipts=new Map(),opened=[],exports=[],events=[];
 let lose=false,readError=false,flushError=false,postPause=null,composer='existing draft';
 const row={content_id:'art',session_id:'source-session',project_id:'p',source:{contest_id:'contest',candidate_id:'candidate'},artifact:{id:'file',path:'page.html',sha256:'hash',size:20},type:'web',created_at:1};
 const ctx={writable:true,sid:'target-session',active:true,conn:{controller:new AbortController(),localState:{getDraft:k=>saved.get(k),setDraft:(k,v)=>saved.set(k,v),deleteDraft:k=>saved.delete(k),flush:async()=>{events.push('flush');if(flushError)throw new Error('disk failed');}},client:{get:async path=>{
   if(readError)throw new Error('offline');if(path.startsWith('/session/')){events.push(path);return {delivered:{available:true,text:'<h1>deliver</h1>',truncated:false},version:'current_changed'};}
   if(path.includes('/export'))return {filename:'page.md',text:'saved export',version:1};
   if(path.startsWith('/desktop/content/files'))return {project_id:'p',directory:'',entries:[{name:'a.txt',path:'a.txt',kind:'file',size:10}],warning:'scope',truncated:false};
   if(path.startsWith('/desktop/content/file?'))return {project_id:'p',path:'a.txt',text:'0123456789',size:10,available:true,truncated:false,modified_at:1};
   if(path.includes('/library'))return {artifacts:[row],warning:'',truncated:false};
   if(path.includes('/search'))return {results:[{id:'msg',title:'source',kind:'message',excerpt:'<script>needle',session_id:'source-session',message_id:'log:12'}],total:1,next_cursor:null,indexed_at:1,scope:'registered',warning:'',stale:false};
   return structuredClone({state,catalogue:{projects:[{project_id:'p',workspace:'/project'}]},warning:''});
 },post:async(path,body)=>{events.push('post');sent.push({path,body:structuredClone(body)});if(postPause)await postPause;
   if(path.endsWith('/rebuild'))return {};
   if(!receipts.has(body.request_id)){
    if(body.revision!==state.revision)throw Object.assign(new Error('changed'),{status:409});const v=structuredClone(body.value);
    if(body.action==='page_save'){const old=state.pages[v.id],version=(old?.version || 0)+1;if(v.expected_version!==(old?.version || 0))throw Object.assign(new Error('version'),{status:409});const versionRecord={number:version,title:v.title,body:v.body,format:v.format,source:v.source,created_at:version};state.pages[v.id]={...v,version,deleted:false,versions:[...(old?.versions || []),versionRecord],created_at:1,updated_at:version};delete state.pages[v.id].expected_version;}
    if(body.action==='favorite')state.favorites[v.id]=v.favorite;
    if(body.action==='project_save'){delete v.id;state.projects[body.value.id]=v;}
    if(body.action==='space_save')state.spaces[v.id]=v;
    if(body.action==='page_delete')state.pages[v.id].deleted=true;if(body.action==='page_restore')state.pages[v.id].deleted=false;
    state.revision++;receipts.set(body.request_id,true);
   }
   if(lose){lose=false;throw new Error('lost receipt');}return structuredClone({state,catalogue:{projects:[{project_id:'p',workspace:'/project'}]},receipt:{},warning:''});
 }}}};
 const c=createContentController({context:()=>ctx,render:()=>{},openSession:(...s)=>opened.push(s),getComposer:()=>composer,putComposer:text=>composer=text,exportFile:file=>exports.push(file),artifactCard:(a,source)=>`<span>${JSON.stringify(source)}</span>`,...uiOptions});
 const click=(action,id='',extra={})=>c.click({target:{closest:()=>({dataset:{contentAction:action,contentId:id},...extra})}});
 const input=(key,value)=>c.input({target:{dataset:{contentEdit:key},value}});
 const field=(key,value)=>c.input({target:{dataset:{contentField:key},value}});
 const submit=(form='save')=>c.onSubmit({target:{dataset:{contentForm:form}},preventDefault:()=>{}});
 return {c,ctx,state,saved,sent,opened,exports,events,click,input,field,submit,composer:()=>composer,lose:()=>lose=true,failRead:()=>readError=true,failFlush:()=>flushError=true,pause:p=>postPause=p,async page(){await c.load();click('new-page');input('title','Title');input('body','my body');}};
}
test('new page saves version through persistent request and preserves unrelated composer',async()=>{
 const h=harness();await h.page();h.submit();await tick();assert.equal(h.events[0],'flush');assert.equal(h.sent.length,1);const id=h.sent[0].body.value.id;assert.equal(h.state.pages[id].version,1);assert.equal(h.composer(),'existing draft');h.input('body','second');h.submit();await tick();assert.equal(h.state.pages[id].version,2);assert.equal(h.state.pages[id].versions[0].body,'my body');
});
test('lost receipt restores original request without replay, including delayed LocalState initialization',async()=>{
 const h=harness();await h.page();h.lose();h.submit();await tick();const original=h.sent[0],local=h.ctx.conn.localState;h.c.reset();delete h.ctx.conn.localState;await h.c.load();h.c.render('pages');assert.equal(h.sent.length,1);
 h.ctx.conn.localState=local;h.c.render('pages');assert.match(h.c.render(),/未自动重发/);h.click('retry');await tick();assert.deepEqual(h.sent[1],original);assert.equal(Object.values(h.state.pages)[0].version,1);
});
test('disk failures prevent writes and preserve draft and request',async()=>{
 const h=harness();await h.page();h.failFlush();h.submit();await tick();assert.equal(h.sent.length,0);assert.match(h.saved.get('content-editor'),/my body/);assert.match(h.c.render(),/disk failed/);
});
test('CAS conflicts preserve editor and require explicit rebase after newer page',async()=>{
 const h=harness();await h.page();h.submit();await tick();const id=h.sent[0].body.value.id;h.input('body','mine');h.state.revision++;const p=h.state.pages[id];p.version=2;p.body='another window';p.versions.push({...p.versions[0],number:2,body:p.body});h.submit();await tick();assert.match(h.c.render('pages'),/mine/);assert.match(h.c.render(),/another window/);const count=h.sent.length;h.click('retry');await tick();assert.equal(h.sent.length,count);h.click('forget');h.click('rebase-page');h.submit();await tick();assert.equal(h.state.pages[id].body,'mine');assert.equal(h.state.pages[id].version,3);
});
test('replaying accepted older save does not silently rebase stale editor over later work',async()=>{
 const h=harness();await h.page();h.lose();h.submit();await tick();const id=h.sent[0].body.value.id,p=h.state.pages[id];h.state.revision++;p.version=2;p.body='later external';p.versions.push({...p.versions[0],number:2,body:p.body});h.click('retry');await tick();assert.equal(JSON.parse(h.saved.get('content-editor')).value.expected_version,0);assert.match(h.c.render('pages'),/当前页面已变为 v2/);
});
test('text edited during an in-flight save remains in editor after acknowledgement',async()=>{
 const h=harness();await h.page();let release;h.pause(new Promise(r=>release=r));h.submit();await tick();h.input('body','typed while waiting');release();await tick();assert.equal(Object.values(h.state.pages)[0].body,'my body');assert.equal(JSON.parse(h.saved.get('content-editor')).value.body,'typed while waiting');assert.equal(JSON.parse(h.saved.get('content-editor')).value.expected_version,1);
});
test('explicit reference offers append or replace and refuses changed destination',async()=>{
 const h=harness();await h.page();h.submit();await tick();const id=h.sent[0].body.value.id;h.click('reference-page',id);assert.equal(h.composer(),'existing draft');assert.match(h.c.render(),/目标会话已有草稿/);h.ctx.sid='other';h.click('append');assert.equal(h.composer(),'existing draft');h.click('reference-page',id);h.click('append');assert.match(h.composer(),/^existing draft\n\n页面引用/);assert.match(h.composer(),/保存版本 1/);
});
test('artifact copying reads exact source delivery and does not modify its file',async()=>{
 const h=harness();await h.c.load();await h.c.loadLibrary();assert.match(h.c.render('library'),/source-session/);h.click('artifact-copy','art');await tick();const d=JSON.parse(h.saved.get('content-editor')).value;assert.equal(d.body,'<h1>deliver</h1>');assert.deepEqual(d.references,['art']);assert.match(d.source,/原文件不改写/);assert.ok(h.events.some(e=>e==='/session/source-session/artifacts/file?contest_id=contest&candidate_id=candidate'));assert.equal(h.sent.length,0);
});
test('static HTML preview cannot navigate, execute, load resources or expose app bridge',()=>{
 const body='<meta http-equiv="refresh" content="0;url=https://evil"><script>alert(1)</script><a href="javascript:x">link</a><img src="https://evil/x"><p onclick="x" style="background:url(https://evil)">ok</p><iframe srcdoc="bad"></iframe>';
 const rendered=staticHTML(body);assert.doesNotMatch(rendered,/<(?:meta|script|a|img|iframe)\b|onclick=|style=/);assert.match(rendered,/<p>ok<\/p>/);const frame=safePagePreview(body,'html');assert.match(frame,/sandbox=""/);assert.match(frame,/default-src/);assert.doesNotMatch(frame,/allow-scripts|allow-same-origin/);assert.doesNotMatch(safePagePreview('<script>x</script>','markdown'),/<script>/);
});
test('search is explicit, retains source message identity, and escapes results',async()=>{
 const h=harness();await h.c.load();h.field('query','needle');h.submit('search');await tick();assert.match(h.c.render('search'),/&lt;script&gt;needle/);h.click('search-result','0');assert.deepEqual(h.opened,[['source-session',{messageId:'log:12',artifactId:undefined,source:undefined}]]);h.click('rebuild');await tick();assert.equal(h.sent[0].path,'/desktop/content/search/rebuild');
});
test('file browser selects exact text range and clearly identifies source snapshot',async()=>{
 const h=harness();await h.c.load();h.c.openProject('p');h.click('files','p');await tick();h.click('file','a.txt');await tick();assert.match(h.c.render(),/0123456789/);h.click('reference-file','',{closest:()=>({querySelector:()=>({selectionStart:2,selectionEnd:5})})});h.click('append');assert.match(h.composer(),/字符范围 2–5/);assert.match(h.composer(),/\n234$/);assert.doesNotMatch(h.composer(),/0123456789/);
});
test('failed refresh preserves stale saved data and blocks new mutations',async()=>{
 const h=harness();await h.page();h.submit();await tick();h.failRead();await h.c.load();h.input('body','local');h.submit();await tick();assert.equal(h.sent.length,1);assert.match(h.c.render(),/上次快照/);assert.match(h.saved.get('content-editor'),/local/);
});
test('page export uses selected stored version and does not export unsaved edits',async()=>{
 const h=harness();await h.page();h.submit();await tick();const id=h.sent[0].body.value.id;h.input('body','unsaved');h.click('export',id);await tick();assert.equal(h.exports[0].text,'saved export');assert.equal(h.exports[0].version,1);assert.match(h.saved.get('content-editor'),/unsaved/);
});
test('reference merge never replaces an existing different draft without a choice',()=>{
 assert.equal(mergeContentDraft('old','new'),null);assert.equal(mergeContentDraft('old','new','append'),'old\n\nnew');assert.match(contentReference({id:'p',title:'name',version:3,body:'body'}),/保存版本 3/);
});

test('restored malformed editor is disclosed without replacing its original text',async()=>{const h=harness();h.saved.set('content-editor',JSON.stringify({kind:'page',value:{id:'broken'}}));await h.c.load();assert.match(h.c.render('pages'),/草稿无法读取/);h.click('new-page');h.submit();assert.equal(h.sent.length,0);assert.equal(JSON.parse(h.saved.get('content-editor')).value.id,'broken');});
test('panel editor uses an independent durable namespace and renders no second page list',async()=>{const h=harness({draftNamespace:'page:example'});await h.page();assert.ok(h.saved.has('content-editor:page:example'));assert.equal(h.saved.has('content-editor'),false);h.submit();await tick();const id=h.sent[0].body.value.id;h.c.openPage(id);const html=h.c.render('page-panel');assert.match(html,/页面编辑器/);assert.doesNotMatch(html,/空间与页面/);h.c.reset();await h.c.load();assert.match(h.c.render('page-panel'),/my body/);});
