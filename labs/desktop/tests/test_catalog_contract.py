"""A CLI history branches through the real desktop client without changing its source."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from labs.desktop.tests import test_macos_backend as backend_tests

ROOT = Path(__file__).resolve().parents[3]
CLIENT = (ROOT / "labs/desktop/web/client.mjs").as_uri()


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("node"),
                     "macOS backend and Node browser client are required")
class CatalogContractTests(unittest.TestCase):
    setUp = backend_tests.DesktopBackendTests.setUp
    cleanup_children = backend_tests.DesktopBackendTests.cleanup_children
    start = backend_tests.DesktopBackendTests.start
    ready = backend_tests.DesktopBackendTests.ready
    request = backend_tests.DesktopBackendTests.request
    stop = backend_tests.DesktopBackendTests.stop

    def test_cli_branch_retries_then_executes_without_mutating_source_history(self):
        child = self.start("--fixture")
        info = self.ready(child)
        source = f'import {{ReadClient,LocalState,Outbox,stateOf}} from {json.dumps(CLIENT)};\n' + r'''
import assert from "node:assert/strict";
let raw = ""; for await (const chunk of process.stdin) raw += chunk;
const info = JSON.parse(raw);
const client = new ReadClient(new URL(info.url).origin, info.token);
const health = await client.get("/health");
assert.equal(health.catalog_protocol, 1);
const original = await client.get("/session");
assert.equal(original.sessions.length, 4);
const catalog = await client.get("/catalog/sessions?cursor=0&limit=100");
assert.equal(catalog.sessions.length, 1);
const entry = catalog.sessions[0];
assert.equal(entry.source, "cli");
assert.equal(entry.can_fork, true);
assert.equal(entry.locked, false);
const historyPath = `/catalog/sessions/${encodeURIComponent(entry.catalog_id)}/history?cursor=0&limit=100`;
const before = await client.get(historyPath);
assert.equal(before.persisted, true);
assert.equal(before.can_fork, true);
assert.deepEqual(before.messages.map(m => m.role), ["user", "assistant"]);
const local = await LocalState.load(client);
const outbox = new Outbox(local.storage, "native");
const body = {fork_from:{catalog_id:entry.catalog_id,revision:before.revision}};
const item = outbox.prepare("create", {kind:"create",path:"/session",body,instance:health.instance_id});
const key = item.key;
const duplicate = outbox.prepare("create", {kind:"create",path:"/session",body,instance:health.instance_id});
assert.equal(duplicate.key, key);
const firstPromise = outbox.send("create", client);
assert.equal(await outbox.send("create", client), null, "double click must not submit twice");
const created = await firstPromise;
assert.equal(created.fork_origin.catalog_id, entry.catalog_id);
assert.equal(created.fork_origin.revision, before.revision);
assert.equal(created.workspace, entry.workspace);
assert.equal(created.turns, 0);
// Simulate a lost response using the original persisted receipt, never a fresh key.
item.state = "unknown"; outbox.save(); await local.flush();
const retried = await outbox.send("create", client);
assert.equal(item.key, key);
assert.equal(retried.session_id, created.session_id);
assert.equal((await client.get("/session")).sessions.length, 5);
const forkHistory = await client.get(`/session/${created.session_id}/history?cursor=0&limit=100`);
const content = page => page.messages.map(m => ({role:m.role,content:m.content}));
assert.deepEqual(content(forkHistory), content(before));
assert.deepEqual(await client.get(historyPath), before);
outbox.forget("create"); await local.flush();

const sid = created.session_id;
const promptScope = `${sid}:prompt`;
outbox.prepare(promptScope, {kind:"prompt",sid,path:`/session/${sid}/prompt_async`,
  body:{text:"基于终端任务背景，生成这次分支的成果文件。"},instance:health.instance_id});
await outbox.send(promptScope, client);
async function waitFor(predicate) {
  for (let i=0;i<500;i++) {
    const state = await client.get(`/session/${sid}/status`);
    if (predicate(state)) return state;
    await new Promise(resolve => setTimeout(resolve,20));
  }
  throw new Error("branch did not reach the expected execution state");
}
const pending = await waitFor(state => state.pending_approvals.length > 0);
assert.equal(stateOf(pending).label, "等待审批");
const request = pending.pending_approvals[0];
const approvalScope = `${sid}:approval:${request.request_id}`;
outbox.prepare(approvalScope,{kind:"approval",sid,path:`/session/${sid}/permissions`,
  body:{request_id:request.request_id,decision:"allow"},instance:health.instance_id});
await outbox.send(approvalScope,client);
const finished = await waitFor(state => !state.busy);
assert.equal(stateOf(finished).label,"已完成");
assert.equal(finished.turns,1);
const runs = await client.get(`/session/${sid}/runs?cursor=0&limit=50`);
assert.equal(runs.runs.length,1);
assert.equal(runs.runs[0].status,"completed");
assert.equal(runs.runs[0].result.artifacts[0].path,"task-result.md");
assert.deepEqual(await client.get(historyPath),before,"executing the branch must not rewrite source history");
const catalogAfter = await client.get("/catalog/sessions?cursor=0&limit=100");
assert.equal(catalogAfter.sessions[0].revision,before.revision);

// The existing fixture uses the real batch tool; it is not a canned GUI snapshot.
let batches = [];
for (const session of original.sessions) {
  const page = await client.get(`/session/${session.session_id}/weaves?cursor=0&limit=20`);
  batches.push(...page.batches);
}
assert.equal(batches.length,1);
assert.equal(batches[0].final,true);
assert.deepEqual(batches[0].items.map(item=>item.status),["completed","completed"]);
assert.ok(batches[0].items.every(item=>item.run_id && item.answer));
await local.flush();
console.log(JSON.stringify({workspace:created.workspace,artifact:runs.runs[0].result.artifacts[0].path,
  sessions:(await client.get("/session")).sessions.length,sourceRevision:before.revision}));
'''
        observed = subprocess.run([shutil.which("node"), "--input-type=module", "-e", source],
                                  input=json.dumps(info), capture_output=True, text=True,
                                  encoding="utf-8", errors="replace", timeout=30, check=False)
        self.assertEqual(observed.returncode, 0, observed.stderr)
        result = json.loads(observed.stdout)
        self.assertEqual(result["sessions"], 5)
        self.assertIn("脚本模型验证已完成", (Path(result["workspace"]) / result["artifact"]).read_text(
            encoding="utf-8", errors="replace"))
        self.stop(child, info)


if __name__ == "__main__":
    unittest.main()
