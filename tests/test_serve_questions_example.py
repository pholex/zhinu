"""Runnable host contract against the real REST app and durable session journal."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from examples.serve.questions import QuestionHost, demo, demo_server, http_transport, wait_until
from tests.test_serve import HAS_FASTAPI


@unittest.skipUnless(HAS_FASTAPI, "requires [serve]")
class HTTPQuestionExampleTests(unittest.TestCase):
    def test_offline_demo_timely_late_and_idempotent_answers(self):
        with redirect_stdout(StringIO()) as output:
            demo()
        self.assertIn("HTTP questions demo: OK", output.getvalue())

    def test_reconnect_after_eviction_and_restart_preserves_complete_terminal_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with demo_server(root, buffer_limit=2, max_field=8) as request:
                sid = request("POST", "/session", {"questions": {}}, None)["session_id"]
                host = QuestionHost(request, sid)
                host.reconnect()
                host.prompt("Ask", "ask")
                wait_until(lambda: not host.status()["busy"])
                self.assertGreater(host.status()["first_seq"], host.cursor)
                question, = host.poll(0)
                self.assertEqual(question["items"][0]["question"], "Which output format?")
                self.assertEqual(host.poll(0), [])
                submission = {"answers": [{"item_id": question["items"][0]["item_id"],
                                           "custom": "用户输入"}], "idempotency_key": "click-1"}
                receipt = host.answer(question["question_id"], submission)
                self.assertEqual(receipt["state"], "queued")
            with demo_server(root, script="text: Accepted\n") as request:
                # Retain the old client cursor/version map across a transport reconnect.
                host.request = request
                changed = host.poll(0)
                self.assertEqual(changed[0]["answer_id"], receipt["answer_id"])
                self.assertFalse(host.status()["busy"])
                host.prompt("Continue", "continue")
                wait_until(lambda: not host.status()["busy"])
                final, = host.poll(0)
                self.assertEqual(final["state"], "answered")
                self.assertEqual(host.answer(question["question_id"], submission), final)
                self.assertEqual(host.reconnect(), [])
                new_host = QuestionHost(request, sid)
                self.assertEqual(new_host.reconnect(), [final])

    def test_clipped_event_without_eviction_fetches_full_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            with demo_server(Path(temporary).resolve(), max_field=8) as request:
                sid = request("POST", "/session", {"questions": {}}, None)["session_id"]
                host = QuestionHost(request, sid)
                host.reconnect()
                host.prompt("Ask", "ask")
                wait_until(lambda: not host.status()["busy"])
                self.assertEqual(host.status()["dropped_events"], 0)
                question, = host.poll(0)
                self.assertEqual(question["items"][0]["question"], "Which output format?")
                self.assertEqual(question["state"], "pending")
                # Observation alone must not answer, cancel or start another turn.
                self.assertEqual(host.reconnect(), [])
                self.assertEqual(host.status()["turns"], 1)

    def test_replayed_or_older_snapshot_never_regresses_a_version(self):
        with tempfile.TemporaryDirectory() as temporary:
            with demo_server(Path(temporary).resolve()) as request:
                sid = request("POST", "/session", {"questions": {}}, None)["session_id"]
                host = QuestionHost(request, sid)
                host.prompt("Ask", "ask")
                wait_until(lambda: not host.status()["busy"])
                old = request("GET", host.path + "/questions?include_terminal=true", None, None)
                question, = host.reconnect()
                host.answer(question["question_id"], {"answers": [{
                    "item_id": question["items"][0]["item_id"], "skipped": True}],
                    "idempotency_key": "skip"})
                self.assertEqual(host.reconnect()[0]["state"], "queued")
                host.request = lambda *args: old
                self.assertEqual(host.reconnect(), [])
                self.assertEqual(host.questions[question["question_id"]]["state"], "queued")


class HTTPTransportTests(unittest.TestCase):
    def test_utf8_body_auth_and_prompt_key_reach_the_transport(self):
        with patch("examples.serve.questions.build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.read.return_value = b'{"accepted":true}'
            request = http_transport("http://127.0.0.1:8420/", "instance-token")
            host = QuestionHost(request, "session")
            self.assertTrue(host.prompt("继续", "prompt-key")["accepted"])
            req = build.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, "http://127.0.0.1:8420/session/session/prompt_async")
            self.assertEqual(json.loads(req.data), {"text": "继续"})
            self.assertEqual(req.get_header("Authorization"), "Bearer instance-token")
            self.assertEqual(req.get_header("Idempotency-key"), "prompt-key")


if __name__ == "__main__":
    unittest.main()
