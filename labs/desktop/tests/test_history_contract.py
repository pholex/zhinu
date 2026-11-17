"""Desktop HTTP history survives real backend exit, crash and event-window loss.

Only the model is scripted. The native child's authenticated HTTP, session logs,
tool approvals, file artifacts and browser client are used without replacement.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest

from labs.desktop.tests import test_macos_backend as backend_tests

ROOT = Path(__file__).resolve().parents[3]
CLIENT = (ROOT / "labs/desktop/web/client.mjs").as_uri()


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("node"),
                     "macOS backend and Node browser client are required")
class HistoryContractTests(unittest.TestCase):
    # Reuse process ownership and isolation helpers, without inheriting its tests.
    setUp = backend_tests.DesktopBackendTests.setUp
    cleanup_children = backend_tests.DesktopBackendTests.cleanup_children
    start = backend_tests.DesktopBackendTests.start
    ready = backend_tests.DesktopBackendTests.ready
    request = backend_tests.DesktopBackendTests.request
    stop = backend_tests.DesktopBackendTests.stop

    def open_workspace(self):
        workspace = self.root / "history 项目"
        workspace.mkdir(exist_ok=True)
        child = self.start("--workspace", str(workspace))
        return workspace, child, self.ready(child)

    def api(self, info, path, *, method="GET", body=None, expected=200):
        code, _, raw = self.request(info, path, method=method, body=body)
        self.assertEqual(code, expected, raw.decode("utf-8", errors="replace"))
        return json.loads(raw)

    def create(self, info, script):
        (self.root / "script.txt").write_text(script, encoding="utf-8")
        return self.api(info, "/session", method="POST",
                        body={"model": "lab-scripted"})["session_id"]

    @staticmethod
    def write_script(path, content):
        return "tool_call: " + json.dumps({"name": "write_file", "arguments": {
            "path": path, "content": content}}, ensure_ascii=False) + "\n"

    def wait_for(self, info, sid, predicate):
        deadline = time.monotonic() + 15
        state = None
        while time.monotonic() < deadline:
            state = self.api(info, f"/session/{sid}/status")
            if predicate(state):
                return state
            time.sleep(.02)
        self.fail(f"Session did not settle: {state}")

    def submit(self, info, sid, text, *, approve=False):
        self.api(info, f"/session/{sid}/prompt_async", method="POST",
                 body={"text": text}, expected=202)
        if approve:
            pending = self.wait_for(info, sid, lambda s: bool(s["pending_approvals"]))
            self.api(info, f"/session/{sid}/permissions", method="POST", body={
                "request_id": pending["pending_approvals"][0]["request_id"],
                "decision": "allow"})
        return self.wait_for(info, sid, lambda s: not s["busy"])

    def pages(self, info, sid, resource):
        field = "messages" if resource == "history" else "runs"
        cursor, collected, seen = 0, [], set()
        while True:
            self.assertNotIn(cursor, seen, "pagination cursor did not advance")
            seen.add(cursor)
            page = self.api(info, f"/session/{sid}/{resource}?cursor={cursor}&limit=1")
            self.assertTrue(page["persisted"], page.get("warning"))
            collected.extend(page[field])
            if page["next_cursor"] is None:
                self.assertEqual(len(collected), page["total"])
                break
            cursor = page["next_cursor"]
        ids = [item["id"] for item in collected]
        self.assertEqual(len(ids), len(set(ids)), "pagination repeated a record")
        return collected

    def browser(self, info, sid):
        source = (
            f'import {{ReadClient,stateOf}} from {json.dumps(CLIENT)};'
            'let raw="";for await(const chunk of process.stdin)raw+=chunk;'
            'const {info,sid}=JSON.parse(raw);const c=new ReadClient(new URL(info.url).origin,info.token);'
            'const session=await c.get(`/session/${sid}`);'
            'const history=await c.get(`/session/${sid}/history?cursor=0&limit=100`);'
            'const runs=await c.get(`/session/${sid}/runs?cursor=0&limit=50`);'
            'console.log(JSON.stringify({state:stateOf(session),history,runs}));'
        )
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", source],
                                input=json.dumps({"info": info, "sid": sid}),
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_two_deliveries_and_paginated_history_survive_native_restart(self):
        workspace, child, info = self.open_workspace()
        self.assertEqual(self.api(info, "/health")["history_protocol"], 1)
        # Many deltas make the 200-event desktop view insufficient for full history.
        chunks = "".join(f"text: 片段{i}；\n" for i in range(240))
        sid = self.create(info, self.write_script("first.md", "第一轮成果")
                          + "---\n" + chunks + "text: 第一份已交付。\n---\n"
                          + self.write_script("second.md", "第二轮成果")
                          + "---\ntext: 第二份已交付。\n")
        self.submit(info, sid, "生成第一份报告", approve=True)
        self.submit(info, sid, "继续生成第二份报告", approve=True)
        before = self.api(info, f"/session/{sid}")
        self.assertIn("第一份报告", before["title"])
        messages = self.pages(info, sid, "history")
        self.assertEqual([m["content"] for m in messages if m["role"] == "user"],
                         ["生成第一份报告", "继续生成第二份报告"])
        self.assertIn("片段0；", "".join(m["content"] for m in messages))
        runs = self.pages(info, sid, "runs")
        self.assertEqual([run["status"] for run in runs], ["completed", "completed"])
        self.assertEqual([run["result"]["artifacts"][0]["path"] for run in runs],
                         ["first.md", "second.md"])
        self.assertEqual((workspace / "first.md").read_text(encoding="utf-8"), "第一轮成果")
        self.assertEqual((workspace / "second.md").read_text(encoding="utf-8"), "第二轮成果")
        # Event loss is deliberate and must not truncate the persisted conversation.
        status = self.api(info, f"/session/{sid}/status")
        page = self.api(info, f"/session/{sid}/events?from={max(1, status['next_seq'] - 200)}&limit=200")
        self.assertLessEqual(len(page["events"]), 200)
        self.assertFalse(any(event.get("kind") == "text.delta" and event.get("text") == "片段0；"
                             for event in page["events"]))
        self.stop(child)
        _, restarted, next_info = self.open_workspace()
        self.assertNotEqual(info["token"], next_info["token"])
        after = self.api(next_info, f"/session/{sid}")
        self.assertEqual(after["title"], before["title"])
        self.assertEqual(after["last_result"], before["last_result"])
        self.assertEqual(self.pages(next_info, sid, "history"), messages)
        self.assertEqual(self.pages(next_info, sid, "runs"), runs)
        view = self.browser(next_info, sid)
        self.assertEqual(view["state"]["label"], "已完成")
        self.assertEqual(view["history"]["messages"], messages)
        self.assertEqual(view["runs"]["runs"], runs)
        self.stop(restarted)

    def test_failed_turn_remains_failed_after_native_restart(self):
        _, child, info = self.open_workspace()
        sid = self.create(info, "error: scripted history failure\n")
        self.submit(info, sid, "执行会失败的检查")
        self.stop(child)
        _, restarted, next_info = self.open_workspace()
        runs = self.pages(next_info, sid, "runs")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "failed")
        self.assertEqual(self.browser(next_info, sid)["state"]["label"], "执行失败")
        self.stop(restarted)

    def test_crash_while_waiting_for_approval_never_becomes_completed(self):
        workspace, child, info = self.open_workspace()
        sid = self.create(info, self.write_script("unapproved.md", "不得生成")
                          + "---\ntext: 不得显示成功。\n")
        self.api(info, f"/session/{sid}/prompt_async", method="POST",
                 body={"text": "等待审批时模拟进程消失"}, expected=202)
        self.wait_for(info, sid, lambda s: bool(s["pending_approvals"]))
        child.kill()
        child.wait(timeout=5)
        _, restarted, next_info = self.open_workspace()
        runs = self.pages(next_info, sid, "runs")
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["status"], "interrupted_by_restart")
        self.assertFalse((workspace / "unapproved.md").exists())
        state = self.api(next_info, f"/session/{sid}/status")
        self.assertFalse(state["pending_approvals"])
        self.assertIsNone(state["last_result"])
        self.assertEqual(self.browser(next_info, sid)["state"]["label"], "重启中断")
        self.stop(restarted)

    def test_drafts_and_submission_identity_survive_origin_change_without_replay(self):
        _, child, info = self.open_workspace()
        sid = self.create(info, "text: 此模型不应被自动调用。\n")
        initial = self.api(info, "/desktop/state")
        action = {"kind": "prompt", "sid": sid, "path": f"/session/{sid}/prompt_async",
                  "body": {"text": "保留这份原文，等待用户核对后重试"},
                  "key": "desktop-restart-original-submission", "state": "sending"}
        scope = f"{sid}:prompt"
        saved = {"revision": initial["revision"], "drafts": {sid: "跨启动草稿"},
                 "outbox": {scope: action}}
        revision = self.api(info, "/desktop/state", method="POST", body=saved)["revision"]
        self.assertEqual(self.request(info, "/desktop/state", auth=False)[0], 401)
        self.stop(child)
        _, restarted, next_info = self.open_workspace()
        self.assertNotEqual(info["token"], next_info["token"])
        restored = self.api(next_info, "/desktop/state")
        self.assertEqual(restored, {**saved, "revision": revision})
        source = (
            f'import {{ReadClient,LocalState,Outbox}} from {json.dumps(CLIENT)};'
            'let raw="";for await(const chunk of process.stdin)raw+=chunk;'
            'const {info,sid}=JSON.parse(raw);const client=new ReadClient(new URL(info.url).origin,info.token);'
            'const local=await LocalState.load(client);const outbox=new Outbox(local.storage,"restored-workspace");'
            'console.log(JSON.stringify({draft:local.getDraft(sid),items:outbox.items}));'
        )
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", source],
                                input=json.dumps({"info": next_info, "sid": sid}), capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=10, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        observed = json.loads(result.stdout)
        self.assertEqual(observed["draft"], "跨启动草稿")
        queued = observed["items"][scope]
        self.assertEqual(queued["key"], action["key"])
        self.assertEqual(queued["body"], action["body"])
        self.assertEqual(queued["state"], "unknown")
        self.assertEqual(self.request(next_info, f"/session/{sid}/runs")[0], 404)
        self.assertEqual(self.request(next_info, f"/session/{sid}/status")[0], 404)
        self.assertEqual(self.api(next_info, "/session")["sessions"], [])
        self.stop(restarted)


if __name__ == "__main__":
    unittest.main()
