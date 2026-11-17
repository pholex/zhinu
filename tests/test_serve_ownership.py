from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from xiaoyu import serve_ownership as owner
from xiaoyu.session_log import SessionLog, SessionLockedError
from tests.test_serve import ServeCase


class StrictLogTests(unittest.TestCase):
    def test_platform_failure_never_creates_unlocked_log_cli_policy_unchanged(self):
        with tempfile.TemporaryDirectory() as directory, patch("xiaoyu.session_log._try_lock", side_effect=OSError("unsupported")):
            root = Path(directory)
            with self.assertRaises(OSError):
                owner.StrictSessionLog.create("fixture", str(root), directory=root, session_id="strict")
            self.assertFalse(list(root.glob("*strict.jsonl")))
            ordinary = SessionLog.create("fixture", str(root), directory=root, session_id="ordinary")
            self.assertFalse(ordinary.locked)
            self.assertTrue(ordinary.path.is_file())
            ordinary.close()

    def test_cross_process_state_and_log_ownership(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = '''import sys,time
from pathlib import Path
from xiaoyu.session_log import SessionWriteLock,SessionLog
root=Path(sys.argv[1]); state=SessionWriteLock(root/'serve-owner'); log=SessionLog(root/'held.jsonl')
print('ready',flush=True); sys.stdin.readline()
'''
            process = subprocess.Popen([sys.executable, "-P", "-c", script, str(root)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=os.environ)
            try:
                self.assertEqual(process.stdout.readline().strip(), "ready")
                with self.assertRaises(SessionLockedError):
                    owner.StateOwner(root, True)
                with self.assertRaises(SessionLockedError):
                    owner.StrictSessionLog(root / "held.jsonl")
            finally:
                process.communicate("exit\n", timeout=5)
            state = owner.StateOwner(root, True)
            log = owner.StrictSessionLog(root / "held.jsonl")
            log.close(); state.close()


class ServiceOwnershipTests(ServeCase):
    mcp = False
    token = "ownership-fixture"

    def test_state_lifecycle_exclusive_and_idle_release(self):
        state = Path(self.tmp) / "state"
        self.start("text: done\n", state_dir=state)
        with self.assertRaises(SessionLockedError):
            owner.StateOwner(state, True)
        self.client.__exit__(None, None, None)
        lease = owner.StateOwner(state, True)
        lease.close()

    def test_log_release_blocks_execution_allow_steer_budget_but_allows_safety(self):
        self.start("text: must not execute\n")
        sid = self.new_session()
        session = self.client.app.state.sessions[sid]
        from xiaoyu.serve import _Pending
        approval = _Pending(id="pending", name="write_file", args={"path": "x", "content": "y"}, created_at=time.time())
        session.add_pending(approval)
        session.agent.session_log.release()
        rejected = self.client.post(f"/session/{sid}/permissions", headers=self.headers(), json={"request_id": "pending", "decision": "allow"})
        self.assertEqual(rejected.status_code, 503)
        self.assertFalse(approval.done.is_set())
        denied = self.client.post(f"/session/{sid}/permissions", headers=self.headers(), json={"request_id": "pending", "decision": "deny"})
        self.assertEqual(denied.status_code, 200)
        self.assertTrue(approval.done.is_set())
        for endpoint, body in [("prompt_async", {"text": "no"}), ("steer", {"text": "no"}), ("budget", {"budget": None})]:
            response = self.client.post(f"/session/{sid}/{endpoint}", headers=self.headers(), json=body)
            self.assertEqual(response.status_code, 503, response.text)
            self.assertEqual(response.json()["detail"]["code"], "execution_owner_unavailable")
        self.assertFalse(self.status(sid)["writable"])
        self.assertEqual(session.turns, 0)
        self.assertEqual(self.client.post(f"/session/{sid}/abort", headers=self.headers(), json={}).status_code, 200)
        self.assertEqual(self.client.delete(f"/session/{sid}", headers=self.headers()).status_code, 200)

    def test_new_log_lock_failure_returns_503_no_execution(self):
        self.start("text: must not execute\n")
        with patch("xiaoyu.session_log._try_lock", side_effect=OSError("unsupported")):
            response = self.client.post("/session", headers=self.headers(), json={})
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.client.app.state.sessions, {})

    def test_failed_restore_preserves_manifest_and_creation_key(self):
        state = Path(self.tmp) / "state"
        self.start("text: done\n", state_dir=state)
        first = self.client.post("/session", headers={**self.headers(), "Idempotency-Key": "create-once"}, json={})
        sid = first.json()["session_id"]
        log_path = self.client.app.state.sessions[sid].agent.session_log.path
        self.client.__exit__(None, None, None)
        held = owner.StrictSessionLog(log_path)
        self.addCleanup(held.release)
        self.start("text: must not execute\n", state_dir=state)
        self.assertNotIn(sid, self.client.app.state.sessions)
        self.assertTrue((state / "sessions" / (sid + ".json")).is_file())
        health = self.client.get("/health", headers=self.headers()).json()
        self.assertEqual(health["recovery_errors"][0]["session_id"], sid)
        retry = self.client.post("/session", headers={**self.headers(), "Idempotency-Key": "create-once"}, json={})
        self.assertEqual(retry.status_code, 409)

    def test_state_loss_blocks_new_writes_and_new_session(self):
        self.start("text: must not execute\n")
        sid = self.new_session()
        self.client.app.state.execution_owner.close()
        response = self.client.post(f"/session/{sid}/prompt_async", headers=self.headers(), json={"text": "no"})
        self.assertEqual(response.status_code, 503)
        response = self.client.post("/session", headers=self.headers(), json={})
        self.assertEqual(response.status_code, 503)

    def test_management_gate_blocks_new_prompt_until_worker_leaves(self):
        self.start("text: completed\n")
        sid = self.new_session()
        session = self.client.app.state.sessions[sid]
        session.management_busy = True
        response = self.client.post(f"/session/{sid}/prompt_async", headers=self.headers(), json={"text": "no"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(session.turns, 0)
        session.management_busy = False
        response = self.client.post(f"/session/{sid}/prompt", headers=self.headers(), json={"text": "yes"})
        self.assertEqual(response.status_code, 200)

    def test_uncertain_worker_blocks_only_its_session(self):
        self.start("text: completed\n")
        sid = self.new_session()
        session = self.client.app.state.sessions[sid]
        session.owner_uncertain = True
        response = self.client.post(f"/session/{sid}/prompt_async", headers=self.headers(), json={"text": "no"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("进程", self.status(sid)["write_blocked_reason"])
        other = self.new_session()
        response = self.client.post(f"/session/{other}/prompt", headers=self.headers(), json={"text": "yes"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.status(other)["turns"], 1)

    def test_plain_stop_reconciles_real_completion_and_can_continue(self):
        self.start('tool_call: {"name":"write_file","arguments":{"path":"never.txt","content":"no"}}\n---\ntext: resumed\n')
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt_async", headers=self.headers(), json={"text": "start"})
        self._wait_for(sid, "waiting_for_approval")
        self.client.post(f"/session/{sid}/abort", headers=self.headers(), json={})
        self._wait_for(sid, "interrupted")
        self.assertTrue(self.status(sid)["writable"])
        response = self.client.post(f"/session/{sid}/prompt", headers=self.headers(), json={"text": "continue"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.status(sid)["turns"], 2)

    def test_successful_delegation_then_plain_stop_can_continue(self):
        from tests import test_serve_commands as commands
        sid = commands.CommandTests.boot(self, f"text: {commands.LONG}\n---\ntext: {commands.LONG}\n---\n"
            'tool_call: {"name":"write_file","arguments":{"path":"never.txt","content":"no"}}\n---\ntext: resumed\n')
        body = commands.CommandTests.preview(self, sid, {"spec": "reader", "prompt_template": "{{item}}", "items": ["甲", "乙"]})
        reply = commands.CommandTests.submit(self, sid, body)
        self.assertEqual(reply.status_code, 202)
        state = commands.CommandTests.wait(self, sid)
        self.assertEqual(state["last_result"]["stopped"], "done", state)
        session = self.client.app.state.sessions[sid]
        self.assertTrue(session.owner_fanout)
        self.client.post(f"/session/{sid}/prompt_async", headers=self.headers(), json={"text": "plain"})
        self._wait_for(sid, "waiting_for_approval")
        self.client.post(f"/session/{sid}/abort", headers=self.headers(), json={})
        self._wait_for(sid, "interrupted")
        self.assertTrue(self.status(sid)["writable"])
        reply = self.client.post(f"/session/{sid}/prompt", headers=self.headers(), json={"text": "continue"})
        self.assertEqual(reply.status_code, 200, reply.text)
        self.assertEqual(self.status(sid)["turns"], 3)

    def test_delegation_exception_retains_unknown_but_plain_failure_does_not(self):
        from xiaoyu.events import ToolRunning
        self.start("text: unused\n")
        for delegated in (False, True):
            sid = self.new_session()
            session = self.client.app.state.sessions[sid]
            async def broken_stream(*args, **kwargs):
                if delegated:
                    yield ToolRunning(name="qixiang", args={})
                raise RuntimeError("fixture failure")
            with patch.object(session.async_agent, "stream", side_effect=broken_stream):
                reply = self.client.post(f"/session/{sid}/prompt", headers=self.headers(), json={"text": "start"})
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(self.status(sid)["detail"], "failed")
            self.assertEqual(session.owner_uncertain, delegated)
            self.assertEqual(self.status(sid)["writable"], not delegated)

    def test_actual_worker_uncertainty_retains_state_and_log_until_process_exit(self):
        state = Path(self.tmp) / "child-state"
        script = r"""
import importlib.util,sys,threading,asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tests.test_serve import ServeCase
case=ServeCase();case.setUp();case.token='fixture';case.mcp=False;case.start('text: never\n',state_dir=Path(sys.argv[1]))
sid=case.new_session();session=case.client.app.state.sessions[sid]
gate=threading.Event();entered=threading.Event()
def worker(): entered.set();gate.wait(15)
executor=ThreadPoolExecutor(max_workers=1)
future=executor.submit(worker)
async def begin():
    session._task=asyncio.wrap_future(future)
case.client.portal.call(begin);entered.wait(3)
session.busy=True;session.interrupt_owned();session.busy=False
case.client.portal.call(session._task.cancel)
case.client.__exit__(None,None,None)
print(str(session.agent.session_log.path),flush=True)
sys.stdin.readline();gate.set();executor.shutdown(wait=True)
print('released-worker',flush=True)
sys.stdin.readline()
"""
        process = subprocess.Popen([sys.executable, "-P", "-c", script, str(state)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", env=os.environ)
        try:
            log_path = Path(process.stdout.readline().strip())
            self.assertTrue(log_path.is_file())
            with self.assertRaises(SessionLockedError):
                owner.StateOwner(state, True)
            with self.assertRaises(SessionLockedError):
                owner.StrictSessionLog(log_path)
            process.stdin.write("finish\n");process.stdin.flush()
            self.assertEqual(process.stdout.readline().strip(), "released-worker")
            with self.assertRaises(SessionLockedError):
                owner.StateOwner(state, True)
        finally:
            try:
                out, error = process.communicate("exit\n", timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                out, error = process.communicate(timeout=3)
                self.fail("ownership fixture did not exit: " + error)
        self.assertEqual(process.returncode, 0, error)
        recovered = owner.StateOwner(state, True)
        recovered.close()


if __name__ == "__main__":
    unittest.main()
