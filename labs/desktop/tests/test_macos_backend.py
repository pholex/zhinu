"""Exercise the actual child protocol, authenticated HTTP, persistence and EOF shutdown."""
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import selectors
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]


@unittest.skipUnless(sys.platform == "darwin", "macOS desktop lifecycle")
class DesktopBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="desktop-contract-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.children = []
        self.addCleanup(self.cleanup_children)

    def cleanup_children(self):
        for child, errors in self.children:
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            child.stdout.close()
            errors.close()

    def start(self, *args):
        env = {key: os.environ[key] for key in ("PATH", "TMPDIR") if key in os.environ}
        env.update(HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / "config"),
                   PYTHONPATH=str(ROOT), PYTHONNOUSERSITE="1", XIAOYU_SUGGESTIONS="0")
        for feature in ("MCP", "PLUGINS", "SKILLS", "HOOKS", "AGENTS", "EXPLORE", "WEB_SEARCH", "BROWSER"):
            env[f"XIAOYU_ENABLE_{feature}"] = "0"
        script = self.root / "script.txt"
        script.write_text("text: Desktop persistence check\n", encoding="utf-8")
        env["XIAOYU_SCRIPTED_SCRIPTS"] = str(script)
        errors = tempfile.TemporaryFile()
        child = subprocess.Popen([sys.executable, "-P", "-u", "-m", "labs.desktop.macos.backend", *args],
            cwd=ROOT, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        self.children.append((child, errors))
        return child

    def ready(self, child):
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(20), "readiness timeout")
        line = child.stdout.readline()
        if not line:
            errors = next(log for proc, log in self.children if proc is child)
            errors.seek(0)
            self.fail(errors.read().decode("utf-8", errors="replace"))
        info = json.loads(line)
        self.assertEqual(info["pid"], child.pid)
        self.assertEqual(urlsplit(info["url"]).hostname, "127.0.0.1")
        return info

    def request(self, info, path, *, auth=True, method="GET", headers=None, body=None):
        hdr = dict(headers or {})
        if auth:
            hdr["Authorization"] = "Bearer " + info["token"]
        if body is not None:
            hdr["Content-Type"] = "application/json"
            body = json.dumps(body)
        with closing(http.client.HTTPConnection("127.0.0.1", urlsplit(info["url"]).port, timeout=4)) as conn:
            conn.request(method, path, body=body, headers=hdr)
            response = conn.getresponse()
            return response.status, dict(response.headers), response.read()

    def stop(self, child, info=None):
        child.stdin.close()
        self.assertEqual(child.wait(timeout=12), 0)
        if info is not None:
            with self.assertRaises(OSError):
                self.request(info, "/session")

    def test_fixture_ready_auth_assets_and_owner_eof_cleanup(self):
        child = self.start("--fixture")
        info = self.ready(child)
        self.assertNotIn(info["token"], info["url"])
        code, _, raw = self.request(info, "/session")
        self.assertEqual(code, 200)
        sessions = json.loads(raw)["sessions"]
        self.assertEqual(len(sessions), 4)
        batches = []
        for session in sessions:
            status, _, body = self.request(info, f"/session/{session['session_id']}/weaves")
            self.assertEqual(status, 200)
            batches.extend(json.loads(body)["batches"])
        self.assertEqual(len(batches), 1)
        self.assertTrue(batches[0]["final"])
        self.assertTrue(batches[0]["run_id"])
        self.assertEqual([item["status"] for item in batches[0]["items"]], ["completed", "completed"])
        self.assertTrue(all(item["answer"] and item["run_id"] for item in batches[0]["items"]))
        temporary_root = Path(sessions[0]["workspace"]).parents[1]
        self.assertTrue(temporary_root.exists())
        self.assertEqual(self.request(info, "/session", auth=False)[0], 401)
        self.assertEqual(self.request(info, "/session", headers={"Origin": "https://invalid.example"})[0], 403)
        self.assertEqual(self.request(info, "/workbench/", headers={"Host": "invalid.example"})[0], 403)
        code, headers, html = self.request(info, "/workbench/", auth=False)
        self.assertEqual(code, 200)
        self.assertIn("connect-src 'self'", headers["content-security-policy"])
        self.assertNotIn(info["token"].encode(), html)
        self.assertEqual(self.request(info, "/workbench/app.mjs", auth=False)[0], 200)
        self.assertEqual(self.request(info, "/workbench/../../.env", auth=False)[0], 404)
        self.assertEqual(self.request(info, "/workbench/", method="POST")[0], 404)
        self.stop(child, info)
        self.assertFalse(temporary_root.exists())

    def test_workspace_lock_and_restart_persistence(self):
        workspace = self.root / "项目 with spaces"
        workspace.mkdir()
        child = self.start("--workspace", str(workspace))
        info = self.ready(child)
        (self.root / "script.txt").write_text('tool_call: {"name":"write_file","arguments":{"path":"pending.txt","content":"only after approval"}}\n---\ntext: Done\n', encoding="utf-8")
        code, _, body = self.request(info, "/session", method="POST", body={"model": "lab-scripted"})
        self.assertEqual(code, 200, body)
        sid = json.loads(body)["session_id"]
        self.assertEqual(self.request(info, f"/session/{sid}/prompt_async", method="POST", body={"text": "Write"})[0], 202)
        for _ in range(100):
            state = json.loads(self.request(info, f"/session/{sid}/status")[2])
            if state["pending_approvals"]:
                break
            time.sleep(.02)
        self.assertTrue(state["pending_approvals"])
        duplicate = self.start("--workspace", str(workspace))
        self.assertNotEqual(duplicate.wait(timeout=10), 0)
        self.assertEqual(duplicate.stdout.read(), b"")
        self.stop(child, info)
        self.assertFalse((workspace / "pending.txt").exists())
        restored = self.start("--workspace", str(workspace))
        next_info = self.ready(restored)
        self.assertNotEqual(info["token"], next_info["token"])
        code, _, body = self.request(next_info, "/session")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["sessions"][0]["session_id"], sid)
        self.stop(restored, next_info)

    def test_owner_disappears_during_startup(self):
        child = self.start("--fixture")
        self.stop(child)
        self.assertEqual(child.stdout.read(), b"")

    def test_invalid_workspace_exits_without_ready(self):
        child = self.start("--workspace", str(self.root / "missing"))
        self.assertNotEqual(child.wait(timeout=10), 0)
        self.assertEqual(child.stdout.read(), b"")


if __name__ == "__main__":
    unittest.main()
