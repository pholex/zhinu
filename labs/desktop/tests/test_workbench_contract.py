"""Read the real serve responses through the workbench's JavaScript model."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import unittest

from tests.test_serve import ServeCase

ROOT = Path(__file__).resolve().parents[3]
CLIENT = (ROOT / "labs/desktop/web/client.mjs").as_uri()


@unittest.skipUnless(shutil.which("node"), "Node is needed for the browser model contract")
class WorkbenchContract(ServeCase):
    mcp = False

    def consume(self, sid):
        session = self.client.get(f"/session/{sid}", headers=self.headers()).json()
        if session["questions"] is not None:
            session["questionItems"] = self.client.get(f"/session/{sid}/questions", headers=self.headers()).json()["questions"]
        page = self.client.get(f"/session/{sid}/events", headers=self.headers()).json()
        source = (
            f'import {{stateOf, eventWindow, mergeEvents}} from {json.dumps(CLIENT)};'
            'let raw=""; for await (const chunk of process.stdin) raw += chunk;'
            'const {session, page} = JSON.parse(raw);'
            'const view = mergeEvents(eventWindow({}), page);'
            'console.log(JSON.stringify({state:stateOf(session), events:view.events.length, gap:view.gap,'
            'artifacts:session.last_result?.artifacts || []}));'
        )
        result = subprocess.run([shutil.which("node"), "--input-type=module", "-e", source],
                                input=json.dumps({"session": session, "page": page}),
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=10, check=True)
        return json.loads(result.stdout)

    def test_real_file_result_and_event_contract(self):
        self.approval = "allow_all"
        self.start('tool_call: {"name":"write_file","arguments":{"path":"report.md","content":"报告已生成"}}\n---\ntext: 已交付报告。\n')
        sid = self.new_session()
        response = self.client.post(f"/session/{sid}/prompt", json={"text": "生成报告"})
        self.assertEqual(response.status_code, 200, response.text)
        observed = self.consume(sid)
        self.assertEqual(observed["state"]["label"], "已完成")
        self.assertEqual(observed["artifacts"][0]["path"], "report.md")
        self.assertGreater(observed["events"], 0)

    def test_pending_question_is_not_finished_and_reads_do_not_start_model(self):
        self.start('tool_call: {"name":"ask_user","arguments":{"questions":[{"question":"输出哪种格式？","options":["表格","文本"]}]}}\n---\ntext: 等待回答。\n')
        sid = self.new_session(questions={})
        self.client.post(f"/session/{sid}/prompt", json={"text": "先询问格式"})
        before = self.status(sid)
        for _ in range(2):
            self.assertEqual(self.consume(sid)["state"]["label"], "等待回答")
        after = self.status(sid)
        self.assertEqual(after["turns"], before["turns"])
        self.assertEqual(after["next_seq"], before["next_seq"])
        self.assertFalse(after["busy"])

    def test_approval_is_visible_without_executing_the_write(self):
        self.start('tool_call: {"name":"write_file","arguments":{"path":"check.txt","content":"ok"}}\n---\ntext: Finished\n')
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt_async", json={"text": "Write"})
        self._wait_for(sid, "waiting_for_approval")
        self.assertEqual(self.consume(sid)["state"]["label"], "等待审批")
        self.client.post(f"/session/{sid}/abort", json={})
        self._wait_for(sid, "interrupted")
        self.assertFalse((self.root / "check.txt").exists())

    def test_real_failure_never_looks_completed(self):
        self.start("error: fixture source unavailable\n")
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt", json={"text": "Check"})
        self.assertEqual(self.consume(sid)["state"]["label"], "执行失败")

    def test_restart_restores_result_while_event_window_still_reports_gap(self):
        state_dir = Path(self.tmp) / "state"
        self.start("text: Finished\n", state_dir=state_dir)
        sid = self.new_session()
        self.client.post(f"/session/{sid}/prompt", json={"text": "Run once"})
        self.client.__exit__(None, None, None)
        self.start("text: Next\n", state_dir=state_dir)
        observed = self.consume(sid)
        self.assertEqual(observed["state"]["label"], "已完成")
        self.assertTrue(observed["gap"])
        self.assertEqual(observed["artifacts"], [])
        self.assertEqual(self.status(sid)["last_result"]["text"], "Finished")
        history = self.client.get(f"/session/{sid}/history").json()
        self.assertEqual([m["content"] for m in history["messages"]], ["Run once", "Finished"])

    def test_token_service_requires_authentication_on_session_reads(self):
        self.token = "fixture-only-token"
        self.start("text: Ready\n")
        sid = self.new_session()
        self.assertEqual(self.client.get("/session").status_code, 401)
        self.assertEqual(self.consume(sid)["state"]["label"], "尚未开始")

    def test_explicit_origin_is_required_and_read_client_auth_does_not_weaken_it(self):
        self.start("text: Ready\n", cors_origins=("http://127.0.0.1:8421",))
        self.assertEqual(self.client.get("/session", headers={"Origin": "https://invalid.example"}).status_code, 403)
        response = self.client.get("/session", headers={"Origin": "http://127.0.0.1:8421"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "http://127.0.0.1:8421")


if __name__ == "__main__":
    unittest.main()
