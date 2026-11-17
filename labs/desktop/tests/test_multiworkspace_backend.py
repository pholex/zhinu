"""Two owned native services must isolate execution and survive a peer's restart."""
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
WRITE_SCRIPT = ('tool_call: {"name":"write_file","arguments":'
                '{"path":"result.txt","content":"approved in this workspace"}}\n'
                '---\ntext: Finished\n')


@unittest.skipUnless(sys.platform == "darwin", "macOS desktop lifecycle")
class MultiworkspaceBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="desktop-multi-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.children = []
        self.addCleanup(self.cleanup_children)
        self.a = self.root / "工作台 A"
        self.b = self.root / "工作台 B"
        self.a.mkdir()
        self.b.mkdir()

    def cleanup_children(self):
        for child, errors in self.children:
            if not child.stdin.closed:
                child.stdin.close()
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=3)
            child.stdout.close()
            errors.close()

    def start(self, workspace, script="text: Finished\n"):
        # Both processes share a user home, as real application windows do.
        # No inherited provider credentials or user integrations enter this fixture.
        env = {key: os.environ[key] for key in ("PATH", "TMPDIR") if key in os.environ}
        env.update(HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / "config"),
                   PYTHONPATH=str(ROOT), PYTHONNOUSERSITE="1", XIAOYU_SUGGESTIONS="0")
        for feature in ("MCP", "PLUGINS", "SKILLS", "HOOKS", "AGENTS", "EXPLORE",
                        "WEB_SEARCH", "X_SEARCH", "DEEP_RESEARCH", "BROWSER", "ASKPASS"):
            env[f"XIAOYU_ENABLE_{feature}"] = "0"
        script_path = self.root / f"script-{len(self.children)}.txt"
        script_path.write_text(script, encoding="utf-8")
        env["XIAOYU_SCRIPTED_SCRIPTS"] = str(script_path)
        errors = tempfile.TemporaryFile()
        child = subprocess.Popen(
            [sys.executable, "-P", "-u", "-m", "labs.desktop.macos.backend",
             "--workspace", str(workspace)], cwd=ROOT, env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
        self.children.append((child, errors))
        return child

    def ready(self, child):
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            self.assertTrue(selector.select(20), "readiness timeout")
        line = child.stdout.readline()
        if not line:
            errors = next(log for process, log in self.children if process is child)
            errors.seek(0)
            self.fail(errors.read().decode("utf-8", errors="replace"))
        info = json.loads(line)
        self.assertEqual(info["pid"], child.pid)
        self.assertEqual(urlsplit(info["url"]).hostname, "127.0.0.1")
        return info

    def request(self, info, path, *, method="GET", body=None, token=None, expected=200):
        headers = {"Authorization": "Bearer " + (token if token is not None else info["token"])}
        if body is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(body)
        with closing(http.client.HTTPConnection("127.0.0.1", urlsplit(info["url"]).port, timeout=4)) as conn:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            raw = response.read()
            self.assertEqual(response.status, expected, raw.decode("utf-8", errors="replace"))
            return json.loads(raw) if raw else None

    def session(self, info):
        return self.request(info, "/session", method="POST", body={"model": "lab-scripted"})["session_id"]

    def wait_status(self, info, sid, predicate):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = self.request(info, f"/session/{sid}/status")
            if predicate(state):
                return state
            time.sleep(.025)
        self.fail(f"session did not reach expected state: {state}")

    def stop(self, child, info):
        child.stdin.close()
        self.assertEqual(child.wait(timeout=12), 0)
        with self.assertRaises(OSError):
            self.request(info, "/session")

    def test_default_project_exists_before_chat_and_collects_new_sessions(self):
        workspace = self.root / "Documents" / "羽案" / "Default"
        workspace.mkdir(parents=True)
        info = self.ready(self.start(workspace))
        catalog = self.request(info, "/desktop/workspace")["catalogue"]
        self.assertEqual(len(catalog["projects"]), 1)
        project = catalog["projects"][0]
        self.assertEqual(Path(project["workspace"]).name, "Default")
        self.assertEqual(catalog["sessions"], {})
        first, second = self.session(info), self.session(info)
        updated = self.request(info, "/desktop/workspace")["catalogue"]
        self.assertEqual(updated["sessions"], {first: project["project_id"], second: project["project_id"]})
        self.assertEqual(updated["projects"], catalog["projects"])

    def test_simultaneous_services_isolate_auth_sessions_drafts_and_execution(self):
        a = self.ready(self.start(self.a, "text: Result A\n"))
        b = self.ready(self.start(self.b, "text: Result B\n"))
        self.assertNotEqual(a["pid"], b["pid"])
        self.assertNotEqual(urlsplit(a["url"]).port, urlsplit(b["url"]).port)
        self.assertNotEqual(a["token"], b["token"])
        self.assertNotEqual(self.request(a, "/health")["instance_id"],
                            self.request(b, "/health")["instance_id"])
        self.request(a, "/session", token=b["token"], expected=401)
        self.request(b, "/session", token=a["token"], expected=401)
        sa, sb = self.session(a), self.session(b)
        self.request(a, f"/session/{sb}", expected=404)
        self.request(b, f"/session/{sa}", expected=404)
        for info, label in ((a, "A"), (b, "B")):
            state = self.request(info, "/desktop/state")
            state["drafts"]["new-task"] = f"Draft {label}"
            self.request(info, "/desktop/state", method="POST", body=state)
        for info, sid, label in ((a, sa, "A"), (b, sb, "B")):
            self.request(info, f"/session/{sid}/prompt_async", method="POST",
                         body={"text": f"Run {label}"}, expected=202)
        for info, sid, label in ((a, sa, "A"), (b, sb, "B")):
            state = self.wait_status(info, sid, lambda s: not s["busy"] and s["last_result"])
            self.assertEqual(state["last_result"]["text"], f"Result {label}")
            self.assertEqual(self.request(info, "/desktop/state")["drafts"], {"new-task": f"Draft {label}"})
            sessions = self.request(info, "/session")["sessions"]
            self.assertEqual([s["session_id"] for s in sessions], [sid])

    def test_restart_of_a_preserves_b_pending_approval_and_does_not_replay_a(self):
        child_a, child_b = self.start(self.a, WRITE_SCRIPT), self.start(self.b, WRITE_SCRIPT)
        a, b = self.ready(child_a), self.ready(child_b)
        sa, sb = self.session(a), self.session(b)
        for info, sid in ((a, sa), (b, sb)):
            self.request(info, f"/session/{sid}/prompt_async", method="POST",
                         body={"text": "Write result after approval"}, expected=202)
        pending_a = self.wait_status(a, sa, lambda s: s["pending_approvals"])
        pending_b = self.wait_status(b, sb, lambda s: s["pending_approvals"])
        identity_b = self.request(b, "/health")["instance_id"]
        self.stop(child_a, a)
        self.assertIsNone(child_b.poll())
        self.assertEqual(self.request(b, "/health")["instance_id"], identity_b)
        still_pending = self.request(b, f"/session/{sb}/status")["pending_approvals"]
        stable = lambda items: [{key: value for key, value in item.items() if key != "waiting_seconds"}
                                for item in items]
        self.assertEqual(stable(still_pending), stable(pending_b["pending_approvals"]))
        restored = self.ready(self.start(self.a, WRITE_SCRIPT))
        self.assertNotEqual(restored["token"], a["token"])
        self.request(restored, "/session", token=a["token"], expected=401)
        state = self.request(restored, f"/session/{sa}/status")
        self.assertFalse(state["busy"])
        self.assertFalse(state["pending_approvals"])
        # The interrupted in-flight turn is finalized during shutdown, not retried.
        self.assertEqual(state["turns"], 1)
        history = self.request(restored, f"/session/{sa}/history")["messages"]
        self.assertEqual([m["content"] for m in history if m["role"] == "user"],
                         ["Write result after approval"])
        # Neither a restored view nor an old approval may restart interrupted work.
        self.request(restored, f"/session/{sa}/permissions", method="POST", expected=404,
                     body={"request_id": pending_a["pending_approvals"][0]["request_id"], "decision": "allow"})
        self.assertFalse((self.a / "result.txt").exists())
        self.request(b, f"/session/{sb}/permissions", method="POST",
                     body={"request_id": pending_b["pending_approvals"][0]["request_id"], "decision": "allow"})
        finished = self.wait_status(b, sb, lambda s: not s["busy"] and s["last_result"])
        self.assertEqual(finished["last_result"]["text"], "Finished")
        self.assertEqual((self.b / "result.txt").read_text(encoding="utf-8", errors="replace"),
                         "approved in this workspace")
        self.assertFalse((self.a / "result.txt").exists())
        self.assertEqual(self.request(b, "/health")["instance_id"], identity_b)

    def test_canonical_directory_lock_rejects_alias_and_releases_only_its_owner(self):
        child_a = self.start(self.a)
        a = self.ready(child_a)
        b = self.ready(self.start(self.b))
        alias = self.root / "alias A"
        alias.symlink_to(self.a, target_is_directory=True)
        for path in (self.a, alias, self.a / ".." / self.a.name):
            with self.subTest(path=str(path)):
                duplicate = self.start(path)
                self.assertNotEqual(duplicate.wait(timeout=10), 0)
                self.assertEqual(duplicate.stdout.read(), b"")
                self.request(a, "/session")
                self.request(b, "/session")
        self.stop(child_a, a)
        replacement = self.ready(self.start(alias))
        self.session(replacement)
        self.assertEqual(self.request(b, "/session")["sessions"], [])


if __name__ == "__main__":
    unittest.main()
