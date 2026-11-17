"""Direct collaboration commands retain policy and durable submission semantics."""
from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

from tests.test_serve import ServeCase

LONG = "已经逐项核对输入并给出了本项完整交付结论。" * 20


class CommandTests(ServeCase):
    mcp = False
    token = "command-test-token"

    def boot(self, script=None, **config):
        self.start(script or f"text: {LONG}\n---\ntext: {LONG}\n", **config)
        os.environ["XIAOYU_ENABLE_AGENTS"] = "1"
        os.environ["XIAOYU_QIXIANG_CONCURRENCY"] = "1"
        self.addCleanup(os.environ.pop, "XIAOYU_QIXIANG_CONCURRENCY", None)
        directory = self.root / ".xiaoyu" / "agents"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "reader.toml").write_text('description = "读取并核对"\nsystem_prompt = "完成本项读取。"\nreadonly = true\ntools = ["read_file"]\n', encoding="utf-8")
        return self.new_session()

    def preview(self, sid, body):
        response = self.client.post(f"/session/{sid}/weaves/preview", headers=self.headers(), json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return {**body, "preview_token": response.json()["preview_token"]}

    def submit(self, sid, body, key="batch-key"):
        return self.client.post(f"/session/{sid}/weaves", headers={**self.headers(), "Idempotency-Key": key}, json=body)

    def wait(self, sid):
        for _ in range(300):
            state = self.client.get(f"/session/{sid}/status", headers=self.headers()).json()
            if not state["busy"]:
                return state
            time.sleep(.01)
        self.fail("batch did not settle")

    def batches(self, sid):
        return self.client.get(f"/session/{sid}/weaves", headers=self.headers()).json()["batches"]

    def test_direct_success_and_same_key_retry(self):
        sid = self.boot()
        caps = self.client.get(f"/session/{sid}/weaves/capabilities", headers=self.headers()).json()
        self.assertTrue(caps["can_start"], caps)
        body = self.preview(sid, {"spec": "reader", "prompt_template": "{{item}}", "items": ["甲", "乙"]})
        first = self.submit(sid, body)
        self.assertEqual(first.status_code, 202, first.text)
        state = self.wait(sid)
        self.assertEqual(state["last_result"]["stopped"], "done", state)
        self.assertEqual(len(self.batches(sid)), 1)
        retry = self.submit(sid, body)
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertTrue(retry.json()["duplicate"])
        self.assertEqual(retry.json()["run_id"], first.json()["run_id"])
        self.assertEqual(self.wait(sid)["turns"], 1)
        self.assertEqual(self.submit(sid, {**body, "items": ["丙", "丁"]}).status_code, 409)

    def test_preflight_entire_selection_and_repeated_resume(self):
        sid = self.boot(f"text: {LONG}\n---\ntext: {LONG}\n---\ntext: {LONG}\n---\ntext: {LONG}\n")
        body = self.preview(sid, {"spec": "reader", "prompt_template": "{{item}}", "items": ["甲", "乙"]})
        self.assertEqual(self.submit(sid, body).status_code, 202)
        self.wait(sid)
        source = self.batches(sid)[0]
        request = {"source_batch_id": source["batch_id"], "item_ids": [source["items"][0]["id"]], "instruction": "继续核对"}
        bad = {**request, "item_ids": [source["items"][0]["id"], "not-in-this-batch"]}
        response = self.client.post(f"/session/{sid}/weaves/preview", headers=self.headers(), json=bad)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(self.batches(sid)), 1)
        for key in ("resume-1", "resume-2"):
            reviewed = self.preview(sid, request)
            response = self.submit(sid, reviewed, key)
            self.assertEqual(response.status_code, 202, response.text)
            self.wait(sid)
            latest = self.batches(sid)[0]
            self.assertEqual(latest["source_batch_id"], source["batch_id"])
            self.assertEqual(latest["source_item_ids"], request["item_ids"])
            self.assertEqual(latest["items"][0]["resume_from"], source["items"][0]["run_id"])
        self.assertEqual(len(self.batches(sid)), 3)
        self.assertEqual(self.batches(sid)[-1], source)

    def test_denied_tool_never_creates_batch(self):
        sid = self.boot("error: model must not be called\n")
        session = self.client.app.state.sessions[sid]
        body = self.preview(sid, {"spec": "reader", "prompt_template": "{{item}}", "items": ["甲", "乙"]})
        from xiaoyu.agent import Deny
        with patch.object(session.agent, "approver", return_value=Deny("明确拒绝")):
            from xiaoyu.permissions import Permissions, parse_rule
            session.agent.permissions = Permissions(self.root, [parse_rule("ask qixiang")])
            response = self.submit(sid, body)
            self.assertEqual(response.status_code, 202, response.text)
            state = self.wait(sid)
        self.assertNotEqual(state["last_result"]["stopped"], "done")
        self.assertEqual(self.batches(sid), [])

    def test_restart_recorded_retry_never_reexecutes(self):
        state_dir = Path(self.tmp) / "state"
        sid = self.boot(state_dir=state_dir)
        body = self.preview(sid, {"spec": "reader", "prompt_template": "{{item}}", "items": ["甲", "乙"]})
        self.assertEqual(self.submit(sid, body).status_code, 202)
        self.wait(sid)
        source = self.batches(sid)
        self.client.__exit__(None, None, None)
        self.start("error: must never run\n", state_dir=state_dir)
        response = self.submit(sid, body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["detail"]["code"], "submission_recorded")
        for batch in source:
            for item in batch["items"]:
                if item.get("execution_id"):
                    item["recovered"] = True
        self.assertEqual(self.batches(sid), source)

    def test_token_and_missing_archive_are_fail_closed(self):
        sid = self.boot()
        body = self.preview(sid, {"spec": "reader", "prompt_template": "{{item}}", "items": ["甲", "乙"]})
        self.assertEqual(self.submit(sid, {**body, "items": ["丙", "丁"]}).status_code, 409)
        self.assertEqual(self.batches(sid), [])
        self.assertEqual(self.submit(sid, body).status_code, 202)
        self.wait(sid)
        batch = self.batches(sid)[0]
        item = batch["items"][0]
        resume = self.preview(sid, {"source_batch_id": batch["batch_id"], "item_ids": [item["id"]], "instruction": "继续"})
        store = self.client.app.state.sessions[sid].agent.subagent_runs
        (store.directory() / f"{item['run_id']}.json").unlink()
        self.assertEqual(self.submit(sid, resume, "resume").status_code, 409)
        self.assertEqual(len(self.batches(sid)), 1)

    def test_host_tool_history_keyless_snapshot(self):
        """Pin the paired transcript generated without a parent model request."""
        import json
        from xiaoyu.permissions import Permissions, parse_rule
        sid = self.boot("error: parent model must not be called\n")
        session = self.client.app.state.sessions[sid]
        session.agent.permissions = Permissions(self.root, [parse_rule("deny qixiang")])
        request = self.preview(sid, {"spec": "reader", "prompt_template": "{{item}}", "items": ["甲", "乙"]})
        self.assertEqual(self.submit(sid, request).status_code, 202)
        self.wait(sid)
        messages = []
        for original in session.agent.messages[1:]:
            message = dict(original)
            if "tool_calls" in message:
                message["tool_calls"] = [{**call, "id": "<HOST_CALL>"} for call in message["tool_calls"]]
            if "tool_call_id" in message:
                message["tool_call_id"] = "<HOST_CALL>"
            messages.append(message)
        actual = json.dumps(messages, ensure_ascii=False, indent=2) + "\n"
        golden = Path(__file__).parent / "fixtures" / "host_tool_history.expected.json"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            golden.write_text(actual, encoding="utf-8")
        self.assertEqual(actual, golden.read_text(encoding="utf-8", errors="replace"))

    def test_budget_and_plan_reject_before_consuming_submission(self):
        sid = self.boot("error: must never execute\n")
        request = {"spec": "reader", "prompt_template": "{{item}}", "items": ["甲", "乙"]}
        session = self.client.app.state.sessions[sid]
        reviewed = self.preview(sid, request)
        with patch.object(session, "check_budget", return_value="test limit"):
            self.assertEqual(self.submit(sid, reviewed).status_code, 409)
        self.assertEqual(session.submission_receipts, {})
        session.agent.set_mode("plan")
        self.assertEqual(self.submit(sid, reviewed).status_code, 409)
        self.assertEqual(session.submission_receipts, {})
        self.assertEqual(self.batches(sid), [])

    def test_simultaneous_same_key_submissions_reserve_only_one_run(self):
        import concurrent.futures
        import threading
        from xiaoyu import serve_commands
        sid = self.boot()
        body = self.preview(sid, {"spec": "reader", "prompt_template": "{{item}}", "items": ["甲", "乙"]})
        prepare = serve_commands.prepare
        barrier = threading.Barrier(2)

        def simultaneous(*args, **kwargs):
            result = prepare(*args, **kwargs)
            barrier.wait(timeout=3)
            return result

        with patch.object(serve_commands, "prepare", side_effect=simultaneous), concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            requests = [pool.submit(self.submit, sid, body) for _ in range(2)]
            responses = [request.result(timeout=5) for request in requests]
        self.assertEqual([response.status_code for response in responses], [202, 202])
        self.assertEqual(len({response.json()["run_id"] for response in responses}), 1)
        self.wait(sid)
        self.assertEqual(len(self.batches(sid)), 1)
