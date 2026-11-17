"""The static entry point must not expose workspace files or configuration."""
import http.client
import re
from contextlib import closing
import threading
import unittest
from http.server import HTTPServer

from labs.desktop.server import AssetsHandler


class AssetsTests(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), AssetsHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def request(self, path, method="GET", headers=None):
        with closing(http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)) as conn:
            conn.request(method, path, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.headers), response.read()

    def test_asset_whitelist_and_security_headers(self):
        code, headers, content = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn("羽案".encode(), content)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        for path in ("/.env", "/../README.md", "/%2e%2e/README.md", "/server.py", "/tests/test_assets.py"):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/", headers={"Host": "external.example"})[0], 403)
        self.assertEqual(self.request("/", method="POST")[0], 501)

    def test_entry_module_entire_import_graph_is_served(self):
        pending, seen = ["/app.mjs"], set()
        while pending:
            path = pending.pop()
            if path in seen:
                continue
            seen.add(path)
            code, headers, body = self.request(path)
            self.assertEqual(code, 200, f"missing imported module: {path}")
            self.assertIn("javascript", headers["Content-Type"])
            source = body.decode("utf-8", errors="replace")
            for relative in re.findall(r"(?:from\s*|import\s*)['\"](\./[^'\"]+)['\"]", source):
                pending.append("/" + relative.removeprefix("./"))
        self.assertIn("/view_state.mjs", seen)

    def test_module_mime_and_head(self):
        for path in ("/conversation_ui.mjs", "/model_picker.mjs", "/ui.mjs", "/client.mjs", "/workflows.mjs", "/contests.mjs", "/fleets.mjs", "/fleet_commands.mjs", "/weave_controls.mjs", "/workspace.mjs"):
            with self.subTest(path=path):
                code, headers, body = self.request(path, method="HEAD")
                self.assertEqual(code, 200)
                self.assertEqual(body, b"")
                self.assertIn("javascript", headers["Content-Type"])
                self.assertTrue(self.request(path)[2])


if __name__ == "__main__":
    unittest.main()
