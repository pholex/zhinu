"""Local attach client contracts; no model, user sessions, or TCP listener."""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest import mock

from xiaoyu import cli_connect as connect


POSIX_PAIRING = hasattr(socket, "AF_UNIX") and hasattr(os, "getuid")

class ConnectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.pairing = connect.Pairing("http://127.0.0.1:8420", "memory-token", "instance-a", str(self.root))

    def test_keyless_help_contract(self):
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"COLUMNS": "80"}), contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as exit:
            connect.connect_command(["--help"])
        self.assertEqual(exit.exception.code, 0)
        rendered = output.getvalue()
        expected = Path(__file__).parent / "fixtures" / "cli_connect_help.expected.txt"
        if os.environ.get("XIAOYU_SNAPSHOT") == "refresh":
            expected.write_text(rendered, encoding="utf-8")
        #  argparse 的折行位置随 Python 版本变（3.11 与 3.14 不同）；契约是内容，不是折行
        self.assertEqual(rendered.split(), expected.read_text(encoding="utf-8", errors="replace").split())

    def health(self):
        return {"instance_id": "instance-a", "root": str(self.root)}

    @unittest.skipUnless(POSIX_PAIRING, "配对通道基于 Unix 套接字与属主权限，Windows 上没有")
    def test_socket_path_is_short_and_workspace_specific(self):
        path = connect.pairing_socket_path(self.root)
        self.assertLess(len(str(path).encode()), 104)
        self.assertEqual(path.parent, Path("/tmp") / f"xiaoyu-desktop-{os.getuid()}")
        self.assertEqual(connect.socket_path(self.root / "."), path)
        child = self.root / "nested"
        child.mkdir()
        self.assertNotEqual(connect.socket_path(child), path)

    @unittest.skipUnless(POSIX_PAIRING, "配对通道基于 Unix 套接字与属主权限，Windows 上没有")
    def test_private_unix_pairing_reads_token_only_in_memory(self):
        parent = self.root / "pair"
        parent.mkdir(mode=0o700)
        path = parent / "p.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(path))
        path.chmod(0o600)
        server.listen(1)
        requests, errors = [], []
        def respond():
            try:
                with server.accept()[0] as channel:
                    channel.settimeout(2)
                    data = bytearray()
                    while b"\n" not in data:
                        data.extend(channel.recv(4096))
                    requests.append(json.loads(data))
                    response = {"protocol":1, "workspace":str(self.root), "base_url":self.pairing.base_url,
                                "token":self.pairing.token, "instance_id":"instance-a"}
                    channel.sendall(json.dumps(response).encode() + b"\n")
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=respond, daemon=True)
        worker.start()
        with mock.patch.object(connect, "pairing_socket_path", return_value=path):
            paired = connect.pair(self.root)
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(paired, self.pairing)
        self.assertEqual(requests, [{"protocol":1,"client":"xiaoyu-cli","workspace":str(self.root)}])
        self.assertNotIn(self.pairing.token, repr(paired))
        self.assertEqual(list(parent.iterdir()), [path])

    @unittest.skipUnless(POSIX_PAIRING, "配对通道基于 Unix 套接字与属主权限，Windows 上没有")
    def test_pairing_rejects_symlinks_permissive_directory_and_regular_file(self):
        parent = self.root / "pair"
        parent.mkdir(mode=0o700)
        target = parent / "not-a-socket"
        target.write_text("no credentials", encoding="utf-8")
        target.chmod(0o600)
        with self.assertRaises(connect.ConnectError):
            connect.validate_pairing_socket(target)
        link = self.root / "alias"
        link.symlink_to(parent)
        with self.assertRaises(connect.ConnectError):
            connect.validate_pairing_socket(link / target.name)
        parent.chmod(0o755)
        with self.assertRaises(connect.ConnectError):
            connect.validate_pairing_socket(target)

    def test_origin_rejects_remote_credentials_redirect_paths_and_bad_ports(self):
        for url in ("https://127.0.0.1:8420", "http://example.org:8420", "http://u:p@127.0.0.1:8420",
                    "http://127.0.0.1:8420/anything", "http://127.0.0.1:8420?q=token",
                    "http://127.0.0.1:8420/#fragment", "http://127.0.0.1:99999", "http://127.0.0.1:8420\n"):
            with self.subTest(url=url), self.assertRaises(connect.ConnectError):
                connect._origin(url)

    def test_pairing_rejects_wrong_workspace_and_oversized_or_untrusted_payload(self):
        good = {"protocol":1,"workspace":str(self.root),"base_url":self.pairing.base_url,
                "token":self.pairing.token,"instance_id":"instance-a"}
        samples = [json.dumps(dict(good, workspace="/different")).encode() + b"\n",
                   json.dumps(dict(good, base_url="http://remote.example:8420")).encode() + b"\n",
                   json.dumps({"protocol":1,"error":"denied","token":"never-echo"}).encode() + b"\n",
                   b"x" * (connect.PAIR_LIMIT + 1)]
        for payload in samples:
            channel = mock.MagicMock()
            channel.__enter__.return_value = channel
            channel.recv.return_value = payload
            with self.subTest(payload_size=len(payload)), \
                    mock.patch.object(connect, "validate_pairing_socket", return_value=SimpleNamespace(st_dev=1,st_ino=1)), \
                    mock.patch.object(connect.socket, "socket", return_value=channel), \
                    self.assertRaises(connect.ConnectError) as failure:
                connect.pair(self.root)
            self.assertNotIn("never-echo", str(failure.exception))

    def transport(self, responses):
        calls = []
        sequence = iter(responses)
        def make(host, port, timeout):
            class Connection:
                def request(self, method, path, body, headers):
                    calls.append(dict(host=host, port=port, method=method, path=path,
                                      body=json.loads(body) if body else None, headers=headers))
                def getresponse(self):
                    value = next(sequence)
                    if isinstance(value, Exception):
                        raise value
                    status, body = value
                    return mock.Mock(status=status, read=lambda limit: json.dumps(body).encode())
                def close(self):
                    pass
            return Connection()
        return calls, mock.patch.object(connect.http.client, "HTTPConnection", side_effect=make)

    def test_http_uses_header_token_loopback_and_no_redirect_follow(self):
        calls, transport = self.transport([(302, {"location":"http://remote/"})])
        with transport, self.assertRaises(connect.ConnectError):
            connect.ConnectClient(self.pairing).request("GET", "/session")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["host"], "127.0.0.1")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer memory-token")
        self.assertNotIn(self.pairing.token, calls[0]["path"])

    def test_unknown_write_is_not_replayed_until_explicit_same_key_retry(self):
        client = connect.ConnectClient(self.pairing)
        calls, transport = self.transport([ConnectionResetError("must-not-echo memory-token"),
                                           (200,self.health()), (202,{"accepted":True,"duplicate":True})])
        with transport:
            with self.assertRaises(connect.UnknownSubmission) as failure:
                client.write("/session/s1/prompt_async", {"text":"task"}, key="stable-key", retryable=True)
            self.assertNotIn(self.pairing.token, str(failure.exception))
            self.assertEqual(len(calls), 1)
            self.assertEqual(client.pending.state, "unknown")
            with self.assertRaises(connect.ConnectError):
                client.write("/session/s1/prompt_async", {"text":"other"}, key="other")
            self.assertTrue(client.retry_pending()["duplicate"])
        posts = [c for c in calls if c["method"] == "POST"]
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1])

    def test_instance_change_prevents_retry_and_steer_never_retries(self):
        client = connect.ConnectClient(self.pairing)
        calls, transport = self.transport([ConnectionResetError(), (200,{"instance_id":"new", "root":str(self.root)})])
        with transport:
            with self.assertRaises(connect.UnknownSubmission):
                client.write("/session/s1/prompt_async", {"text":"task"}, key="key", retryable=True)
            with self.assertRaises(connect.ConnectError):
                client.retry_pending()
        self.assertEqual(sum(c["method"] == "POST" for c in calls), 1)
        client = connect.ConnectClient(self.pairing)
        calls, transport = self.transport([ConnectionResetError()])
        with transport:
            with self.assertRaises(connect.UnknownSubmission):
                client.write("/session/s1/steer", {"text":"task", "turn":1})
            with self.assertRaises(connect.ConnectError):
                client.retry_pending()
        self.assertEqual(len(calls), 1)

    def test_pending_answer_keeps_original_nested_body_on_explicit_retry(self):
        client = connect.ConnectClient(self.pairing)
        body = {"answers":[{"item_id":"a","custom":"original"}],"idempotency_key":"stable"}
        calls, transport = self.transport([ConnectionResetError(), (200,self.health()), (202,{"question":{"state":"queued"}})])
        with transport:
            with self.assertRaises(connect.UnknownSubmission):
                client.write("/session/s1/questions/q1/answers", body, key="stable", retryable=True)
            body["answers"][0]["custom"] = "mutated"
            client.retry_pending()
        self.assertEqual(calls[0]["body"], calls[-1]["body"])
        self.assertEqual(calls[-1]["body"]["answers"][0]["custom"], "original")

    def cli(self, argv, responses):
        calls, transport = self.transport([(200, self.health()), *responses])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(connect, "pair", return_value=self.pairing), transport, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = connect.connect_command(["--workspace", str(self.root), *argv])
        return result, calls, out.getvalue(), err.getvalue()

    def test_cli_default_lists_existing_sessions_without_creating_any(self):
        result, calls, _, _ = self.cli([], [(200,{"sessions":[{"session_id":"s1"}]})])
        self.assertEqual(result, 0)
        self.assertEqual([(c["method"],c["path"]) for c in calls], [("GET","/health"),("GET","/session")])

    def test_history_pagination_never_repeats_or_opens_logs(self):
        result, calls, out, _ = self.cli(["--session","s1"], [
            (200,{"messages":[{"id":"log:1"}],"next_cursor":1}),
            (200,{"messages":[{"id":"log:2"}],"next_cursor":None})])
        self.assertEqual(result, 0)
        self.assertIn("cursor=1", calls[-1]["path"])
        self.assertEqual(out.count("log:1"), 1)
        self.assertEqual(out.count("log:2"), 1)
        self.assertTrue(all(c["method"] == "GET" for c in calls))

    def test_explicit_steer_includes_exact_turn_instance_and_queue_result(self):
        result, calls, out, _ = self.cli(["--session","s1","--steer","hello","--turn","3"],
                                       [(200,{"steered":True})])
        self.assertEqual(result, 0)
        self.assertEqual(calls[-1]["body"], {"turn":3,"text":"hello"})
        self.assertEqual(calls[-1]["headers"]["X-Xiaoyu-Instance"], "instance-a")
        self.assertNotIn("accepted", out)

    def test_one_time_approval_and_answer_preserve_protocol_truth(self):
        result, calls, _, _ = self.cli(["--session","s1","--approve","r1","--decision","deny","--reason","skip"],
                                     [(200,{"ok":True})])
        self.assertEqual(result, 0)
        self.assertEqual(calls[-1]["body"], {"request_id":"r1","decision":"deny","reason":"skip"})
        result, calls, out, _ = self.cli(["--session","s1","--answer","q1","--answers-json", '[{"item_id":"a","custom":"answer"}]',
                                       "--idempotency-key","answer-key"], [(202,{"question":{"state":"queued"}})])
        self.assertEqual(result, 0)
        self.assertEqual(calls[-1]["body"]["idempotency_key"], "answer-key")
        self.assertIn('"state": "queued"', out)
        self.assertNotIn("accepted", out)

    def test_invalid_flags_do_not_pair_or_write(self):
        for argv in (["--prompt","task"], ["--session","s1","--abort"],
                     ["--session","s1","--approve","r1"], ["--session","s1","--answer","q1"]):
            with self.subTest(argv=argv), mock.patch.object(connect, "pair") as pair, contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(connect.connect_command(argv), 2)
                pair.assert_not_called()

    def test_disconnect_exit_is_unknown_and_never_replays(self):
        result, calls, out, err = self.cli(["--session","s1","--prompt","task","--idempotency-key","stable"],
                                         [ConnectionResetError("credential memory-token")])
        self.assertEqual(result, 3)
        self.assertEqual(sum(c["method"] == "POST" for c in calls), 1)
        self.assertIn("stable", out)
        self.assertNotIn("memory-token", out + err)

    def test_watch_marks_event_gap_and_detach_never_aborts(self):
        client = connect.ConnectClient(self.pairing)
        responses = iter([self.health(), {"events":[],"first_seq":10,"next":11}, self.health(), KeyboardInterrupt()])
        def request(*args, **kwargs):
            value = next(responses)
            if isinstance(value, BaseException):
                raise value
            return value
        with mock.patch.object(client, "request", side_effect=request) as transport:
            stream = client.watch("s1", 1)
            self.assertIn("warning", next(stream))
            self.assertEqual(next(stream)["next"], 11)
            with self.assertRaises(KeyboardInterrupt):
                next(stream)
            self.assertTrue(all(call.args[0] == "GET" for call in transport.call_args_list))


if __name__ == "__main__":
    unittest.main()
