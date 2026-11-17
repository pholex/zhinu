"""One owned loopback service for the native lab; stdin EOF means owner exited."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack, asynccontextmanager, redirect_stdout
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import tempfile

from labs.desktop.server import ASSETS, WEB


class DesktopAssets:
    """Public assets contain no credentials; API retains serve authentication."""

    def __init__(self, app, origin: str):
        self.app, self.origin = app, origin

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        from starlette.responses import Response

        headers = dict(scope.get("headers", []))
        if (headers.get(b"host", b"").decode("latin-1") != self.origin.removeprefix("http://")
                or headers.get(b"origin", self.origin.encode()).decode("latin-1") != self.origin):
            await Response(status_code=403)(scope, receive, send)
            return
        path = scope["path"]
        if not path.startswith("/workbench/"):
            await self.app(scope, receive, send)
            return
        asset = ASSETS.get(path.removeprefix("/workbench"))
        if scope["method"] not in {"GET", "HEAD"} or asset is None:
            await Response(status_code=404)(scope, receive, send)
            return
        body = (WEB / asset[0]).read_bytes()
        response = Response(body, headers={
            "Content-Type": asset[1], "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; frame-src 'self' blob:; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        })
        if scope["method"] == "HEAD":
            response.body = b""
        await response(scope, receive, send)


def workspace_state(workspace: Path) -> Path:
    key = hashlib.sha256(os.fsencode(workspace)).hexdigest()[:24]
    return Path.home() / "Library/Application Support/Xiaoyu Desktop Lab/state" / key



def load_model_configuration(workspace: Path, *, fallback_model="", disabled_providers=()) -> None:
    """Use the CLI configuration loader; native credentials supplement its environment."""
    from xiaoyu import folder_trust
    from xiaoyu.config import Config, MissingConfig, load_dotenv
    from xiaoyu.providers import PRESETS, build

    trusted = folder_trust.evaluate(workspace, interactive=False).verdict == "trusted"
    load_dotenv(untrusted_dir=None if trusted else workspace)
    for name in disabled_providers:
        if name in PRESETS:
            for key in PRESETS[name].key_envs:
                os.environ.pop(key, None)
    if os.environ.get("XIAOYU_MODEL", "").strip():
        return
    config = Config.from_env(workspace=workspace)
    try:
        registry = build(config, discover=False)
    except MissingConfig:
        return
    explicit_routing = any(os.environ.get(key, "").strip() for key in
                           ("XIAOYU_PROVIDERS", "XIAOYU_BASE_URL", "XIAOYU_API_KEY"))
    if fallback_model and not explicit_routing:
        try:
            registry.resolve(fallback_model)
        except MissingConfig:
            pass
        else:
            os.environ["XIAOYU_MODEL"] = fallback_model
            return
    if not os.environ.get("XIAOYU_BASE_URL", "").strip():
        choices = registry.listing()
        if choices:
            os.environ["XIAOYU_MODEL"] = choices[0].model


def interrupt_on_exit(app, origin: str, token: str) -> None:
    """Use the existing abort contract before serve persists its final manifests."""
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with original(application):
            try:
                yield
            finally:
                import httpx
                try:
                    async with asyncio.timeout(2), httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app), base_url=origin,
                        headers={"Authorization": f"Bearer {token}"},
                    ) as client:
                        response = await client.get("/session")
                        response.raise_for_status()
                        active = [s["session_id"] for s in response.json()["sessions"] if s["busy"]]
                        for sid in active:
                            (await client.post(f"/session/{sid}/abort", json={})).raise_for_status()
                        while active:
                            for sid in active[:]:
                                if not (await client.get(f"/session/{sid}/status")).json()["busy"]:
                                    active.remove(sid)
                            if active:
                                await asyncio.sleep(.02)
                except (TimeoutError, httpx.HTTPError):
                    print("服务停止未能在等待期限内完成；退出后请检查中断的会话。", file=sys.stderr)

    app.router.lifespan_context = lifespan


async def run(args) -> None:
    import uvicorn

    protocol = sys.stdout
    # Keep arbitrary library/fixture output off the private readiness protocol.
    with ExitStack() as stack, redirect_stdout(sys.stderr):
        sock = stack.enter_context(socket.socket(socket.AF_INET, socket.SOCK_STREAM))
        sock.bind(("127.0.0.1", 0))
        sock.listen(128)
        sock.setblocking(False)
        port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        token = secrets.token_urlsafe(32)
        if args.fixture:
            from labs.desktop.demo import fixture_app, isolated_environment
            root = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="xiaoyu-desktop-demo-"))).resolve()
            isolated_environment(root)
            app = fixture_app(root, port, origin, token, announce=False)
            state = root / "desktop-state"
            workspace = root / "projects"
        else:
            from xiaoyu.serve import ServeConfig, create_app
            workspace = Path(args.workspace).expanduser().resolve(strict=True)
            if not workspace.is_dir():
                raise ValueError("工作目录不是文件夹")
            state = workspace_state(workspace)
            state.mkdir(parents=True, exist_ok=True, mode=0o700)
            lock = stack.enter_context((state / "desktop.lock").open("a"))
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("此工作目录已在另一个桌面实例中打开") from None
            os.chdir(workspace)
            load_model_configuration(workspace, fallback_model=getattr(args, "fallback_model", ""),
                                     disabled_providers=getattr(args, "disabled_provider", ()))
            from labs.desktop.workspace import ProjectResolver, project_state_path
            project_resolver = ProjectResolver(project_state_path(state), workspace)
            app = create_app(ServeConfig(root=workspace, port=port, token=token, state_dir=state,
                environment="host", sandbox=True, mode="default", approval="ask", persist_empty_sessions=False, project_resolver=project_resolver,
                catalog_everywhere=True, mcp=False, cors_extensions=False, cors_origins=(origin,)))

        from labs.desktop.state import install_state_routes
        install_state_routes(app, state / "desktop.json", token)
        from labs.desktop.workspace import install_workspace_routes, project_state_path
        projects_path = project_state_path(state)
        install_workspace_routes(app, projects_path, token, workspace)
        from labs.desktop.connections_api import install_connection_routes
        install_connection_routes(app, state / "connections-v2.json", token, origin, legacy_path=state / "connections.json")
        from labs.desktop.preferences_api import install_preferences_routes
        install_preferences_routes(app, state / "preferences.json", token, root=workspace, origin=origin,
            skill_settings_path=state / "skill-settings.json" if args.fixture else state.parent.parent / "skill-settings.json")
        from labs.desktop.content_api import install_content_routes
        install_content_routes(app, state / "content.json", token, workspace, workspace_path=projects_path)
        from labs.desktop.data_api import install_data_routes
        install_data_routes(app, token, workspace_path=projects_path, managed_paths={
            "应用偏好":state / "preferences.json", "项目元数据":projects_path,
            "空间与页面":state / "content.json", "草稿与回执":state / "desktop.json",
            "自动化记录":state / "automations.json"})
        from labs.desktop.terminal import install_terminal_routes
        install_terminal_routes(app, token, workspace)
        interrupt_on_exit(app, origin, token)
        from labs.desktop.automation_api import install_automation_routes
        install_automation_routes(app, state / "automations.json", token, workspace, origin)
        server = uvicorn.Server(uvicorn.Config(DesktopAssets(app, origin), log_level="warning",
            access_log=False, lifespan="on", timeout_graceful_shutdown=3))
        loop = asyncio.get_running_loop()
        deadline = None

        def owner_input():
            nonlocal deadline
            if not os.read(sys.stdin.fileno(), 4096):
                loop.remove_reader(sys.stdin.fileno())
                server.should_exit = True
                # Also bound shutdown if a provider or integration is unresponsive.
                deadline = loop.call_later(8, os._exit, 1)

        loop.add_reader(sys.stdin.fileno(), owner_input)
        task = asyncio.create_task(server.serve(sockets=[sock]))
        pairing = None
        try:
            while not server.started and not task.done() and not server.should_exit:
                await asyncio.sleep(.02)
            if server.started and not server.should_exit:
                import httpx
                from labs.desktop.pairing import PairingServer
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=origin,
                        headers={"Authorization": f"Bearer {token}"}) as client:
                    health = (await client.get("/health")).json()
                pairing = await PairingServer(workspace, origin, token, health["instance_id"]).start()
                protocol.write(json.dumps({"url": origin + "/workbench/", "token": token,
                    "fixture": args.fixture, "pid": os.getpid()}) + "\n")
                protocol.flush()
            await task
        finally:
            if pairing is not None:
                await pairing.close()
            server.should_exit = True
            loop.remove_reader(sys.stdin.fileno())
            if deadline is not None:
                deadline.cancel()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture", action="store_true")
    mode.add_argument("--workspace")
    parser.add_argument("--fallback-model", default="")
    parser.add_argument("--disabled-provider", action="append", default=[])
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except Exception as error:
        print(f"桌面服务启动失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
