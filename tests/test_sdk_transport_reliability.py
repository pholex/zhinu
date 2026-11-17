"""Actual SDK HTTP failure boundaries with a local scripted provider."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import itertools
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))
from xiaoyu_agent_sdk import BudgetOptions, ExecutionError, ModelOptions, ModelPrice, Session, SessionOptions


def response_bytes(protocol, partial=False):
    if protocol == "chat":
        data = {"id": "local", "object": "chat.completion.chunk", "model": "local",
                "choices": [{"index": 0, "delta": {"content": "42"}, "finish_reason": None if partial else "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6,
                          "prompt_tokens_details": {"cached_tokens": 2}}}
        return ("data: " + json.dumps(data) + "\n\n" + ("" if partial else "data: [DONE]\n\n")).encode()
    if protocol == "responses":
        events = [
            {"type": "response.created", "response": {"id": "resp_local", "status": "in_progress", "output": []}},
            {"type": "response.output_text.delta", "item_id": "msg_local", "output_index": 0, "content_index": 0, "delta": "42"},
        ]
        if not partial:
            events.append({"type": "response.completed", "response": {"id": "resp_local", "status": "completed",
                "output": [{"id": "msg_local", "type": "message", "role": "assistant", "status": "completed",
                            "content": [{"type": "output_text", "text": "42", "annotations": []}]}],
                "usage": {"input_tokens": 5, "output_tokens": 1, "total_tokens": 6,
                          "input_tokens_details": {"cached_tokens": 2}}}})
    else:
        events = [
            {"type": "message_start", "message": {"id": "msg_local", "type": "message", "role": "assistant",
                "model": "local", "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 0, "cache_read_input_tokens": 2,
                          "cache_creation_input_tokens": 3}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "42"}},
        ]
        if not partial:
            events.extend([{"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}},
                {"type": "message_stop"}])
    return "".join("event: " + e["type"] + "\ndata: " + json.dumps(e) + "\n\n" for e in events).encode()


class TransportTests(unittest.TestCase):
    def test_faults_are_bounded_redacted_and_same_session_recovers(self):
        case = self
        for protocol, mode in itertools.product(("chat", "responses", "anthropic"), ("429", "500", "timeout", "truncated", "malformed", "refused")):
            with self.subTest(protocol=protocol, mode=mode), tempfile.TemporaryDirectory() as tmp:
                state = {"mode": mode, "requests": 0}
                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *args):
                        pass
                    def do_POST(self):
                        self.rfile.read(int(self.headers.get("Content-Length", 0)))
                        state["requests"] += 1
                        fault = state["mode"]
                        if fault in {"429", "500"}:
                            data = json.dumps({"error": {"message": "synthetic-private-token", "type": "server_error"}}).encode()
                            self.send_response(int(fault))
                            self.send_header("Content-Length", str(len(data)))
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()
                        else:
                            data = response_bytes(protocol, partial=fault == "truncated")
                            if fault == "malformed":
                                data = b'data: {broken-json}\n\n'
                            self.send_response(200)
                            self.send_header("Content-Type", "text/event-stream")
                            self.send_header("Content-Length", str(len(data) + (100 if fault == "truncated" else 0)))
                            self.end_headers()
                            if fault == "timeout":
                                time.sleep(.2)
                        try:
                            self.wfile.write(data)
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                        self.close_connection = True
                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                port = server.server_port
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                started = mode != "refused"
                if started:
                    worker.start()
                else:
                    server.server_close()
                try:
                    base = f"http://127.0.0.1:{port}" + ("" if protocol == "anthropic" else "/v1")
                    options = SessionOptions(ModelOptions("local", base_url=base, protocol=protocol,
                        api_key="synthetic-private-token", request_timeout=.05), Path(tmp), builtin_tools=(),
                        budget=BudgetOptions({"local": ModelPrice(1, 2, .1, "synthetic-rate", cache_creation_per_million=3)}, max_requests=100))
                    with Session(options) as session:
                        delays = []
                        events = []
                        with patch.object(session._agent, "_sleep", side_effect=lambda duration: delays.append(duration)):
                            with self.assertRaises(ExecutionError) as error:
                                for event in session.stream("fail"):
                                    events.append(event)
                        case.assertNotIn("synthetic-private-token", str(error.exception))
                        case.assertNotIn("synthetic-private-token", repr(events))
                        if mode != "refused":
                            case.assertGreater(state["requests"], 0)
                        else:
                            case.assertEqual(state["requests"], 0)
                        case.assertLessEqual(state["requests"], 8)
                        case.assertLessEqual(len(delays), 8)
                        state["mode"] = "ok"
                        if mode == "refused":
                            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
                            worker = threading.Thread(target=server.serve_forever, daemon=True)
                            worker.start()
                            started = True
                        case.assertEqual(session.run("recover").text, "42")
                        entry = session.cost.entries[-1]
                        case.assertEqual((entry.input_tokens, entry.output_tokens), (10 if protocol == "anthropic" else 5, 1))
                        case.assertEqual(entry.cached_tokens, 2)
                        case.assertEqual(entry.cache_creation_tokens, 3 if protocol == "anthropic" else 0)
                        case.assertEqual(entry.usd, "0.0000162" if protocol == "anthropic" else "0.0000052")
                finally:
                    if started:
                        server.shutdown()
                        worker.join(5)
                    server.server_close()
