import asyncio
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from labs.desktop.content import ContentConflict, ContentStore, artifact_id, export_page
from labs.desktop.content_api import SearchIndex, install_content_routes, library
from labs.desktop.workspace import project_id


def page(identifier='page-a', body='正文', version=0, **extra):
    return dict(id=identifier, title='页面', body=body, format='markdown', project_id='', space_id='', references=[], source='用户输入', expected_version=version, **extra)


def req(action, value, revision=0, key='request-a'):
    return dict(action=action, value=value, revision=revision, request_id=key)


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'content.json'; self.store = ContentStore(self.path)

    def test_versions_cas_replay_recovery_and_export(self):
        first = req('page_save', page())
        self.store.update(first)
        second = req('page_save', page(body='新版', version=1), 1, 'request-b')
        self.store.update(second)
        reopened = ContentStore(self.path)
        replay = reopened.update(first)
        self.assertTrue(replay['duplicate'])
        self.assertEqual(replay['state']['pages']['page-a']['body'], '新版')
        self.assertEqual(len(replay['state']['pages']['page-a']['versions']), 2)
        self.assertEqual(export_page(replay['state']['pages']['page-a'], 1)['text'], '正文')
        with self.assertRaises(ContentConflict): reopened.update({**first, 'value': page(body='stale')})
        with self.assertRaises(ContentConflict): reopened.update(req('page_save', page(body='stale', version=1), 2, 'request-c'))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_delete_restore_keeps_versions_and_space_no_cascade(self):
        self.store.update(req('space_save', dict(id='space',title='知识',description='',project_ids=[],artifact_ids=[])))
        p=page();p['space_id']='space';self.store.update(req('page_save',p,1,'p'))
        self.store.update(req('page_delete',{'id':'page-a'},2,'delete'))
        with self.assertRaises(ContentConflict):self.store.update(req('space_delete',{'id':'space'},3,'delete-space'))
        restored=self.store.update(req('page_restore',{'id':'page-a'},3,'restore'))
        self.assertFalse(restored['state']['pages']['page-a']['deleted'])
        self.assertEqual(restored['state']['pages']['page-a']['version'],1)

    def test_owner_loss_at_every_write_boundary_preserves_state_and_known_receipts(self):
        first=req('page_save',page());self.store.update(first);original=self.path.read_bytes()
        for boundary in (1,2,3):
            count=0
            def guard():
                nonlocal count
                count+=1
                if count==boundary:raise OSError('lost owner')
            store=ContentStore(self.path,write_guard=guard)
            self.assertTrue(store.update(first)['duplicate'])
            with self.assertRaises(OSError):store.update(req('page_save',page(body='new',version=1),1,'new'))
            self.assertEqual(self.path.read_bytes(),original)
            self.assertFalse(list(self.path.parent.glob('.content-*')))

    def test_schema_corruption_and_unknown_references_cannot_overwrite(self):
        for action,value in [('page_save',{**page(),'project_id':'unknown'}),('page_save',{**page(),'references':['unknown']}),('favorite',{'id':'missing','favorite':True}),('project_save',{'id':'unknown','instructions':'','materials':[]})]:
            with self.assertRaises(ValueError):self.store.update(req(action,value))
        self.path.write_bytes(b'{"bad":\xff}')
        with self.assertRaises(ValueError):self.store.update(req('page_save',page()))
        self.assertEqual(self.path.read_bytes(),b'{"bad":\xff}')

    def test_project_materials_and_space_artifact_references_are_explicit(self):
        pid=project_id('/project')
        value={'id':pid,'instructions':'写作约束','materials':[{'id':'m','title':'资料','text':'摘要','source':'手工粘贴'}]}
        self.store.update(req('project_save',value),project_ids=[pid])
        self.store.update(req('favorite',{'id':'artifact-a','favorite':True},1,'star'),artifact_ids=['artifact-a'])
        s=self.store.update(req('space_save',{'id':'space','title':'资料集','description':'','project_ids':[pid],'artifact_ids':['artifact-a']},2,'space'),project_ids=[pid],artifact_ids=['artifact-a'])
        self.assertTrue(s['state']['favorites']['artifact-a'])
        self.assertEqual(s['state']['projects'][pid]['materials'][0]['text'],'摘要')
        self.assertNotIn('messages',s['state'])


class ContentApiTests(unittest.TestCase):
    def session(self,root,body='交付正文',sid='run'):
        raw=body.encode(); artifact={'id':'file','path':'note.md','sha256':hashlib.sha256(raw).hexdigest(),'size':len(raw)}
        preview={'id':'file','path':'note.md','delivered':{'available':True,'base64':base64.b64encode(raw).decode()}}
        return SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(workspace=root),session_log=None),
            runs=[{'id':sid,'started_at':10,'result':{'artifacts':[artifact]},'artifact_previews':[preview]}],title='测试会话',busy=False)

    def test_aggregate_exact_identity_same_path_different_sessions_and_no_disk_read(self):
        sessions={'one':self.session(Path('/one')),'two':self.session(Path('/one'))}
        rows,warning,_=library(sessions,{})
        self.assertEqual(len(rows),2);self.assertNotEqual(rows[0]['content_id'],rows[1]['content_id'])
        self.assertEqual({r['session_id'] for r in rows},{'one','two'})
        self.assertEqual(rows[0]['source'],{'run_id':'run'})
        rows[0]['artifact']['path']='mutated';self.assertEqual(sessions['one'].runs[0]['result']['artifacts'][0]['path'],'note.md')

    def test_search_only_user_assistant_and_verified_delivery_and_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);log=root/'log.jsonl'
            messages=[{'role':'system','content':'secret-system'},{'role':'user','content':'需要苹果计划'},{'role':'assistant','content':'苹果方案'},{'role':'tool','content':'secret-tool'}]
            log.write_text('\n'.join(json.dumps(v) for v in messages)+'\n')
            store=ContentStore(root/'content.json');p=page(body='苹果知识');store.update(req('page_save',p))
            sessions={'one':self.session(root,'苹果交付')};rows,_,_=library(sessions,{})
            index=SearchIndex();index.build(store.public(),[],rows,[dict(path=log,before=log.stat().st_size,session_id='one',project_id=project_id(str(root)),title='测试')],{})
            found=index.search('苹果')['results'];self.assertEqual({r['kind'] for r in found},{'page','artifact','message'})
            self.assertEqual(next(r for r in found if r['kind']=='artifact')['source'],{'run_id':'run'})
            self.assertEqual(next(r for r in found if r['kind']=='message')['message_id'],'log:2')
            self.assertEqual(index.search('secret')['total'],0)
            rows[0]['artifact']['sha256']='wrong';index.build(store.public(),[],rows,[],{})
            self.assertEqual(index.search('苹果交付')['total'],0)

    def test_http_auth_cas_export_library_search_rebuild_and_owner_gate(self):
        import httpx
        from fastapi import FastAPI
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);app=FastAPI();app.state.execution_owner=SimpleNamespace(held=True)
            app.state.sessions={'one':self.session(root)}
            install_content_routes(app,root/'content.json','test-token',root)
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local') as client:
                    self.assertEqual((await client.get('/desktop/content')).status_code,401)
                    client.headers['Authorization']='Bearer test-token'
                    content=(await client.get('/desktop/content')).json();self.assertEqual(content['state']['revision'],0)
                    rows=(await client.get('/desktop/content/library')).json()['artifacts'];self.assertNotIn('_preview',rows[0])
                    first=req('page_save',page(body='离线搜索正文'))
                    result=await client.post('/desktop/content',json=first);self.assertEqual(result.status_code,200,result.text)
                    self.assertEqual((await client.get('/desktop/content/pages/page-a/export?version=1')).json()['text'],'离线搜索正文')
                    self.assertEqual((await client.get('/desktop/content/search',params={'q':'离线搜索'})).json()['total'],1)
                    self.assertEqual((await client.post('/desktop/content/search/rebuild',json={})).status_code,200)
                    self.assertEqual((await client.post('/desktop/content',json=req('page_save',page(body='bad'),0,'different'))).status_code,409)
                    app.state.execution_owner.held=False
                    self.assertTrue((await client.post('/desktop/content',json=first)).json()['duplicate'])
                    self.assertEqual((await client.post('/desktop/content',json=req('page_delete',{'id':'page-a'},1,'delete'))).status_code,503)
                    self.assertEqual((await client.get('/desktop/content')).status_code,200)
                    self.assertEqual((await client.get('/desktop/content/pages/..%2Fsecret/export')).status_code,404)
            asyncio.run(check())

    def test_search_skipped_large_log_warns_and_does_not_scan_neighbor(self):
        import httpx
        from fastapi import FastAPI
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);log=root/'large.jsonl';log.write_bytes(b' '*(2*1024*1024+1))
            (root/'secret.txt').write_text('never-index-me')
            app=FastAPI();s=self.session(root);s.agent.session_log=SimpleNamespace(path=log)
            app.state.sessions={'one':s};install_content_routes(app,root/'content.json','token',root)
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local',headers={'Authorization':'Bearer token'}) as client:
                    value=(await client.get('/desktop/content/search?q=never-index-me')).json()
                    self.assertEqual(value['total'],0);self.assertIn('2 MiB',value['warning'])
            asyncio.run(check())


class ProjectFileTests(unittest.TestCase):
    def test_browse_and_read_are_bounded_and_refuse_hidden_links_credentials_and_escape(self):
        from labs.desktop.content import project_files, project_file
        import os
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'docs').mkdir();(root/'docs'/'note.md').write_bytes(b'hello\xff')
            (root/'.env').write_text('password');(root/'credentials.json').write_text('secret');(root/'private.key').write_text('key')
            (root/'binary.bin').write_bytes(b'\x00\x01');(root/'long.txt').write_text('a'*70000)
            (root/'alias').symlink_to(root/'docs',target_is_directory=True)
            (root/'hard.txt').write_text('hard');os.link(root/'hard.txt',root/'hard2.txt')
            listing=project_files(root);names={entry['name'] for entry in listing['entries']}
            self.assertEqual(names,{'docs','binary.bin','long.txt'})
            self.assertEqual(project_file(root,'docs/note.md')['text'],'hello\ufffd')
            self.assertFalse(project_file(root,'binary.bin')['available'])
            self.assertEqual(project_file(root,'binary.bin')['text'],'')
            long=project_file(root,'long.txt');self.assertTrue(long['truncated']);self.assertEqual(len(long['text']),65536);self.assertEqual(long['sha256'],'')
            for path in ['../secret','/etc/passwd','.env','credentials.json','private.key','docs/../long.txt','docs//note.md']:
                with self.subTest(path=path),self.assertRaises(ValueError):project_file(root,path)
            for path in ['alias/note.md','hard.txt']:
                with self.subTest(path=path),self.assertRaises((ValueError,OSError)):project_file(root,path)

    def test_file_http_uses_registered_project_not_client_root(self):
        import httpx
        from fastapi import FastAPI
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve();(root/'note.md').write_text('registered text')
            app=FastAPI();app.state.sessions={};install_content_routes(app,root/'content.json','token',root)
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local',headers={'Authorization':'Bearer token'}) as client:
                    pid=project_id(str(root))
                    listing=await client.get('/desktop/content/files',params={'project_id':pid,'directory':''});self.assertEqual(listing.status_code,200)
                    file=await client.get('/desktop/content/file',params={'project_id':pid,'path':'note.md'});self.assertEqual(file.json()['text'],'registered text')
                    self.assertEqual((await client.get('/desktop/content/file',params={'project_id':'unknown','path':'note.md'})).status_code,404)
                    self.assertEqual((await client.get('/desktop/content/file',params={'project_id':pid,'path':'../note.md'})).status_code,422)
            asyncio.run(check())
