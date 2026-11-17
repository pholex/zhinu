"""Project metadata and cross-session results survive the native service lifecycle."""
import shutil
import sys
import unittest
from labs.desktop.tests import test_weave_actions_contract as shared


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('node') and shutil.which('git'), 'native desktop tools')
class WorkspaceContract(unittest.TestCase):
    setUp = shared.WeaveActionsContractTests.setUp
    cleanup_children = shared.WeaveActionsContractTests.cleanup_children
    ready = shared.WeaveActionsContractTests.ready
    request = shared.WeaveActionsContractTests.request
    stop = shared.WeaveActionsContractTests.stop
    node = shared.WeaveActionsContractTests.node
    launch = shared.WeaveActionsContractTests.launch
    write = staticmethod(shared.WeaveActionsContractTests.write)

    def test_project_context_organization_and_exact_result_reference_survive_restart(self):
        child,info=self.launch(self.write('project-result.txt','项目成果')+'---\ntext: 已完成项目成果。\n')
        result=self.node(info,r'''
const empty=await client.get('/desktop/workspace');
assert.equal(empty.catalogue.projects.length,1);
const pid=empty.catalogue.projects[0].project_id;
assert.equal((await client.get(`/desktop/projects/${pid}/artifacts`)).artifacts.length,0);
const body={revision:0,group:'projects',key:pid,value:{context:'所有交付先解释用途。'}};
let dropped=false;
const lossy=new ReadClient(new URL(info.url).origin,info.token,async(url,options)=>{
 const response=await fetch(url,options);
 if(!dropped&&options.method==='POST'){dropped=true;await response.arrayBuffer();throw new TypeError('lost');}
 return response;
});
await assert.rejects(lossy.post('/desktop/workspace',body));
const saved=await client.post('/desktop/workspace',body);assert.equal(saved.state.revision,1);
const sid=(await send('create',{kind:'create',path:'/session',body:{model:'lab-scripted'}})).session_id;
const other=(await send('create-other',{kind:'create',path:'/session',body:{model:'lab-scripted'}})).session_id;
const metadata={revision:1,group:'sessions',key:sid,value:{pinned:true,archived:true}};
assert.equal((await client.post('/desktop/workspace',metadata)).state.revision,2);
await send('prompt',{kind:'prompt',sid,path:`/session/${sid}/prompt_async`,body:{text:'写出项目成果。'}});
await approval(sid,'allow');await waitFor(sid,state=>!state.busy);
const page=await client.get(`/desktop/projects/${pid}/artifacts`);
assert.equal(page.artifacts.length,1);assert.equal(page.artifacts[0].session_id,sid);
const a=page.artifacts[0];assert.ok(a.source.run_id);
const path=`/session/${a.session_id}/artifacts/${a.artifact.id}?run_id=${a.source.run_id}`;
const detail=await client.get(path);assert.equal(detail.delivered.text,'项目成果');
assert.equal((await client.get(`/session/${other}/runs`)).runs.length,0);
assert.equal((await client.get('/desktop/workspace')).state.sessions[sid].archived,true);
await assert.rejects(client.post('/desktop/workspace',{...body,value:{context:'stale overwrite'}}),e=>e.status===409);
console.log(JSON.stringify({pid,sid,other,path,metadata}));
''')
        self.stop(child)
        child,info=self.launch('error: metadata must never execute a model\n')
        self.node(info,r'''
const p=input.previous;
const state=(await client.get('/desktop/workspace')).state;
assert.equal(state.projects[p.pid].context,'所有交付先解释用途。');
assert.deepEqual(state.sessions[p.sid],{pinned:true,archived:true});
assert.equal((await client.post('/desktop/workspace',p.metadata)).state.revision,2);
assert.equal((await client.get(p.path)).delivered.text,'项目成果');
assert.equal((await client.get(`/desktop/projects/${p.pid}/artifacts`)).artifacts[0].session_id,p.sid);
assert.equal((await client.get(`/session/${p.sid}/status`)).busy,false);
console.log(JSON.stringify({ok:true}));
''',previous=result)
        self.stop(child)
