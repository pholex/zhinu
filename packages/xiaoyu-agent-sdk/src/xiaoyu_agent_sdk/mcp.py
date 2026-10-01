"""Explicit MCP ownership and per-server lifecycle over the kernel transport."""
from __future__ import annotations

import hashlib
import math
from pathlib import Path
import tempfile
import threading
from dataclasses import replace
from typing import Any

from .types import CloseTimeoutError, ConfigurationError, McpManagementError, McpServer


class McpPool:
    """Host-owned pool, exclusively leased to one SDK session at a time.

    A borrowed pool outlives its session. Mutations through a leased pool must
    go through Session.mcp_* so the session can enforce its idle boundary.
    """
    def __init__(self, servers: tuple[McpServer, ...] = (), *, state_dir: Path | None = None) -> None:
        self._lock = threading.RLock()
        self._lease: str | None = None
        self._closed = False
        self._pending: tuple[str, ...] = ()
        self._temporary = tempfile.TemporaryDirectory(prefix="xiaoyu-sdk-mcp-") if state_dir is None else None
        self.state_dir = Path(self._temporary.name if self._temporary else state_dir)  # type: ignore[arg-type]
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._specs: dict[str, McpServer] = {}
        self._managers: dict[str, Any] = {}
        self._stopping: set[str] = set()
        self._wrappers: dict[int, Any] = {}
        try:
            for server in servers:
                self.add(server)
        except BaseException:
            self.close()
            raise

    def acquire(self, session_id: str) -> None:
        with self._lock:
            if self._closed or self._lease is not None:
                raise McpManagementError("MCP pool is closed or already leased")
            self._lease = session_id

    def release(self, session_id: str) -> None:
        with self._lock:
            if self._lease != session_id:
                raise McpManagementError("MCP pool ownership differs")
            self._lease = None

    def _check(self, owner: str | None) -> None:
        if self._closed:
            raise McpManagementError("MCP pool is closed")
        if self._lease is not None and owner != self._lease:
            raise McpManagementError("Mutate leased MCP through its session")

    @staticmethod
    def validate(server: McpServer) -> None:
        if not server.name or bool(server.command) == bool(server.url) or not math.isfinite(server.timeout) or server.timeout <= 0:
            raise ConfigurationError("MCP requires name, positive timeout and command or URL")
        if server.oauth is not None:
            if not server.url or server.oauth.resource != server.url or any(k.lower() == "authorization" for k in server.headers):
                raise ConfigurationError("OAuth requires its exact resource URL and no static Authorization")

    def add(self, server: McpServer, *, _owner: str | None = None) -> None:
        self.validate(server)
        with self._lock:
            self._check(_owner)
            if server.name in self._specs:
                raise ConfigurationError("MCP server already exists")
            self._specs[server.name] = replace(server, env=dict(server.env), headers=dict(server.headers))
            self._start(server.name)

    def _start(self, name: str) -> None:
        from xiaoyu.mcp import McpManager, ServerSpec
        s = self._specs[name]
        directory = self.state_dir / hashlib.sha256(name.encode()).hexdigest()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        manager = McpManager([ServerSpec(name=s.name, command=s.command, args=list(s.args), env=dict(s.env),
            url=s.url, headers=dict(s.headers), timeout=s.timeout,
            authorization=s.oauth.authorization if s.oauth else None)], state_dir=directory,
            inherit_environment=False, use_cache=False)
        self._managers[name] = manager  # Keep ownership even if start fails.
        manager.start()

    def stop(self, name: str, *, _owner: str | None = None) -> None:
        with self._lock:
            self._check(_owner)
            self._require(name)
            if name not in self._managers:
                return
            self._stopping.add(name)
            manager = self._managers[name]
            manager.close()
            if manager.shutdown_pending():
                raise CloseTimeoutError("MCP server is still stopping; retry stop")
            del self._managers[name]
            self._stopping.discard(name)

    def start(self, name: str, *, _owner: str | None = None) -> None:
        with self._lock:
            self._check(_owner)
            self._require(name)
            if name in self._stopping:
                raise McpManagementError("Finish stopping MCP before starting")
            if name not in self._managers:
                self._start(name)

    def remove(self, name: str, *, _owner: str | None = None) -> None:
        with self._lock:
            self.stop(name, _owner=_owner)
            del self._specs[name]
            # Keep the declaration baseline: re-adding the same name cannot
            # silently authorize changed tools.

    def reconnect(self, name: str, *, _owner: str | None = None) -> str:
        with self._lock:
            self._check(_owner)
            self._require(name)
            if name not in self._managers or name in self._stopping:
                raise McpManagementError("MCP server is stopped or stopping")
            self._managers[name].reconnect(name)
            return self.server_states()[name]

    def approve_changes(self, name: str, *, _owner: str | None = None) -> None:
        with self._lock:
            self._check(_owner)
            self._require(name)
            if name not in self._managers or name in self._stopping:
                raise McpManagementError("MCP server is stopped or stopping")
            self._managers[name].approve(name)

    def _require(self, name: str) -> None:
        if name not in self._specs:
            raise McpManagementError("Unknown MCP server")

    def ready_tools(self) -> list[Any]:
        with self._lock:
            tools = [tool for name, manager in self._managers.items() if name not in self._stopping
                     for tool in manager.ready_tools()]
            live = {id(tool) for tool in tools}
            self._wrappers = {key: value for key, value in self._wrappers.items() if key in live}
            for tool in tools:
                if id(tool) not in self._wrappers:
                    def available(remote: Any = tool) -> bool:
                        return self._unambiguous(remote.name, remote.server) and remote.check_fn()
                    self._wrappers[id(tool)] = replace(tool, check_fn=available)
            return [self._wrappers[id(tool)] for tool in tools if self._unambiguous(tool.name, tool.server)]

    def _unambiguous(self, tool_name: str, server: str) -> bool:
        with self._lock:
            owners = [name for name, manager in self._managers.items() if name not in self._stopping
                      for tool in manager.ready_tools() if tool.name == tool_name]
            return owners == [server]

    def loading(self) -> bool:
        with self._lock:
            return any(m.loading() for m in self._managers.values())

    def take_media(self) -> list[dict[str, Any]]:
        with self._lock:
            return [part for manager in self._managers.values() for part in manager.take_media()]

    def server_states(self) -> dict[str, str]:
        with self._lock:
            return {name: ("stopping" if name in self._stopping else
                    self._managers[name].server_states().get(name, "loading") if name in self._managers else "stopped")
                    for name in self._specs}

    def shutdown_pending(self) -> tuple[str, ...]:
        with self._lock:
            if self._closed:
                return self._pending
            return tuple(item for manager in self._managers.values() for item in manager.shutdown_pending())

    def close(self, *, _owner: str | None = None) -> None:
        with self._lock:
            if self._lease is not None and _owner != self._lease:
                raise McpManagementError("Close the borrowing session before its MCP pool")
            self._closed = True
            for manager in self._managers.values():
                manager.close()
            self._pending = tuple(item for manager in self._managers.values() for item in manager.shutdown_pending())
            # Session checks pending itself; retain handles and state on timeout.
            if not self._pending and self._temporary is not None:
                self._temporary.cleanup()

    def __enter__(self) -> McpPool:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
        if self.shutdown_pending():
            raise CloseTimeoutError("MCP resources remain; retry close")
