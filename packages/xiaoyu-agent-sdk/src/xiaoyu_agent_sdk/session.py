"""Session ownership and host adapters; execution stays in xiaoyu.Agent."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import math
import queue
import tempfile
import threading
import time
import uuid
from concurrent.futures import Future, InvalidStateError, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from pathlib import Path
from typing import Any, AsyncGenerator, Generator

from xiaoyu.agent import Agent, Deny, Interrupted
from xiaoyu.config import Config
from xiaoyu.embedding import RunCompleted, RunResult, measured_send
from xiaoyu.events import UIEvent
from xiaoyu.hooks import Decision
from xiaoyu.output import OutputContract, validator_for
from xiaoyu.permissions import Permissions, parse_rule
from xiaoyu.providers import Provider, Registry
from xiaoyu.rewind import RewindResult
from xiaoyu.session_log import SessionInfo, SessionLog, load_messages, list_sessions as _list_sessions
from xiaoyu.tools import Tool as KernelTool, Toolbox

from .types import (
    CloseTimeoutError, ConfigurationError, ExecutionError, OutputSpec,
    SessionBusyError, SessionClosedError, SessionOptions, SessionStorageError,
    ToolResult, McpServerStatus,
)


class _Sink:
    def __init__(self, session: Session) -> None:
        self.session = session

    def emit(self, event: UIEvent) -> None:
        events = self.session._events
        if events is None:
            return
        while not self.session._cancel.is_set():
            try:
                events.put(event, timeout=0.05)
                return
            except queue.Full:
                pass


class _Hooks:
    def __init__(self, session: Session, hooks: tuple) -> None:
        self.session, self.hooks = session, hooks

    def has(self, event: str) -> bool:
        return any(h.event == event for h in self.hooks)

    def for_tools(self, workspace: Path) -> _Hooks:
        return _Hooks(self.session, tuple(h for h in self.hooks if h.event in ("PreToolUse", "PostToolUse")))

    def fire(self, event: str, payload: dict, tool_name: str = "", also: str = "") -> Decision:
        for hook in self.hooks:
            if hook.event != event or hook.tool_name and hook.tool_name not in (tool_name, also):
                continue
            try:
                result = self.session._callback(hook.callback, {"event": event, **copy.deepcopy(payload)})
                if not isinstance(result, Decision):
                    raise TypeError("Hook must return HookDecision")
            except Interrupted:
                raise
            except Exception:
                return Decision(True, "Host hook failed")
            if result.blocked:
                return result
        return Decision(False)


class Session:
    """One serial conversation and its owned tools, log, transport and worker.

    Concurrent submissions fail with SessionBusyError. Use distinct instances
    for parallel conversations. close() is idempotent; a timeout leaves the
    session closing, and close() may be retried.
    """

    def __init__(self, options: SessionOptions, *, resume_from: Path | None = None) -> None:
        self._mutex = threading.RLock()
        self._close_mutex = threading.Lock()
        self._cancel = threading.Event()
        self._future: Future | None = None
        self._cleanup_future: Future | None = None
        self._closing = False
        self._closed = False
        self._events: queue.Queue | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._callbacks: set[Future] = set()
        self._async_settling: set[threading.Event] = set()
        self._stream_active = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xiaoyu-session")
        self._callback_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="xiaoyu-host")
        self._owned_clients: list[Any] = []
        self._log: SessionLog | None = None
        self._mcp: Any = None
        self._mcp_directory: tempfile.TemporaryDirectory | None = None
        self._agent: Agent | None = None
        self._toolbox: Toolbox | None = None
        # Callables and borrowed clients retain identity; mutable configuration does not.
        self.options = replace(
            options, workspace=Path(options.workspace).resolve(),
            tool_env=dict(options.tool_env),
            tools=tuple(replace(t, parameters=copy.deepcopy(t.parameters)) for t in options.tools),
            mcp_servers=tuple(replace(s, env=dict(s.env), headers=dict(s.headers)) for s in options.mcp_servers),
        )
        try:
            self._build(resume_from)
        except BaseException:
            self.close()
            raise

    def _build(self, resume_from: Path | None) -> None:
        options, model = self.options, self.options.model
        if not options.workspace.is_dir():
            raise ConfigurationError("workspace must be an existing directory")
        if not model.model or model.protocol not in ("chat", "responses", "anthropic"):
            raise ConfigurationError("A model and supported protocol are required")
        for value in (model.request_timeout, options.close_timeout, options.approval_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError("Timeouts must be positive")
        if any(type(value) is not int or value < 1 for value in (options.max_iterations, options.event_buffer_size)):
            raise ConfigurationError("Iteration and event buffer limits must be positive")
        if options.budget_tokens is not None and (type(options.budget_tokens) is not int or options.budget_tokens < 1):
            raise ConfigurationError("budget_tokens must be a positive integer")
        if any(bool(s.command) == bool(s.url) or not s.name or not math.isfinite(s.timeout) or s.timeout <= 0
               for s in options.mcp_servers):
            raise ConfigurationError("MCP requires a name, positive timeout and exactly one of command/url")
        if any(h.event not in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop") for h in options.hooks):
            raise ConfigurationError("Unsupported hook event")
        if any(s.isolation not in ("none", "worktree") or s.max_iterations < 1 for s in options.subagents):
            raise ConfigurationError("Subagent isolation must be none/worktree and iterations positive")
        rules = [parse_rule(text) for text in options.deny_rules]
        if any(rule is None or rule.behavior != "deny" for rule in rules):
            raise ConfigurationError("deny_rules must contain valid 'deny ...' rules")
        config = Config(
            base_url=model.base_url, model=model.model, workspace=options.workspace,
            summary_model=model.model, explore_model=model.model,
            request_timeout=model.request_timeout, mode="default", auto_approve=False,
            enable_explore=False, enable_plan=False, enable_skills=bool(options.skill_directories),
            skill_directories=tuple(Path(p).resolve() for p in options.skill_directories),
            enable_web_search=False, enable_browser=False, enable_plugins=False,
            enable_mcp=False, enable_hooks=False, enable_agents=False,
            enable_chenshu=False, enable_peers=False, turn_extension=0,
            load_project_instructions=options.load_project_instructions,
            system_prompt=options.system_prompt, max_iterations=options.max_iterations,
            budget_tokens=options.budget_tokens, extra_env=dict(options.tool_env),
        )
        client = model.client
        if client is None:
            if not model.api_key:
                raise ConfigurationError("api_key must be explicitly supplied")
            import httpx
            from openai import OpenAI
            from xiaoyu.responses import wrap

            base = OpenAI(api_key=model.api_key, base_url=model.base_url,
                          timeout=model.request_timeout, max_retries=0,
                          http_client=httpx.Client(trust_env=False))
            self._owned_clients.append(base)

            def anthropic_factory():
                from anthropic import Anthropic
                other = Anthropic(api_key=model.api_key, base_url=model.base_url,
                                  timeout=model.request_timeout, max_retries=0,
                                  http_client=httpx.Client(trust_env=False))
                self._owned_clients.append(other)
                return other

            client = wrap(base, ("*",) if model.protocol == "responses" else (),
                          ("*",) if model.protocol == "anthropic" else (),
                          anthropic_factory=anthropic_factory, provider="sdk")
        registry = Registry([Provider("sdk", model.base_url, model.api_key)], clients={"sdk": client},
                            inherit_environment=False)
        if options.mcp_servers:
            from xiaoyu.mcp import McpManager, ServerSpec
            if len({s.name for s in options.mcp_servers}) != len(options.mcp_servers):
                raise ConfigurationError("MCP server names must be unique")
            self._mcp_directory = tempfile.TemporaryDirectory(prefix="xiaoyu-sdk-mcp-")
            self._mcp = McpManager([
                ServerSpec(name=s.name, command=s.command, args=list(s.args), env=s.env,
                           url=s.url, headers=s.headers, timeout=s.timeout)
                for s in options.mcp_servers
            ], state_dir=Path(self._mcp_directory.name))
            self._mcp.start()
        self._toolbox = Toolbox(config, only=list(options.builtin_tools) if options.builtin_tools is not None else None,
                                mcp_view=self._mcp)
        if options.plugins:
            from xiaoyu.tools import load_plugin_tools
            selected = tuple((p.distribution, p.name) for p in options.plugins)
            if len(set(selected)) != len(selected):
                raise ConfigurationError("Duplicate plugin selection")
            try:
                plugin_tools = load_plugin_tools(config, selected=selected)
            except Exception as exc:
                raise ConfigurationError("Selected plugin could not be loaded") from exc
            for plugin_tool in plugin_tools:
                if plugin_tool.name == "structured_output" or self._toolbox.get(plugin_tool.name) is not None:
                    raise ConfigurationError(f"Duplicate/reserved plugin tool: {plugin_tool.name}")
                self._toolbox.register(plugin_tool)
        for tool in options.tools:
            if tool.name == "structured_output" or self._toolbox.get(tool.name) is not None:
                raise ConfigurationError(f"Duplicate/reserved tool name: {tool.name}")
            validator = validator_for(tool.parameters)
            if tool.parameters.get("type") != "object":
                raise ConfigurationError("Tool parameter schemas must have type object")

            def handler(_tool=tool, _validator=validator, **args):
                if not _validator.is_valid(args):
                    return "ERROR: Tool arguments do not satisfy the declared schema"
                try:
                    result = self._callback(_tool.handler, **args)
                    error = isinstance(result, ToolResult) and result.is_error
                    content = result.content if isinstance(result, ToolResult) else result
                    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, allow_nan=False)
                    return ("ERROR: " if error else "") + text
                except Interrupted:
                    raise
                except Exception:
                    return "ERROR: Host tool failed"

            self._toolbox.register(KernelTool(tool.name, tool.description, tool.parameters,
                                             handler, tool.requires_approval, coerce_arguments=False))
        if resume_from is not None:
            path = Path(resume_from).resolve()
            if not path.is_file():
                raise SessionStorageError("Session log does not exist")
            self._log = SessionLog(path)
            with path.open(encoding="utf-8") as stream:
                metadata = json.loads(stream.readline())
            if Path(metadata.get("workspace", "")).resolve() != options.workspace:
                raise SessionStorageError("Resume workspace differs; use fork to change workspace")
        elif options.session_dir is not None:
            self._log = SessionLog.create(model.model, str(options.workspace),
                                          directory=Path(options.session_dir), session_id=uuid.uuid4().hex)
        if self._log is not None:
            # CLI may degrade to unlocked/broken logs. SDK persistence is fail-closed.
            if not self._log.locked or self._log.broken_reason:
                raise SessionStorageError("Cannot lock or write session log")
        self._agent = Agent(config, self._toolbox, registry=registry, approver=self._approve,
                            sink=_Sink(self), permissions=Permissions(options.workspace, [r for r in rules if r is not None]),
                            hook_engine=_Hooks(self, options.hooks), session_log=self._log,
                            upstream_stop=self._cancel.is_set)
        if resume_from is not None:
            assert self._log is not None
            messages = load_messages(self._log.path)
            if messages.corrupt_lines:
                raise SessionStorageError("Session contains corrupt records")
            self._agent.restore(messages, source=str(self._log.path), copy=False)
        if options.subagents:
            from xiaoyu.agents import AgentSpec, ParentGuards, make_subagent_tool
            agent = self._agent
            for spec in options.subagents:
                child_tool = make_subagent_tool(
                    AgentSpec(spec.name, spec.description, spec.system_prompt, spec.tools,
                              model=spec.model, max_iterations=spec.max_iterations,
                              isolation=spec.isolation, require_isolation=spec.isolation == "worktree"),
                    config, registry, self._agent.usage, self._agent.sink,
                    self._approve, self._agent.permissions,
                    stop_requested=self._agent.interrupt_requested,
                    guards=ParentGuards(mode=lambda: "default", hooks=lambda: agent.hook_engine),
                )
                if self._toolbox.get(child_tool.name) is not None:
                    raise ConfigurationError("Duplicate subagent tool name")
                self._toolbox.register(child_tool)

    def _callback(self, fn, *args, timeout: float | None = None, **kwargs):
        if self._cancel.is_set():
            raise Interrupted()
        if inspect.iscoroutinefunction(fn):
            if self._loop is None:
                raise ConfigurationError("Async callbacks require AsyncSession")
            future: Future[Any] = Future()
            settled = threading.Event()
            with self._mutex:
                self._async_settling.add(settled)

            def launch():
                if future.cancelled():
                    settled.set()
                    return
                task = self._loop.create_task(fn(*args, **kwargs))

                def finish(task):
                    try:
                        if task.cancelled():
                            future.cancel()
                        elif not future.done():
                            error = task.exception()
                            if error is not None:
                                future.set_exception(error)
                            else:
                                future.set_result(task.result())
                        else:
                            task.exception()  # Retrieve failures after host cancellation.
                    except InvalidStateError:
                        pass  # Cancellation raced with delivering the callback result.
                    finally:
                        settled.set()

                task.add_done_callback(finish)
                future.add_done_callback(lambda f: self._loop.call_soon_threadsafe(task.cancel) if f.cancelled() else None)

            self._loop.call_soon_threadsafe(launch)
        else:
            future = self._callback_pool.submit(fn, *args, **kwargs)
        with self._mutex:
            self._callbacks.add(future)
        deadline = time.monotonic() + timeout if timeout is not None else None
        try:
            while True:
                if self._cancel.is_set():
                    future.cancel()
                    raise Interrupted()
                try:
                    result = future.result(timeout=0.05)
                    if inspect.isawaitable(result):
                        if inspect.iscoroutine(result):
                            result.close()
                        raise ConfigurationError("Use an async def callback for async work")
                    return result
                except FutureTimeout:
                    if future.done():
                        raise  # The callback itself raised TimeoutError.
                    if deadline is not None and time.monotonic() >= deadline:
                        future.cancel()
                        raise TimeoutError("Host callback timed out") from None
        finally:
            # Keep running synchronous callbacks owned until they actually finish.
            future.add_done_callback(self._callback_done)

    def _callback_done(self, future: Future) -> None:
        with self._mutex:
            self._callbacks.discard(future)

    def _approve(self, name: str, args: dict):
        if self.options.approver is None:
            return Deny("Host approval is required")
        try:
            return self._callback(self.options.approver, name, copy.deepcopy(args),
                                  timeout=self.options.approval_timeout)
        except Interrupted:
            raise
        except Exception:
            return Deny("Host approval failed or timed out")

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def session_path(self) -> Path | None:
        return self._log.path if self._log else None

    def _check_idle(self) -> None:
        if self._closed or self._closing:
            raise SessionClosedError("Session is closed or closing")
        if self._stream_active or self._future is not None and not self._future.done():
            raise SessionBusyError("A turn is already running")
        self._async_settling = {e for e in self._async_settling if not e.is_set()}
        self._callbacks = {f for f in self._callbacks if not f.done()}
        if self._callbacks or self._async_settling:
            raise SessionBusyError("A host callback is still settling")

    def _start(self, prompt: str, output: OutputSpec | None, stream: bool = False) -> Future:
        # Invalid schema never mutates history or starts execution.
        contract = OutputContract(output.schema, output.max_retries) if output else None
        if self._loop is None and any(inspect.iscoroutinefunction(fn) for fn in (
            self.options.approver, *(t.handler for t in self.options.tools),
            *(h.callback for h in self.options.hooks),
        )):
            raise ConfigurationError("Async callbacks require AsyncSession")
        with self._mutex:
            self._check_idle()
            self._cancel.clear()
            self._events = queue.Queue(self.options.event_buffer_size) if stream else None
            self._stream_active = stream
            self._future = self._executor.submit(self._execute, prompt, contract)
            return self._future

    def _execute(self, prompt: str, contract: OutputContract | None) -> RunResult:
        assert self._agent is not None
        try:
            result = measured_send(self._agent, prompt, output_contract=contract)
            if result.interrupted:
                self._wait_callbacks(only_async=True)
            if self._log and self._log.broken_reason:
                raise SessionStorageError("Session log write failed")
            return result
        except (SessionStorageError, CloseTimeoutError):
            raise
        except Exception as exc:
            self._agent.close_open_tool_calls("Execution failed; outcome may be incomplete.")
            raise ExecutionError(f"Execution failed ({type(exc).__name__})") from exc

    def run(self, prompt: str, *, output: OutputSpec | None = None) -> RunResult:
        return self._start(prompt, output).result()

    def _next_event(self, future: Future):
        assert self._events is not None
        try:
            return self._events.get(timeout=0.05)
        except queue.Empty:
            return RunCompleted(future.result()) if future.done() else None

    def stream(self, prompt: str, *, output: OutputSpec | None = None) -> Generator[UIEvent, None, None]:
        future = self._start(prompt, output, stream=True)
        try:
            while True:
                event = self._next_event(future)
                if event is not None:
                    yield event
                    if isinstance(event, RunCompleted):
                        break
        finally:
            try:
                if not future.done():
                    self.interrupt()
                    self._wait_turn(future)
            finally:
                self._stream_active = False

    def interrupt(self) -> None:
        self._cancel.set()
        if self._agent is not None:
            self._agent.interrupt()

    def _wait_turn(self, future: Future) -> None:
        try:
            future.result(timeout=self.options.close_timeout)
        except FutureTimeout:
            raise CloseTimeoutError("Execution has not stopped yet") from None
        except Exception:
            # Execution failure is reported by run/stream; cleanup still proceeds.
            pass
        self._wait_callbacks()

    def _wait_callbacks(self, *, only_async: bool = False) -> None:
        deadline = time.monotonic() + self.options.close_timeout
        while True:
            with self._mutex:
                pending = (not only_async and any(not f.done() for f in self._callbacks)) or any(
                    not e.is_set() for e in self._async_settling
                )
            if not pending:
                return
            if time.monotonic() >= deadline:
                raise CloseTimeoutError("A host callback has not stopped yet")
            time.sleep(0.01)

    def fork(self, *, options: SessionOptions | None = None) -> Session:
        with self._mutex:
            self._check_idle()
            assert self._agent is not None
            messages = copy.deepcopy(self._agent.messages[1:])
        child = Session(options or self.options)
        try:
            assert child._agent is not None
            child._agent.restore(messages, source=str(self.session_path or "memory"))
        except BaseException:
            child.close()
            raise
        return child

    def checkpoints(self) -> tuple[int, ...]:
        with self._mutex:
            self._check_idle()
            assert self._toolbox is not None
            return tuple(p.index for p in self._toolbox.rewind.points())

    def rewind(self, index: int, *, conversation: bool = True, files: bool = True) -> RewindResult:
        with self._mutex:
            self._check_idle()
            assert self._toolbox is not None and self._agent is not None
            return self._agent.rewind_result(index, conversation=conversation, files=files)

    def mcp_status(self) -> tuple[McpServerStatus, ...]:
        """Read immutable state snapshots; never include server error bodies/headers."""
        if self._mcp is None:
            return ()
        return tuple(McpServerStatus(name, state) for name, state in self._mcp.server_states().items())

    def close(self) -> None:
        with self._close_mutex:
            self._close()

    def _close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._closing = True
            self.interrupt()
            future = self._future
        if future is not None:
            self._wait_turn(future)
        self._wait_callbacks()
        if self._cleanup_future is None:
            self._cleanup_future = self._executor.submit(self._release_resources)
        try:
            pending = self._cleanup_future.result(timeout=self.options.close_timeout)
        except FutureTimeout:
            if self._cleanup_future.done():
                self._cleanup_future = None
            raise CloseTimeoutError("Resource cleanup is still pending; retry close") from None
        except Exception as exc:
            self._cleanup_future = None
            raise SessionStorageError("Resource cleanup failed; retry close") from exc
        if pending:
            self._cleanup_future = None
            raise CloseTimeoutError("Resources still stopping: " + ", ".join(pending))
        self._executor.shutdown(wait=True)
        self._callback_pool.shutdown(wait=True)
        self._closed = True

    def _release_resources(self) -> tuple[str, ...]:
        pending: tuple[str, ...] = ()
        if self._toolbox is not None:
            self._toolbox.tasks.shutdown()
            pending += self._toolbox.tasks.shutdown_pending()
        if self._mcp is not None:
            self._mcp.close()
            pending += self._mcp.shutdown_pending()
        if pending:
            return pending
        if self._mcp_directory is not None:
            self._mcp_directory.cleanup()
        while self._owned_clients:
            self._owned_clients[-1].close()
            self._owned_clients.pop()
        if self._log is not None:
            self._log.close()
        return ()

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class AsyncSession:
    """Async host interface; callbacks run on the caller's event loop."""

    def __init__(self, options: SessionOptions, *, resume_from: Path | None = None) -> None:
        self._session = Session(options, resume_from=resume_from)

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        old = self._session._loop
        if old is not None and old is not loop:
            raise ConfigurationError("AsyncSession belongs to a different event loop")
        self._session._loop = loop

    @property
    def closed(self) -> bool:
        return self._session.closed

    @property
    def session_path(self) -> Path | None:
        return self._session.session_path

    async def run(self, prompt: str, *, output: OutputSpec | None = None) -> RunResult:
        self._bind_loop()
        future = self._session._start(prompt, output)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            self.interrupt()
            await asyncio.to_thread(self._session._wait_turn, future)
            raise

    async def stream(self, prompt: str, *, output: OutputSpec | None = None) -> AsyncGenerator[UIEvent, None]:
        self._bind_loop()
        future = self._session._start(prompt, output, stream=True)
        try:
            while True:
                event = await asyncio.to_thread(self._session._next_event, future)
                if event is not None:
                    yield event
                    if isinstance(event, RunCompleted):
                        break
        finally:
            try:
                if not future.done():
                    self.interrupt()
                    await asyncio.to_thread(self._session._wait_turn, future)
            finally:
                self._session._stream_active = False

    def interrupt(self) -> None:
        self._session.interrupt()

    async def close(self) -> None:
        await asyncio.shield(asyncio.to_thread(self._session.close))

    async def fork(self, *, options: SessionOptions | None = None) -> AsyncSession:
        self._bind_loop()
        child = object.__new__(AsyncSession)
        child._session = await asyncio.to_thread(self._session.fork, options=options)
        return child

    async def rewind(self, index: int, *, conversation: bool = True, files: bool = True) -> RewindResult:
        return await asyncio.to_thread(self._session.rewind, index, conversation=conversation, files=files)

    def checkpoints(self) -> tuple[int, ...]:
        return self._session.checkpoints()

    def mcp_status(self) -> tuple[McpServerStatus, ...]:
        return self._session.mcp_status()

    async def __aenter__(self) -> AsyncSession:
        self._bind_loop()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


def run(prompt: str, options: SessionOptions, *, output: OutputSpec | None = None) -> RunResult:
    with Session(options) as session:
        return session.run(prompt, output=output)


async def run_async(prompt: str, options: SessionOptions, *, output: OutputSpec | None = None) -> RunResult:
    async with AsyncSession(options) as session:
        return await session.run(prompt, output=output)


def list_sessions(directory: Path, *, limit: int = 20, workspace: Path | None = None) -> list[SessionInfo]:
    """List only the explicitly selected host session directory."""
    return _list_sessions(limit, str(workspace.resolve()) if workspace else None, directory=Path(directory))
