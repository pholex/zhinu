import {newConversationInput} from "../web/view_state.mjs";
import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import {createViewHistory,viewHistoryKey} from '../web/view_history.mjs';

const view=(source,extra={})=>({source,selected:'',newTask:false,filter:'all',projectSelected:'',cliSelected:'',...extra});
test('view history tracks distinct destinations, truncates forward and never mutates callers',()=>{
  const first=view('workbench',{selected:'one'}),h=createViewHistory({initial:first,key:viewHistoryKey});
  first.selected='changed';assert.equal(h.current().selected,'one');
  h.visit(view('settings'));h.visit(view('project',{projectSelected:'p'}));
  assert.equal(h.back().source,'settings');assert.equal(h.back().selected,'one');assert.equal(h.back(),null);
  assert.equal(h.forward().source,'settings');h.visit(view('library'));
  assert.equal(h.forward(),null);assert.equal(h.back().source,'settings');
});
test('refresh replacement and same-destination updates do not add back entries or lose forward',()=>{
  const h=createViewHistory({initial:view('workbench'),key:viewHistoryKey});
  h.visit(view('workbench',{selected:'one'}),{replace:true});assert.equal(h.back(),null);
  h.visit(view('settings'));h.visit(view('settings',{selected:'one',model:'changed'}));
  assert.equal(h.back().selected,'one');h.replace(view('workbench',{selected:'one',model:'latest'}));
  assert.equal(h.forward().source,'settings');assert.equal(h.back().model,'latest');
});
test('native state publishes only boolean changes and reset clears both directions',()=>{
  const updates=[],h=createViewHistory({initial:view('settings'),onChange:s=>updates.push(s)});
  h.visit(view('library'));h.visit(view('capabilities'));assert.equal(updates.length,2);
  h.back();h.back();h.reset(view('settings'));
  assert.deepEqual(updates,[{canGoBack:false,canGoForward:false},{canGoBack:true,canGoForward:false},{canGoBack:true,canGoForward:true},{canGoBack:false,canGoForward:true},{canGoBack:false,canGoForward:false}]);
});
test('project, CLI item, new-task and workbench filters have distinct identities',()=>{
  const values=[view('project',{projectSelected:'a'}),view('project',{projectSelected:'b'}),view('cli',{cliSelected:'a'}),view('cli',{cliSelected:'b'}),view('workbench'),view('workbench',{newTask:true}),view('workbench',{filter:'archived'})];
  assert.equal(new Set(values.map(viewHistoryKey)).size,values.length);
});
test('history remains bounded without changing the current view',()=>{
  const h=createViewHistory({initial:0,limit:3});for(let i=1;i<6;i++)h.visit(i);
  assert.equal(h.current(),5);assert.equal(h.back(),4);assert.equal(h.back(),3);assert.equal(h.back(),null);
});

function applicationFixture(){
  const source=readFileSync(new URL('../web/app.mjs',import.meta.url),'utf8');
  const navigation=source.slice(source.indexOf('function viewSnapshot()'),source.indexOf('function openManagement('));
  const choosing=source.slice(source.indexOf('function choose(id,'),source.indexOf('function titleOf('));
  const nodes=new Map(),reads=[],notifications=[],stored=new Map();
  const node=id=>{if(!nodes.has(id))nodes.set(id,{value:'',hidden:false,scrollTop:0,focus(){}});return nodes.get(id);};
  const loader={load(...args){reads.push(args);}};
  const context=vm.createContext({
    $,createViewHistory,viewHistoryKey,newConversationInput,hasRecentActivity:s=>!!s?.turns,session:()=>context.sessions.find(s=>s.session_id===context.selected),CustomEvent:class {constructor(type,options){this.type=type;this.detail=options.detail;}},
    window:{dispatchEvent:e=>notifications.push(e),webkit:{messageHandlers:{navigationState:{postMessage:s=>{notifications.push({native:s});return Promise.reject(new Error('bridge gone'));}}}}},
    desktop:true,collaborationPage:'',navigationUnavailable:'',source:'workbench',selected:'one',newTask:false,project:'',projectSelected:'',filter:'all',cliSelected:'',connectionTarget:'',navigationHistory:null,
    drafts:new Map(),connection:{localState:{getDraft:id=>stored.get(id),setDraft:(id,value)=>stored.set(id,value)}},sessions:[{session_id:'one',turns:1},{session_id:'two',turns:1}],
    modelPicker:{close(){}},permissionPicker:{close(){}},workflowPicker:{close(){}},eventController:{abort(){}},pageControllers:new Map(),cliList:{state:'ready'},
    watch:()=>reads.push('watch'),loadArchive:()=>reads.push('archive'),loadWeaves:()=>reads.push('weaves'),loadCLIList:()=>reads.push('cli-list'),loadCLIHistory:()=>reads.push('cli-history'),
    contests:loader,fleets:loader,fleetCommands:loader,workspaceController:{...loader,loadArtifacts(){},projectForSession:()=>"",projectForWorkspace:()=>"",projects:()=>[]},settingsController:{...loader,values:()=>({ui:{}})},providersController:loader,dataController:loader,connections:loader,automations:loader,
    render(){},notice(){},readMetadata:()=>null,pendingFor:()=>[],
  });
  function $(id){return node(id);}
  vm.runInContext(source.slice(source.indexOf('let inputWorkspace='),source.indexOf('const modelSaves='))+navigation+choosing+'\nnavigationHistory=createViewHistory({initial:viewSnapshot(),key:viewHistoryKey,onChange:navigationStateChanged});',context);
  return {context,nodes,reads,notifications,node,run:code=>vm.runInContext(code,context)};
}
test('actual app traversal restores latest unsent session drafts without submitting or reloading the document',async()=>{
  const f=applicationFixture();f.node('message').value='draft one';f.run('choose("two")');f.node('message').value='draft two';
  f.run('traverseNavigation("back")');assert.equal(f.context.selected,'one');assert.equal(f.node('message').value,'draft one');
  f.node('message').value='edited one';f.run('traverseNavigation("forward")');assert.equal(f.context.selected,'two');assert.equal(f.node('message').value,'draft two');
  f.run('traverseNavigation("back")');assert.equal(f.node('message').value,'edited one');
  await Promise.resolve();
  assert.ok(f.notifications.filter(n=>n.native).every(n=>Object.keys(n.native).sort().join(',')==='canGoBack,canGoForward'));
});
test('actual app traversal restores in-memory new task drafts without storage and reads CLI without replay',()=>{
  const f=applicationFixture();f.run('connection=null; selected=""; newTask=true; navigationHistory.reset(viewSnapshot())');f.node('message').value='new unsent draft';
  f.run('prepareNavigation();saveDraft();source="cli";cliSelected="catalog-one";rememberNavigation();traverseNavigation("back")');
  assert.equal(f.context.source,'workbench');assert.equal(f.context.newTask,true);assert.equal(f.node('message').value,'new unsent draft');
  f.run('traverseNavigation("forward")');assert.equal(f.context.source,'cli');assert.equal(f.context.cliSelected,'catalog-one');
});

test('unavailable historical session keeps its draft and cannot submit a replacement task',()=>{
  const f=applicationFixture();f.node('message').value='original draft';f.run('choose("two");sessions=[{session_id:"two"}];traverseNavigation("back")');
  assert.equal(f.context.selected,'one');assert.equal(f.context.navigationUnavailable,'one');assert.equal(f.node('message').value,'original draft');
  const app=readFileSync(new URL('../web/app.mjs',import.meta.url),'utf8');
  f.context.session=()=>undefined;f.context.submit=()=>{throw new Error('must not submit')};
  vm.runInContext(app.slice(app.indexOf('function submitText('),app.indexOf('function disconnect(')),f.context);
  assert.doesNotThrow(()=>f.run('submitText("original draft")'));
});
test('history does not copy execution continuations or replay workflow change events',()=>{
  const f=applicationFixture();f.context.startingDraft='in flight';f.context.pendingWorkflow={mode:'weave',sid:'two'};
  f.node('work-mode').value='weave';f.run('choose("two");traverseNavigation("back")');
  assert.equal(f.context.startingDraft,'in flight');assert.deepEqual(f.context.pendingWorkflow,{mode:'weave',sid:'two'});
  const snapshot=f.run('viewSnapshot()');assert.equal('startingDraft' in snapshot,false);assert.equal('pendingWorkflow' in snapshot,false);
  assert.equal(f.node('work-mode').value,'weave');
});

test('legacy template modes restore as conversation without altering drafts',()=>{
  for(const mode of ['chat','create','code']){
    const f=applicationFixture();f.node('work-mode').value=mode;f.node('message').value='原有草稿';
    f.run('choose("two");traverseNavigation("back")');
    assert.equal(f.node('work-mode').value,'auto');assert.equal(f.node('message').value,'原有草稿');
  }
});

test('each workflow page is a distinct destination and returning keeps the draft',()=>{
  const f=applicationFixture();
  f.node('message').value='保留的对话草稿';
  for(const mode of ['weave','contest','fleet']){
    f.run('prepareNavigation();collaborationPage="'+mode+'";rememberNavigation();');
  }
  f.run('traverseNavigation("back")');assert.equal(f.context.collaborationPage,'contest');
  f.run('traverseNavigation("back")');assert.equal(f.context.collaborationPage,'weave');
  f.run('choose("one")');assert.equal(f.context.collaborationPage,'');
  assert.equal(f.node('message').value,'保留的对话草稿');
  f.run('traverseNavigation("back")');assert.equal(f.context.collaborationPage,'weave');
});
