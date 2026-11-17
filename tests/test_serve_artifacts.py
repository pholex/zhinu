"""Authenticated delivered-file inspection through real execution and restart."""
from pathlib import Path
import os
import unittest

from tests.test_serve import ServeCase


@unittest.skipUnless(os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"), "需要安全目录句柄")
class ArtifactEndpointsTests(ServeCase):
    mcp = False
    approval = "allow_all"
    token = "artifact-test-token"

    def deliver(self, **config):
        self.start('tool_call: {"name":"write_file","arguments":{"path":"report.md","content":"delivered\\n"}}\n---\ntext: done\n', **config)
        sid = self.new_session()
        response = self.client.post(f"/session/{sid}/prompt", json={"text": "create report"}, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        artifact = response.json()["result"]["artifacts"][0]
        runs = self.client.get(f"/session/{sid}/runs", headers=self.headers()).json()["runs"]
        self.assertNotIn("artifact_previews", runs[0])
        return sid, artifact, runs[0]["id"]

    def test_delivered_snapshot_survives_current_edit_and_restart(self):
        state = Path(self.tmp) / "state"
        sid, artifact, run = self.deliver(state_dir=state)
        url = f"/session/{sid}/artifacts/{artifact['id']}"
        params = {"run_id": run}
        view = self.client.get(url, params=params, headers=self.headers()).json()
        self.assertEqual(view["version"], "delivered")
        self.assertTrue(view["diff"]["available"])
        (self.root / "report.md").write_text("later edit\n", encoding="utf-8")
        self.client.__exit__(None, None, None)
        self.start("error: must not execute\n", state_dir=state)
        response = self.client.get(url, params=params, headers=self.headers())
        self.assertEqual(response.status_code, 200, response.text)
        view = response.json()
        self.assertEqual(view["version"], "current_changed")
        self.assertEqual(view["delivered"]["text"], "delivered\n")
        self.assertEqual(view["current"]["text"], "later edit\n")
        self.assertIn("+delivered", view["diff"]["text"])
        (self.root / "report.md").unlink()
        view = self.client.get(url, params=params, headers=self.headers()).json()
        self.assertEqual(view["version"], "missing")
        self.assertEqual(view["delivered"]["text"], "delivered\n")
        self.assertFalse(view["can_reveal"])
        self.assertEqual(self.client.get(url + "/location", params=params, headers=self.headers()).status_code, 409)

    def test_lookup_requires_auth_registered_owner_and_safe_location(self):
        sid, artifact, run = self.deliver()
        url = f"/session/{sid}/artifacts/{artifact['id']}"
        params = {"run_id": run}
        self.assertEqual(self.client.get(url, params=params).status_code, 401)
        self.assertEqual(self.client.get(url, headers=self.headers()).status_code, 400)
        self.assertEqual(self.client.get(url, params={"run_id": "wrong"}, headers=self.headers()).status_code, 404)
        other = self.new_session()
        self.assertEqual(self.client.get(url.replace(sid, other), params=params, headers=self.headers()).status_code, 404)
        location = self.client.get(url + "/location", params=params, headers=self.headers())
        self.assertEqual(location.json()["path"], str(self.root / "report.md"))
        (self.root / "report.md").unlink()
        (self.root / "report.md").symlink_to(Path(self.tmp) / "script.txt")
        self.assertEqual(self.client.get(url + "/location", params=params, headers=self.headers()).status_code, 409)
        view = self.client.get(url, params=params, headers=self.headers()).json()
        self.assertFalse(view["can_reveal"])
        self.assertFalse(view["current"]["available"])
        self.assertEqual(view["delivered"]["text"], "delivered\n")
