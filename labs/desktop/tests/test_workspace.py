import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from labs.desktop.workspace import Conflict, WorkspaceState, install_workspace_routes, project_id


class WorkspaceStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "workspace.json"
        self.store = WorkspaceState(self.path)
        self.pid = project_id("/project")

    def test_persistence_conflict_and_lost_reply_preserve_newer_edits(self):
        first = self.store.update(0, "projects", self.pid, {"context": "约束 A"})
        self.assertEqual(first["revision"], 1)
        reopened = WorkspaceState(self.path)
        self.assertEqual(reopened.update(0, "projects", self.pid, {"context": "约束 A"}), first)
        second = reopened.update(1, "projects", self.pid, {"context": "约束 B"})
        with self.assertRaises(Conflict):
            self.store.update(0, "projects", self.pid, {"context": "约束 A"})
        self.assertEqual(self.store.read(), second)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_write_guard_loss_at_each_boundary_keeps_project_state(self):
        original = self.store.update(0, "projects", self.pid, {"context": "saved"})
        for boundary in (1, 2, 3):
            with self.subTest(boundary=boundary):
                count = 0
                def guard():
                    nonlocal count
                    count += 1
                    if count == boundary:
                        raise OSError("fixture ownership lost")
                store = WorkspaceState(self.path, write_guard=guard)
                with self.assertRaises(OSError):
                    store.update(1, "projects", self.pid, {"context": "unowned"})
                self.assertEqual(store.read(), original)
                self.assertFalse(list(self.path.parent.glob(".workspace-*")))

    def test_organization_is_independent_and_exact_boolean(self):
        self.store.update(0, "sessions", "sess-one", {"pinned": True, "archived": False})
        self.store.update(1, "sessions", "sess-one", {"pinned": True, "archived": True})
        with self.assertRaises(ValueError):
            self.store.update(2, "sessions", "sess-one", {"pinned": 1, "archived": False})
        with self.assertRaises(ValueError):
            self.store.update(2, "sessions", "missing", None)
        self.assertEqual(self.store.read()["projects"], {})

    def test_corruption_and_oversize_do_not_reset_or_overwrite(self):
        self.path.write_bytes(b'{"broken":\xff}')
        original = self.path.read_bytes()
        with self.assertRaises(ValueError):
            self.store.update(0, "projects", self.pid, {"context": "new"})
        self.assertEqual(self.path.read_bytes(), original)
        self.path.unlink()
        with self.assertRaises(ValueError):
            self.store.update(0, "projects", self.pid, {"context": "x" * 20001})
        self.assertFalse(self.path.exists())

    def test_http_requires_auth_and_registered_project_or_session(self):
        from fastapi import FastAPI
        import httpx
        app = FastAPI()
        app.state.execution_owner = SimpleNamespace(held=True)
        app.state.sessions = {"sess-one": SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(workspace=Path("/project"))))}
        install_workspace_routes(app, self.path, "test-connection")

        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local") as client:
                self.assertEqual((await client.get("/desktop/workspace")).status_code, 401)
                client.headers["Authorization"] = "Bearer test-connection"
                data = (await client.get("/desktop/workspace")).json()
                self.assertEqual(data["catalogue"]["sessions"], {"sess-one": self.pid})
                body = {"revision": 0, "group": "projects", "key": self.pid, "value": {"context": "项目说明"}}
                saved = await client.post("/desktop/workspace", json=body)
                self.assertEqual(saved.status_code, 200)
                self.assertEqual((await client.post("/desktop/workspace", json=body)).json(), saved.json())
                for changes, status in [({"key": "unknown"}, 404), ({"group": []}, 422),
                                        ({"value": {"context": 12}}, 422), ({"value": {"context": "different"}}, 409)]:
                    self.assertEqual((await client.post("/desktop/workspace", json={**body, **changes})).status_code, status)
                self.assertEqual((await client.post("/desktop/workspace", content=b"x" * 100001)).status_code, 413)
                self.assertNotIn("token", json.dumps(saved.json()))
        asyncio.run(check())

    def test_http_owner_loss_blocks_mutation_but_keeps_reads_and_duplicate_receipt(self):
        from fastapi import FastAPI
        import httpx
        app = FastAPI()
        app.state.execution_owner = SimpleNamespace(held=True)
        app.state.sessions = {}
        install_workspace_routes(app, self.path, "test-owner", Path("/project"))
        async def check():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://local",
                    headers={"Authorization": "Bearer test-owner"}) as client:
                body = {"revision": 0, "group": "projects", "key": self.pid, "value": {"context": "saved"}}
                saved = (await client.post("/desktop/workspace", json=body)).json()
                for owner in (SimpleNamespace(held=False), None):
                    app.state.execution_owner = owner
                    response = await client.post("/desktop/workspace", json={**body, "revision": 1, "value": {"context": "no"}})
                    self.assertEqual(response.status_code, 503)
                    self.assertIn("所有权", response.json()["detail"])
                    self.assertEqual((await client.get("/desktop/workspace")).json(), saved)
                    self.assertEqual((await client.post("/desktop/workspace", json=body)).json(), saved)
        asyncio.run(check())

class ProjectArtifactsTests(unittest.TestCase):
    def test_aggregate_preserves_exact_cross_session_sources_and_limit(self):
        from labs.desktop.workspace import project_artifacts
        def session(workspace):
            return SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(workspace=Path(workspace))),
                runs=[{'id':'run-a','started_at':1,'result':{'artifacts':[{'id':'file-a','path':'a.txt'}]}}],
                weaves=SimpleNamespace(batches={'batch':{'batch_id':'batch','updated_at':2,
                    'items':[{'id':'item','artifacts':[{'id':'file-b'}]}]}}),
                contests=SimpleNamespace(contests={'contest':{'contest_id':'contest','updated_at':3,
                    'candidates':[{'id':'candidate','artifacts':[{'id':'file-c'}]}]}}))
        sessions={'one':session('/project'),'other':session('/other')}
        result=project_artifacts(sessions,project_id('/project'),limit=2)
        self.assertTrue(result['truncated'])
        self.assertEqual([a['session_id'] for a in result['artifacts']],['one','one'])
        self.assertEqual(result['artifacts'][0]['source'],{'contest_id':'contest','candidate_id':'candidate'})
        self.assertEqual(result['artifacts'][1]['source'],{'batch_id':'batch','item_id':'item'})
        result['artifacts'][0]['artifact']['id']='changed'
        self.assertEqual(sessions['one'].contests.contests['contest']['candidates'][0]['artifacts'][0]['id'],'file-c')

    def test_empty_root_project_available_before_first_session(self):
        import httpx
        from fastapi import FastAPI
        app=FastAPI();app.state.sessions={}
        with tempfile.TemporaryDirectory() as root:
            install_workspace_routes(app,Path(root)/'workspace.json','test-pair',Path('/empty'))
            async def check():
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://local',headers={'Authorization':'Bearer test-pair'}) as client:
                    catalogue=(await client.get('/desktop/workspace')).json()['catalogue']
                    self.assertEqual(catalogue['projects'],[{'project_id':project_id('/empty'),'workspace':'/empty','name':'empty','folders':['/empty'],'primary':'/empty'}])
                    page=await client.get('/desktop/projects/'+project_id('/empty')+'/artifacts')
                    self.assertEqual(page.status_code,200)
                    self.assertEqual(page.json()['artifacts'],[])
                    self.assertEqual((await client.get('/desktop/projects/unknown/artifacts')).status_code,404)
            asyncio.run(check())
