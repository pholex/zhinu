"""Dynamic MCP lifecycle, OAuth exchange and real transport ownership."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit, urlencode
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages/xiaoyu-agent-sdk/src"))
from xiaoyu_agent_sdk import (CloseTimeoutError, ConfigurationError, McpManagementError, McpPool, McpServer,
    MemoryTokenStore, ModelOptions, OAuthClient, OAuthError, OAuthTokens, Session, SessionBusyError, SessionLockedError, SessionOptions)
from tests.test_agent_paths import FakeClient, chunk
from tests.test_mcp import FAKE_SERVER, _HttpServerCase, _McpHttpHandler


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.path = self.root / "server.py"
        self.path.write_text(FAKE_SERVER, encoding="utf-8")

    def spec(self):
        return McpServer("own", command=sys.executable, args=(str(self.path),))

    def options(self, **kwargs):
        return SessionOptions(ModelOptions("test", client=FakeClient([[chunk("ok")]])), self.root, builtin_tools=(), **kwargs)

    def ready(self, session):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if session.mcp_status()[0].state != "loading":
                return
            time.sleep(.01)
        self.fail("MCP did not finish startup")

    def test_add_stop_start_reconnect_remove_real_process(self):
        with Session(self.options()) as session:
            session.mcp_add(self.spec())
            self.ready(session)
            self.assertEqual(session.mcp_status()[0].state, "ready")
            manager = session._mcp._managers["own"]
            process = manager._servers["own"]._proc
            self.assertIsNone(process.poll())
            self.assertEqual(session.mcp_manage("own", "stop"), "stopped")
            self.assertIsNotNone(process.poll())
            self.assertEqual(session._mcp.ready_tools(), [])
            session.mcp_manage("own", "start")
            self.ready(session)
            self.assertEqual(session.mcp_manage("own", "reconnect"), "ready")
            self.assertEqual(session.mcp_manage("own", "remove"), "removed")
            self.assertEqual(session.mcp_status(), ())
            self.assertEqual(session.run("continue").text, "ok")

    def test_borrowed_pool_survives_session_and_has_exclusive_lease(self):
        pool = McpPool((self.spec(),))
        try:
            with Session(self.options(mcp_pool=pool)) as session:
                self.ready(session)
                with self.assertRaises(McpManagementError):
                    Session(self.options(mcp_pool=pool))
                with self.assertRaises(McpManagementError):
                    pool.stop("own")
                with self.assertRaises(McpManagementError):
                    pool.close()
            self.assertEqual(pool.server_states()["own"], "ready")
            with Session(self.options(mcp_pool=pool)):
                pass
        finally:
            pool.close()
        self.assertEqual(pool.shutdown_pending(), ())

    def test_readding_name_does_not_approve_changed_declarations(self):
        with Session(self.options(mcp_servers=(self.spec(),))) as session:
            self.ready(session)
            session.mcp_manage("own", "remove")
            self.path.write_text(FAKE_SERVER.replace("回显文本", "changed meaning"), encoding="utf-8")
            session.mcp_add(self.spec())
            self.ready(session)
            tools = session._mcp.ready_tools()
            self.assertFalse(any(t.raw_name == "echo" and t.check_fn() for t in tools))
            session.mcp_manage("own", "approve_changes")
            self.assertTrue(any(t.raw_name == "echo" and t.check_fn() for t in session._mcp.ready_tools()))

    def test_management_during_active_turn_is_rejected(self):
        entered, release = threading.Event(), threading.Event()
        def stream():
            entered.set()
            release.wait(5)
            yield chunk("ok")
        opts = self.options()
        opts.model.client.completions.script = [stream()]
        with Session(opts) as session:
            thread = threading.Thread(target=session.run, args=("wait",))
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                with self.assertRaises(SessionBusyError):
                    session.mcp_add(self.spec())
            finally:
                release.set()
                thread.join(5)

    def test_close_during_blocked_stop_retains_process_and_session_ownership(self):
        entered, release = threading.Event(), threading.Event()
        options = self.options(session_dir=self.root / "logs", close_timeout=.02)
        session = Session(options)
        session.mcp_add(self.spec())
        self.ready(session)
        manager = session._mcp._managers["own"]
        process = manager._servers["own"]._proc
        original = manager.close
        errors, calls = [], []
        def blocked():
            calls.append(1)
            entered.set()
            release.wait(5)
            original()
        def stop():
            try:
                session.mcp_manage("own", "stop")
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=stop)
        try:
            with patch.object(manager, "close", blocked):
                worker.start()
                self.assertTrue(entered.wait(3))
                with self.assertRaises(CloseTimeoutError):
                    session.close()
                self.assertFalse(session.closed)
                self.assertIsNone(process.poll())
                self.assertIs(session._mcp._managers["own"], manager)
                with self.assertRaises(SessionLockedError):
                    Session(options, resume_from=session.session_path)
                release.set()
                worker.join(5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
                self.assertEqual(calls, [1])
        finally:
            release.set()
            if worker.ident is not None:
                worker.join(5)
            session.options = replace(session.options, close_timeout=5)
            session.close()
        self.assertIsNotNone(process.poll())
        self.assertTrue(session.closed)

    def test_parallel_mcp_media_is_assigned_to_its_calling_child(self):
        from xiaoyu.mcp import McpManager
        manager = McpManager([], state_dir=self.root)
        barrier = threading.Barrier(2)
        results = {}
        def caller(name):
            manager.stash_media([{"owner": name}])
            barrier.wait(5)
            results[name] = manager.take_media()
        threads = [threading.Thread(target=caller, args=(str(n),)) for n in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        manager.close()
        self.assertEqual(results, {"0": [{"owner": "0"}], "1": [{"owner": "1"}]})

    def test_reused_thread_does_not_deliver_abandoned_child_media_to_next_child(self):
        from xiaoyu.mcp import McpManager, media_scope
        from xiaoyu.config import Config
        from xiaoyu.tools import Toolbox
        manager = McpManager([], state_dir=self.root)
        left = Toolbox(Config(base_url="http://unused", model="test", workspace=self.root, enable_mcp=False), only=(), mcp_view=manager)
        right = Toolbox(Config(base_url="http://unused", model="test", workspace=self.root, enable_mcp=False), only=(), mcp_view=manager)
        with media_scope(left):
            manager.stash_media([{"owner": "left"}])
        self.assertEqual(right.take_media(), [])
        self.assertEqual(left.take_media(), [{"owner": "left"}])
        manager.close()


class OAuthTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.response = {"access_token": "synthetic-secret", "refresh_token": "synthetic-refresh", "token_type": "Bearer", "expires_in": 3600, "scope": "read"}
        self.status = 200
        case = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                case.requests.append((self.path, parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())))
                data = json.dumps(case.response if self.path == "/token" else {}).encode()
                self.send_response(case.status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.worker = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.worker.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.store = MemoryTokenStore()
        self.oauth = OAuthClient(resource=self.url + "/mcp", client_id="registered-client",
            authorization_endpoint=self.url + "/authorize", token_endpoint=self.url + "/token",
            redirect_uri="http://127.0.0.1/callback", scope="read", revocation_endpoint=self.url + "/revoke", token_store=self.store)

    def authorize(self, url):
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["resource"], [self.url + "/mcp"])
        self.challenge = query["code_challenge"][0]
        return "http://127.0.0.1/callback?" + urlencode(dict(code="one-time", state=query["state"][0]))

    def test_pkce_refresh_rotation_and_revoke_over_http(self):
        import hashlib, base64
        self.oauth.authorize(self.authorize)
        self.assertEqual(self.oauth.authorization(), "Bearer synthetic-secret")
        verifier = self.requests[0][1]["code_verifier"][0]
        self.assertEqual(base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("="), self.challenge)
        self.store.save(OAuthTokens("old", "rotate", expires_at=1, scope="read"))
        self.response = {"access_token": "new", "token_type": "Bearer", "expires_in": 3600}
        self.assertEqual(self.oauth.authorization(), "Bearer new")
        self.assertEqual(self.store.load().refresh_token, "rotate")
        self.assertEqual(self.requests[-1][1]["grant_type"], ["refresh_token"])
        self.oauth.revoke()
        self.assertEqual(len([r for r in self.requests if r[0] == "/revoke"]), 2)
        self.assertIsNone(self.store.load())
        with self.assertRaises(OAuthError):
            self.oauth.authorization()

    def test_state_mismatch_does_not_exchange_code(self):
        with self.assertRaises(OAuthError):
            self.oauth.authorize(lambda _: "http://127.0.0.1/callback?code=secret&state=wrong")
        self.assertEqual(self.requests, [])

    def test_store_failure_still_invalidates_client_and_redacts_error(self):
        class BrokenStore(MemoryTokenStore):
            def clear(self):
                raise RuntimeError("synthetic-store-secret")
        store = BrokenStore()
        store.save(OAuthTokens("synthetic-access", scope="read"))
        self.oauth.store = store
        with self.assertRaises(OAuthError) as error:
            self.oauth.revoke()
        self.assertNotIn("synthetic-store-secret", str(error.exception))
        with self.assertRaises(OAuthError):
            self.oauth.authorization()

    def test_refresh_failure_is_bounded_and_clears_credentials(self):
        self.store.save(OAuthTokens("old", "refresh", 1, "read"))
        self.status = 400
        for _ in range(2):
            with self.assertRaises(OAuthError) as error:
                self.oauth.authorization()
            self.assertNotIn("synthetic", str(error.exception))
        self.assertEqual(len(self.requests), 1)
        self.assertIsNone(self.store.load())

    def test_scope_expansion_rejected_and_failed_revocation_clears_locally(self):
        self.response["scope"] = "read admin"
        with self.assertRaises(OAuthError):
            self.oauth.authorize(self.authorize)
        self.store.save(OAuthTokens("synthetic-secret"))
        self.status = 500
        with self.assertRaises(OAuthError):
            self.oauth.revoke()
        self.assertIsNone(self.store.load())

    def test_resource_and_transport_validation(self):
        with self.assertRaises(ConfigurationError):
            McpPool.validate(McpServer("bad", url="https://different/mcp", oauth=self.oauth))
        self.assertNotIn("synthetic", repr(self.store.load()))
        with self.assertRaises(ConfigurationError):
            OAuthClient(resource="http://remote.example/mcp", client_id="x", authorization_endpoint=self.url,
                        token_endpoint=self.url, redirect_uri=self.url)


class OAuthTransportTests(_HttpServerCase):
    def test_kernel_http_authorization_uses_provider_and_never_serializes_tokens(self):
        tokens = MemoryTokenStore()
        tokens.save(OAuthTokens("synthetic-mcp-secret", scope="read"))
        oauth = OAuthClient(resource=self.url, client_id="test", authorization_endpoint=self.url,
                            token_endpoint=self.url, redirect_uri="http://127.0.0.1/callback", scope="read", token_store=tokens)
        _McpHttpHandler.require_auth = "Bearer synthetic-mcp-secret"
        server = self.make_server(authorization=oauth.authorization)
        self.assertTrue(server.bootstrap())
        self.assertNotIn("synthetic-mcp-secret", repr(server.spec))
        with self.assertRaises(OAuthError):
            oauth.revoke()  # No remote endpoint: local invalidation still applies.
        self.assertIn("ERROR", server.call_tool("echo", {"text": "must not authorize"}))
