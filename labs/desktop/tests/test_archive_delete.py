"""Archived deletion removes owned histories, never workspace files or active chats."""
import json
from pathlib import Path
from unittest.mock import patch

from tests.test_serve import ServeCase
from labs.desktop.workspace import install_workspace_routes


class ArchiveDeleteTests(ServeCase):
    def launch(self):
        self.state_dir = Path(self.tmp) / "state"
        self.start("text: done\n", state_dir=self.state_dir)
        self.workspace_path = Path(self.tmp) / "workspace.json"
        install_workspace_routes(self.client.app, self.workspace_path, "archive-test", self.root)
        self.auth = {"Authorization": "Bearer archive-test"}

    def archive(self, sid):
        state = self.client.get('/desktop/workspace', headers=self.auth).json()['state']
        return self.client.post('/desktop/workspace', headers=self.auth, json={
            'revision': state['revision'], 'group': 'sessions', 'key': sid,
            'value': {'pinned': True, 'archived': True}}).json()['state']['revision']

    def delete(self, ids, revision):
        return self.client.post('/desktop/archived-sessions/delete', headers=self.auth,
                                json={'session_ids': ids, 'revision': revision})

    def test_single_bulk_retry_and_restart_preserve_project_files_and_active_chat(self):
        self.launch()
        ids = [self.new_session() for _ in range(3)]
        logs = [self.client.app.state.sessions[sid].agent.session_log for sid in ids]
        project_file = self.root / 'keep.txt'
        project_file.write_text('project output', encoding='utf-8')
        revision = self.archive(ids[0])
        response = self.delete([ids[0]], revision)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['deleted'], ids[:1])
        self.assertFalse(logs[0].path.exists())
        logs[0].event('late')
        self.assertFalse(logs[0].path.exists())
        self.assertFalse((self.state_dir / 'sessions' / (ids[0]+'.json')).exists())
        self.assertEqual(self.delete([ids[0]], revision).status_code, 200)
        self.assertTrue(logs[1].path.exists())
        self.assertNotIn(ids[0], response.json()['state']['sessions'])
        revision = self.archive(ids[1])
        self.assertEqual(self.delete([ids[1]], revision).json()['deleted'], ids[1:2])
        self.assertEqual(project_file.read_text(), 'project output')
        self.client.__exit__(None, None, None)
        self.start('error: must not execute\n', state_dir=self.state_dir)
        self.assertNotIn(ids[0], self.client.app.state.sessions)
        self.assertNotIn(ids[1], self.client.app.state.sessions)
        self.assertIn(ids[2], self.client.app.state.sessions)

    def test_batch_preflight_rejects_busy_unarchived_stale_and_lost_owner(self):
        self.launch()
        a, b, c = [self.new_session() for _ in range(3)]
        self.archive(a)
        revision = self.archive(b)
        session = self.client.app.state.sessions[b]
        for attr in ('busy', 'owner_uncertain', 'management_busy'):
            setattr(session, attr, True)
            result = self.delete([a, b], revision)
            self.assertEqual(result.status_code, 409, result.text)
            self.assertEqual(result.json()['detail']['code'], 'session_delete_busy')
            self.assertIn(a, self.client.app.state.sessions)
            setattr(session, attr, False)
        self.assertEqual(self.delete([a, c], revision).status_code, 409)
        self.assertEqual(self.delete([a, b], revision-1).status_code, 409)
        self.client.app.state.execution_owner.closed = True
        self.assertEqual(self.delete([a, b], revision).status_code, 503)
        self.client.app.state.execution_owner.closed = False
        result = self.delete([a, b], revision)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['deleted'], [a, b])
        self.assertIn(c, self.client.app.state.sessions)

    def test_auth_body_and_disk_failure_are_not_reported_as_success(self):
        self.launch()
        a, b = [self.new_session() for _ in range(2)]
        self.archive(a)
        revision = self.archive(b)
        body = {'session_ids': [a, b], 'revision': revision}
        self.assertEqual(self.client.post('/desktop/archived-sessions/delete', json=body).status_code, 401)
        for ids in ([], [a, a], ['../escape'], [{}]):
            result = self.delete(ids, revision)
            self.assertIn(result.status_code, [200, 409, 422])
            self.assertIn(a, self.client.app.state.sessions)
        log = self.client.app.state.sessions[b].agent.session_log
        with patch.object(log, 'delete_history', side_effect=OSError('fixture disk error')):
            result = self.delete([a, b], revision)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['deleted'], [a])
        self.assertTrue(result.json()['incomplete'])
        self.assertTrue(log.path.exists())
        self.assertTrue((self.state_dir / 'sessions' / (b+'.json')).exists())
        self.assertTrue(json.loads(self.workspace_path.read_text())['sessions'][b]['archived'])
