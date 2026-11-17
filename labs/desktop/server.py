"""Serve only the workbench assets on loopback; never proxy agent requests."""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ASSETS = {
    "/brands/github.svg": ("brands/github.svg", "image/svg+xml"),
    "/brands/supabase.svg": ("brands/supabase.svg", "image/svg+xml"),
    "/brands/vercel.svg": ("brands/vercel.svg", "image/svg+xml"),
    "/brands/cloudflare.svg": ("brands/cloudflare.svg", "image/svg+xml"),
    "/brands/amap.ico": ("brands/amap.ico", "image/x-icon"),
    "/view_state.mjs": ("view_state.mjs", "text/javascript; charset=utf-8"),
    "/view_history.mjs": ("view_history.mjs", "text/javascript; charset=utf-8"),
    "/composer_additions.mjs": ("composer_additions.mjs", "text/javascript; charset=utf-8"),
    "/conversation_ui.mjs": ("conversation_ui.mjs", "text/javascript; charset=utf-8"),
    "/execution_modes.mjs": ("execution_modes.mjs", "text/javascript; charset=utf-8"),
    "/model_picker.mjs": ("model_picker.mjs", "text/javascript; charset=utf-8"),
    "/ui.mjs": ("ui.mjs", "text/javascript; charset=utf-8"),
    "/shortcuts.mjs": ("shortcuts.mjs", "text/javascript; charset=utf-8"),
    "/data.mjs": ("data.mjs", "text/javascript; charset=utf-8"),
    "/state_conflicts.mjs": ("state_conflicts.mjs", "text/javascript; charset=utf-8"),
    "/providers.mjs": ("providers.mjs", "text/javascript; charset=utf-8"),
    "/panes.mjs": ("panes.mjs", "text/javascript; charset=utf-8"),
    "/panes.css": ("panes.css", "text/css; charset=utf-8"),
    "/sidechat.mjs": ("sidechat.mjs", "text/javascript; charset=utf-8"),
    "/capabilities.mjs": ("capabilities.mjs", "text/javascript; charset=utf-8"),
    "/content.mjs": ("content.mjs", "text/javascript; charset=utf-8"),
    "/organization.mjs": ("organization.mjs", "text/javascript; charset=utf-8"),
    "/terminal_sessions.mjs": ("terminal_sessions.mjs", "text/javascript; charset=utf-8"),
    "/settings.mjs": ("settings.mjs", "text/javascript; charset=utf-8"),
    "/settings.css": ("settings.css", "text/css; charset=utf-8"),
    "/automations.mjs": ("automations.mjs", "text/javascript; charset=utf-8"),
    "/connections.mjs": ("connections.mjs", "text/javascript; charset=utf-8"),
    "/workspace.mjs": ("workspace.mjs", "text/javascript; charset=utf-8"),
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.mjs": ("app.mjs", "text/javascript; charset=utf-8"),
    "/client.mjs": ("client.mjs", "text/javascript; charset=utf-8"),
    "/weave_controls.mjs": ("weave_controls.mjs", "text/javascript; charset=utf-8"),
    "/workflows.mjs": ("workflows.mjs", "text/javascript; charset=utf-8"),
    "/contests.mjs": ("contests.mjs", "text/javascript; charset=utf-8"),
    "/fleet_commands.mjs": ("fleet_commands.mjs", "text/javascript; charset=utf-8"),
    "/fleets.mjs": ("fleets.mjs", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/logo.png": ("logo.png", "image/png"),
}
WEB = Path(__file__).resolve().parent / "web"
CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src http://127.0.0.1:* http://localhost:* http://[::1]:*; "
    "img-src 'self' data:; frame-src 'self' blob:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)


class AssetsHandler(BaseHTTPRequestHandler):
    def setup(self) -> None:
        # A browser may open an idle preconnection. Do not let it block other assets.
        self.request.settimeout(3)
        super().setup()

    def do_GET(self) -> None:
        self._respond(body=True)

    def do_HEAD(self) -> None:
        self._respond(body=False)

    def _respond(self, *, body: bool) -> None:
        try:
            hostname = urlsplit("//" + self.headers.get("Host", "")).hostname
        except ValueError:
            hostname = None
        if hostname not in {"127.0.0.1", "localhost", "::1"}:
            self.send_error(403, "Loopback host required")
            return
        asset = ASSETS.get(urlsplit(self.path).path)
        if asset is None:
            self.send_error(404)
            return
        content = (WEB / asset[0]).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", asset[1])
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if body:
            self.wfile.write(content)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8421)
    args = parser.parse_args()
    with HTTPServer(("127.0.0.1", args.port), AssetsHandler) as server:
        print(f"羽案工作台：http://127.0.0.1:{server.server_port}", flush=True)
        print("仅提供界面文件；服务由你单独启动。Ctrl-C 关闭界面服务器。", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
