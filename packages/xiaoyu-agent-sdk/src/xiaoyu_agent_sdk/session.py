"""Session ownership and host adapters; execution stays in xiaoyu.Agent."""
from __future__ import annotations

import asyncio
import copy
from contextlib import nullcontext
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
from typing import Any, AsyncGenerator, Generator, cast

from xiaoyu.agent import Agent, Deny, Interrupted
from xiaoyu.config import Config
from xiaoyu.embedding import RunCompleted, RunResult, measured_send
from xiaoyu.events import UIEvent
from xiaoyu.hooks import Decision
from xiaoyu.output import OutputContract, validator_for
from xiaoyu.permissions import Permissions, parse_rule, call_identity
from xiaoyu.providers import Provider, Registry
from xiaoyu.rewind import RewindResult
from xiaoyu.session_log import SessionInfo, SessionLog, SessionLockedError, load_messages, replay_records, list_sessions as _list_sessions
from xiaoyu.tools import Tool as KernelTool, Toolbox

from .types import (
    CloseTimeoutError, ConfigurationError, ExecutionError, OutputSpec,
    SessionBusyError, SessionClosedError, SessionOptions, SessionStorageError,
    ToolResult, ToolOutput, ResultTransform, ResultTransformError, McpServerStatus, SessionMode,
)
from .storage import _StoreLog, session_metadata
from .questions import QuestionOptions, QuestionManager, AsyncQuestionManager
from .tasks import TaskManager, TaskSpec, TaskHandle, TaskSnapshot
from .types import McpServer
from .budget import _Ledger, _MeteredClient, BudgetExceededError, CostSnapshot, BudgetOptions
from .telemetry import _Telemetry
from .inputs import Prompt, normalize_prompt
from .observation import (
    MessageSnapshot, ModelUsageSnapshot, Notification, PlanStep, SessionSnapshot,
    SessionState, UsageSnapshot, _Notifications,
)


class _Sink:
    def __init__(self, session: Session, streaming: bool = True) -> None:
        self.session = session
        self.streaming = streaming

    def quiet_child(self):
        return _Sink(self.session, False)

    def emit(self, event: UIEvent) -> None:
        if event.kind == "plan.updated":
            # The kernel retains this list; a host may edit its event payload.
            event = copy.deepcopy(event)
        context = self.session._context
        if event.kind == "request.started":
            context.request_id = uuid.uuid4().hex
            context.request_pending = True
        if event.kind == "tool.pending":
            stack = getattr(context, "tool_stack", None)
            if stack is None:
                context.tool_stack = stack = []
            stack.append(getattr(context, "tool_call_id", ""))
            context.tool_call_id = event.tool_call_id
        event = replace(event, session_id=self.session.session_id,
            run_id=getattr(context, "run_id", ""), task_id=getattr(context, "task_id", ""),
            request_id=getattr(context, "request_id", ""),
            tool_call_id=event.tool_call_id or getattr(context, "tool_call_id", ""))
        if self.session._telemetry is not None and event.kind not in {"request.started", "request.ended"}:
            self.session._telemetry.observe(event.kind, name=getattr(event, "name", getattr(event, "model", "")),
                                            failed=not getattr(event, "ok", True), tool_call_id=event.tool_call_id)
        if event.kind in {"tool.completed", "tool.denied"}:
            stack = getattr(context, "tool_stack", [])
            context.tool_call_id = stack.pop() if stack else ""
        if not self.streaming:
            return
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
        parent_input = (event == "UserPromptSubmit" and self.session._agent is not None
                        and self.session._agent.hook_engine is self)
        return parent_input or (self.session._telemetry is not None and event in {"SubagentStart", "SubagentEnd", "BeforeCompact", "AfterCompact"}) or any(h.event == event for h in self.hooks)

    def for_tools(self, workspace: Path) -> _Hooks:
        return _Hooks(self.session, tuple(h for h in self.hooks if h.event in ("PreToolUse", "PostToolUse", "ToolFailed", "BeforeCompact", "AfterCompact")))

    def fire(self, event: str, payload: dict, tool_name: str = "", also: str = "") -> Decision:
        if (event == "UserPromptSubmit" and self.session._agent is not None
                and self.session._agent.hook_engine is self):
            # The kernel has now cleared stale input from the previous turn.
            with self.session._mutex:
                self.session._steer_open = True
        if self.session._telemetry is not None:
            self.session._telemetry.observe(event, name=payload.get("agent", ""), failed=payload.get("failed", False))
        for hook in self.hooks:
            if hook.event != event or hook.tool_name and hook.tool_name not in (tool_name, also):
                continue
            try:
                result = self.session._callback(hook.callback, {"event": event, "session_id": self.session.session_id,
                    "run_id": getattr(self.session._context, "run_id", ""), "task_id": getattr(self.session._context, "task_id", ""),
                    **copy.deepcopy(payload)}, timeout=self.session.options.approval_timeout,
                    _ignore_cancel=event in {"SessionEnd", "SubagentEnd"})
                if not isinstance(result, Decision):
                    raise TypeError("Hook must return HookDecision")
            except Interrupted:
                raise
            except Exception:
                if event in {"SubagentEnd", "ToolFailed", "AfterCompact"}:
                    self.session._hook_errors.append(event)
                return Decision(True, "Host hook failed")
            if result.blocked:
                return result
        if (event == "UserPromptSubmit" and self.session._questions is not None
                and self.session._agent is not None and self.session._agent.hook_engine is self):
            self.session._questions._deliver()
        return Decision(False)


class Session:
    """One serial conversation and its owned tools, log, transport and worker.

    Concurrent submissions fail with SessionBusyError. Use distinct instances
    for parallel conversations. close() is idempotent; a timeout leaves the
    session closing, and close() may be retried.
    """

    def __init__(self, options: SessionOptions, *, resume_from: Path | None = None, resume_id: str | None = None) -> None:
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
        self._steer_open = False
        self._undelivered_steers: list[str] = []
        self._notifications = _Notifications()
        self._usage_lock = threading.Lock()
        self._control_name = ""
        self._control_error = False
        self._plan_directory: tempfile.TemporaryDirectory | None = None
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="xiaoyu-session")
        self._callback_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="xiaoyu-host")
        self._owned_clients: list[Any] = []
        self._log: SessionLog | _StoreLog | None = None
        self._session_id = resume_id or uuid.uuid4().hex
        self._mcp: Any = None
        self._mcp_borrowed = False
        self._mcp_owner = self._session_id
        self._tasks: TaskManager | None = None
        self._questions: QuestionManager | None = None
        self._has_question_history = False
        self._started = False
        self._end_hook_sent = False
        self._hook_errors: list[str] = []
        self._context = threading.local()
        self._run_id = ""
        self._ledger: _Ledger | None = None
        self._telemetry: _Telemetry | None = None
        self._mcp_directory: tempfile.TemporaryDirectory | None = None
        self._agent: Agent | None = None
        self._toolbox: Toolbox | None = None
        # Callables and borrowed clients retain identity; mutable configuration does not.
        self.options = replace(
            options, workspace=Path(options.workspace).resolve(),
            tool_env=dict(options.tool_env),
            result_transforms=tuple(options.result_transforms),
            tools=tuple(replace(t, parameters=copy.deepcopy(t.parameters)) for t in options.tools),
            mcp_servers=tuple(replace(s, env=dict(s.env), headers=dict(s.headers)) for s in options.mcp_servers),
            budget=replace(options.budget, prices=dict(options.budget.prices)) if options.budget else None,
        )
        try:
            self._build(resume_from, resume_id)
        except BaseException:
            self.close()
            raise

    def _build(self, resume_from: Path | None, resume_id: str | None) -> None:
        options, model = self.options, self.options.model
        if options.telemetry is not None:
            self._telemetry = _Telemetry(options.telemetry)
        if options.session_store is not None and (options.session_dir is not None or resume_from is not None):
            raise ConfigurationError("session_store cannot be combined with local session paths")
        if resume_id is not None and (options.session_store is None or not resume_id):
            raise ConfigurationError("resume_id requires session_store and a nonempty ID")
        if not options.workspace.is_dir():
            raise ConfigurationError("workspace must be an existing directory")
        if not model.model or model.protocol not in ("chat", "responses", "anthropic"):
            raise ConfigurationError("A model and supported protocol are required")
        if options.mode not in ("default", "auto", "plan"):
            raise ConfigurationError("mode must be default, auto or plan")
        if type(options.enable_plan) is not bool:
            raise ConfigurationError("enable_plan must be a boolean")
        if options.questions is not None:
            from .storage import SQLiteSessionStore
            if not isinstance(options.questions, QuestionOptions) or not isinstance(options.session_store, SQLiteSessionStore):
                raise ConfigurationError("Durable questions currently require QuestionOptions and SQLiteSessionStore")
            if options.asker is not None:
                raise ConfigurationError("Use questions or asker, not both")
        if (type(options.result_transform_timeout) not in (int, float)
                or not math.isfinite(options.result_transform_timeout) or options.result_transform_timeout <= 0):
            raise ConfigurationError("result_transform_timeout must be finite and positive")
        if any(not isinstance(t, ResultTransform) or not isinstance(t.name, str) or not t.name.strip()
               or not callable(t.callback) or not isinstance(t.tool_name, str) for t in options.result_transforms):
            raise ConfigurationError("Result transforms require a name, callback and optional tool name")
        if len({t.name for t in options.result_transforms}) != len(options.result_transforms):
            raise ConfigurationError("Result transform names must be unique")
        for value in (model.request_timeout, options.close_timeout, options.approval_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ConfigurationError("Timeouts must be positive")
        if (type(options.question_timeout) not in (int, float)
                or not math.isfinite(options.question_timeout) or options.question_timeout <= 0):
            raise ConfigurationError("question_timeout must be finite and positive")
        if options.asker is not None and not callable(options.asker):
            raise ConfigurationError("asker must be callable")
        if any(type(value) is not int or value < 1 for value in (options.max_iterations, options.event_buffer_size, options.max_parallel_tasks)):
            raise ConfigurationError("Iteration and event buffer limits must be positive")
        if options.budget_tokens is not None and (type(options.budget_tokens) is not int or options.budget_tokens < 1):
            raise ConfigurationError("budget_tokens must be a positive integer")
        if any(bool(s.command) == bool(s.url) or not s.name or not math.isfinite(s.timeout) or s.timeout <= 0
               for s in options.mcp_servers):
            raise ConfigurationError("MCP requires a name, positive timeout and exactly one of command/url")
        if any(h.event not in ("PreToolUse", "PostToolUse", "UserPromptSubmit", "Stop", "SessionStart", "SessionEnd",
                              "SubagentStart", "SubagentEnd", "ToolFailed", "BeforeCompact", "AfterCompact") for h in options.hooks):
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
            enable_explore=False, enable_plan=options.enable_plan, enable_skills=bool(options.skill_directories),
            skill_directories=tuple(Path(p).resolve() for p in options.skill_directories),
            enable_web_search=False, enable_browser=False, enable_plugins=False,
            enable_x_search=False, enable_deep_research=False,
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
            import httpx2
            from openai import OpenAI
            from xiaoyu.responses import wrap

            base = OpenAI(api_key=model.api_key, base_url=model.base_url,
                          timeout=model.request_timeout, max_retries=0,
                          http_client=httpx2.Client(trust_env=False))
            self._owned_clients.append(base)

            def anthropic_factory():
                from anthropic import Anthropic
                other = Anthropic(api_key=model.api_key, base_url=model.base_url,
                                  timeout=model.request_timeout, max_retries=0,
                                  http_client=httpx2.Client(trust_env=False))
                self._owned_clients.append(other)
                return other

            client = wrap(base, ("*",) if model.protocol == "responses" else (),
                          ("*",) if model.protocol == "anthropic" else (),
                          anthropic_factory=anthropic_factory, provider="sdk")
        if options.budget is not None or options.telemetry is not None:
            self._ledger = _Ledger(options.budget or BudgetOptions(), self._journal, self._context)
            client = _MeteredClient(client, self._ledger, self._telemetry)
        registry = Registry([Provider("sdk", model.base_url, model.api_key)], clients={"sdk": client},
                            inherit_environment=False)
        if options.mcp_servers and options.mcp_pool is not None:
            raise ConfigurationError("Use mcp_servers or a borrowed mcp_pool")
        if options.mcp_servers or options.mcp_pool is not None:
            from .mcp import McpPool
            pool = options.mcp_pool if options.mcp_pool is not None else McpPool(options.mcp_servers)
            pool.acquire(self._session_id)
            self._mcp = pool
            self._mcp_borrowed = options.mcp_pool is not None
        self._toolbox = Toolbox(config, only=list(options.builtin_tools) if options.builtin_tools is not None else None,
                                mcp_view=self._mcp)
        self._toolbox.result_transform = self._transform_result if options.result_transforms else None
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
                if (plugin_tool.name == "structured_output"
                        or options.enable_plan and plugin_tool.name == "update_plan"
                        or options.questions is not None and plugin_tool.name == "ask_user"
                        or self._toolbox.get(plugin_tool.name) is not None):
                    raise ConfigurationError(f"Duplicate/reserved plugin tool: {plugin_tool.name}")
                self._toolbox.register(plugin_tool)
        for tool in options.tools:
            if (tool.name == "structured_output"
                    or options.enable_plan and tool.name == "update_plan"
                    or options.questions is not None and tool.name == "ask_user"
                    or self._toolbox.get(tool.name) is not None):
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
        stored_records = None
        if options.session_store is not None:
            try:
                writer = options.session_store.open(self._session_id,
                    metadata=session_metadata(self._session_id, model.model, options.workspace),
                    resume=resume_id is not None)
                self._log = _StoreLog(writer)
                stored_records = writer.read()
                if not stored_records or stored_records[0].get("event") != "meta":
                    raise SessionStorageError("Stored session metadata is missing")
                metadata = stored_records[0]
                if metadata.get("session_id") != self._session_id:
                    raise SessionStorageError("Stored session identity differs")
                if metadata.get("workspace") != str(options.workspace):
                    raise SessionStorageError("Resume workspace differs; use fork to change workspace")
                if sum(record.get("event") == "meta" for record in stored_records) != 1:
                    raise SessionStorageError("Stored session metadata is ambiguous")
                replay_records(stored_records)  # Reject newer formats before any writes.
            except (SessionStorageError, SessionLockedError):
                if isinstance(self._log, _StoreLog):
                    self._log.broken_reason = "Stored session could not be loaded"
                raise
            except Exception as exc:
                if isinstance(self._log, _StoreLog):
                    self._log.broken_reason = "Stored session could not be loaded"
                raise SessionStorageError("Cannot open stored session") from exc
        elif resume_from is not None:
            path = Path(resume_from).resolve()
            if not path.is_file():
                raise SessionStorageError("Session log does not exist")
            self._log = SessionLog(path)
            with path.open(encoding="utf-8", errors="replace") as stream:
                metadata = json.loads(stream.readline())
            self._session_id = metadata.get("session_id") or self._session_id
            if Path(metadata.get("workspace", "")).resolve() != options.workspace:
                raise SessionStorageError("Resume workspace differs; use fork to change workspace")
        elif options.session_dir is not None:
            self._log = SessionLog.create(model.model, str(options.workspace),
                                          directory=Path(options.session_dir), session_id=self._session_id)
        if self._log is not None:
            # CLI may degrade to unlocked/broken logs. SDK persistence is fail-closed.
            if not self._log.locked or self._log.broken_reason:
                raise SessionStorageError("Cannot lock or write session log")
        if resume_from is not None:
            assert isinstance(self._log, SessionLog)
            messages = load_messages(self._log.path)
            if messages.corrupt_lines:
                raise SessionStorageError("Session contains corrupt records")
            stored_records = []
            with self._log.path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # load_messages already validated the torn-tail contract.
                    stored_records.append(record)
        try:
            self._tasks = TaskManager(self, stored_records or [])
            if self._ledger is not None:
                self._ledger.restore(stored_records or [])
        except Exception as exc:
            if isinstance(self._log, _StoreLog):
                self._log.broken_reason = "Stored platform state could not be loaded"
            raise SessionStorageError("Cannot restore platform state") from exc
        self._agent = Agent(config, self._toolbox, registry=registry, approver=self._approve,
                            asker=self._ask if options.asker is not None else None,
                            sink=_Sink(self), permissions=Permissions(options.workspace, [r for r in rules if r is not None]),
                            hook_engine=_Hooks(self, options.hooks), session_log=self._log,
                            upstream_stop=self._cancel.is_set)
        self._agent.on_notification = lambda _: self._notifications.changed()
        self._has_question_history = options.questions is not None or any(
            r.get("event") in {"sdk.question", "sdk.question.delivery"} for r in stored_records or [])
        if options.questions is not None:
            self._questions = QuestionManager(self, stored_records or [])
            self._agent._on_step_input = self._questions._deliver
            ask_tool = self._toolbox.get("ask_user")
            assert ask_tool is not None
            self._toolbox.register(replace(ask_tool, handler=self._questions._ask, check_fn=lambda: True,
                description=ask_tool.description + " 问题会持久化并按宿主配置等待回答；超时返回 pending，回答在安全的 step 边界或下一轮接纳。"))
        self._restore_usage(stored_records or [])
        if resume_from is not None:
            assert isinstance(self._log, SessionLog)
            messages = load_messages(self._log.path)
            if messages.corrupt_lines:
                raise SessionStorageError("Session contains corrupt records")
            self._agent.restore(messages, source=str(self._log.path), copy=False)
        elif resume_id is not None:
            try:
                assert stored_records is not None
                messages = replay_records(stored_records)
                self._agent.restore(messages, copy=False)
            except SessionStorageError:
                raise
            except Exception as exc:
                raise SessionStorageError("Cannot restore stored session") from exc
        # Host options remain authoritative on resume: never restore credentials
        # or a less restrictive permission mode from a conversation log.
        saved_mode = "default"
        for record in stored_records or []:
            if record.get("event") == "mode":
                saved_mode = record.get("value", "default")
            elif record.get("event") == "sdk.control" and record.get("mode") in ("default", "auto", "plan"):
                saved_mode = record["mode"]
        if saved_mode == "plan" and options.mode != "plan":
            self._agent.mode = "plan"
        self._apply_mode(options.mode)
        if self._log is not None and self._log.broken_reason:
            raise SessionStorageError("Session mode reconciliation could not be persisted")
        if options.subagents:
            from xiaoyu.agents import AgentSpec, ParentGuards, make_subagent_tool
            agent = self._agent
            if len({s.name for s in options.subagents}) != len(options.subagents):
                raise ConfigurationError("Subagent names must be unique")
            for spec in options.subagents:
                child_tool = make_subagent_tool(
                    AgentSpec(spec.name, spec.description, spec.system_prompt, spec.tools,
                              model=spec.model, max_iterations=spec.max_iterations,
                              isolation=spec.isolation, require_isolation=spec.isolation == "worktree",
                              mcp_mode="named" if spec.mcp_servers else "none", mcp_servers=spec.mcp_servers),
                    config, registry, self._agent.usage, self._agent.sink,
                    self._approve, self._agent.permissions,
                    runs=self._tasks.runs, mcp_manager=self._mcp,
                    on_settled=self._tasks.retain_pending_child,
                    stop_requested=self._agent.interrupt_requested,
                    guards=ParentGuards(mode=lambda: agent.mode, hooks=lambda: agent.hook_engine,
                                        result_transform=self._toolbox.result_transform),
                )
                if self._toolbox.get(child_tool.name) is not None:
                    raise ConfigurationError("Duplicate subagent tool name")
                self._toolbox.register(child_tool)

    def _transform_result(self, name: str, args: dict[str, Any], text: str) -> str:
        target, _ = call_identity(name, args)
        for index, transform in enumerate(self.options.result_transforms):
            if transform.tool_name and transform.tool_name not in (name, target):
                continue
            fields: dict[str, Any] = dict(name=transform.name, index=index, tool=name,
                          run_id=getattr(self._context, "run_id", ""),
                          task_id=getattr(self._context, "task_id", ""),
                          tool_call_id=getattr(self._context, "tool_call_id", ""))
            try:
                if not isinstance(text, str):
                    raise TypeError("Tool output must be text")
                failed = text.startswith("ERROR:")
                value = self._callback(transform.callback, ToolOutput(
                    name, text, failed, self.session_id, fields["run_id"], fields["task_id"],
                    fields["tool_call_id"]), timeout=self.options.result_transform_timeout)
                if not isinstance(value, str) or (not failed and value.startswith("ERROR:")):
                    raise TypeError("Transform must return text without changing execution status")
                text = "ERROR: " + value if failed and not value.startswith("ERROR:") else value
                self._journal("sdk.result_transform", status="applied", **fields)
            except Interrupted:
                raise
            except BaseException:
                # Neither exception messages nor intermediate text are safe to publish.
                try:
                    self._journal("sdk.result_transform", status="failed", **fields)
                except Exception:
                    pass  # A broken store already prevents further execution.
                raise ResultTransformError("Tool executed; result transform failed; output withheld") from None
        return text

    def _callback(self, fn, *args, timeout: float | None = None, _ignore_cancel: bool = False, **kwargs):
        task_stop = getattr(self._context, "task_stop", None)
        def cancelled():
            return not _ignore_cancel and (self._cancel.is_set() or task_stop is not None and task_stop())
        if cancelled():
            raise Interrupted()
        settled: threading.Event | None = None
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
        task_callbacks = getattr(self._context, "task_callbacks", None)
        if task_callbacks is not None:
            task_callbacks.append((future, settled))
        with self._mutex:
            self._callbacks.add(future)
        deadline = time.monotonic() + timeout if timeout is not None else None
        try:
            while True:
                if cancelled():
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
        span = self._telemetry.span("tool.approval", tool=name, tool_call_id=getattr(self._context, "tool_call_id", "")) if self._telemetry else nullcontext()
        with span:
            return self._approve_inner(name, args)

    def _approve_inner(self, name: str, args: dict):
        if self.options.approver is None:
            return Deny("Host approval is required")
        try:
            return self._callback(self.options.approver, name, copy.deepcopy(args),
                                  timeout=self.options.approval_timeout)
        except Interrupted:
            raise
        except Exception:
            return Deny("Host approval failed or timed out")

    def _ask(self, questions: list[dict[str, Any]]) -> dict[str, str]:
        # Answers are user input, never permission decisions. Keep the kernel's
        # normalized questions intact even if a host UI mutates its copy.
        allowed = {item["question"] for item in questions}
        try:
            answers = self._callback(self.options.asker, copy.deepcopy(questions),
                                     timeout=self.options.question_timeout)
            if (not isinstance(answers, dict)
                    or any(not isinstance(key, str) or key not in allowed
                           or not isinstance(value, str) for key, value in answers.items())):
                raise ValueError("Invalid question answers")
            return dict(answers)
        except Interrupted:
            raise
        except Exception:
            # Do not turn a failed UI into a user dismissal or expose its error.
            raise RuntimeError("Host question failed or timed out") from None

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def session_path(self) -> Path | None:
        return self._log.path if self._log else None

    def _check_idle(self) -> None:
        if self._closed or self._closing:
            raise SessionClosedError("Session is closed or closing")
        if self._log is not None and self._log.broken_reason:
            raise SessionStorageError("Session persistence failed; close and inspect storage")
        if self._control_error:
            raise ExecutionError("A control operation failed; close and reopen the session")
        if self._tasks is not None and self._tasks.active():
            raise SessionBusyError("Child tasks are still active")
        if self._stream_active or self._future is not None and not self._future.done():
            raise SessionBusyError("A turn is already running")
        self._async_settling = {e for e in self._async_settling if not e.is_set()}
        self._callbacks = {f for f in self._callbacks if not f.done()}
        if self._callbacks or self._async_settling:
            raise SessionBusyError("A host callback is still settling")

    def _start(self, prompt: Prompt, output: OutputSpec | None, stream: bool = False) -> Future:
        content = normalize_prompt(prompt)
        # Invalid schema never mutates history or starts execution.
        contract = OutputContract(output.schema, output.max_retries) if output else None
        if self._loop is None and any(inspect.iscoroutinefunction(fn) for fn in (
            self.options.approver, self.options.asker, *(t.handler for t in self.options.tools),
            *(t.callback for t in self.options.result_transforms),
            *(h.callback for h in self.options.hooks),
        )):
            raise ConfigurationError("Async callbacks require AsyncSession")
        with self._mutex:
            self._check_idle()
            self._cancel.clear()
            self._run_id = uuid.uuid4().hex
            self._events = queue.Queue(self.options.event_buffer_size) if stream else None
            self._stream_active = stream
            self._future = self._executor.submit(self._execute, content, contract)
            return self._future

    def _execute(self, prompt: str | list[dict[str, Any]], contract: OutputContract | None) -> RunResult:
        self._context.task_stop = None
        self._context.run_id = self._run_id
        self._context.task_id = ""
        trace = self._telemetry.trace({"session_id": self.session_id, "run_id": self._context.run_id}) if self._telemetry else nullcontext()
        try:
            with trace:
                return self._execute_run(prompt, contract)
        finally:
            with self._mutex:
                self._steer_open = False
                if self._agent is not None:
                    self._undelivered_steers.extend(self._agent.drain_steers())
                    self.options = replace(self.options, mode=cast(SessionMode, self._agent.mode))
            self._notifications.changed()
            self._save_usage()

    def _execute_run(self, prompt: str | list[dict[str, Any]], contract: OutputContract | None) -> RunResult:
        assert self._agent is not None
        try:
            self._ensure_started()
            result = measured_send(self._agent, prompt, output_contract=contract)
            if self._telemetry and (result.interrupted or result.stopped != "done" or result.output_status not in {"valid", "not_requested"}):
                self._telemetry.fail_current()
            if result.interrupted:
                self._wait_callbacks(only_async=True)
            if self._log and self._log.broken_reason:
                raise SessionStorageError("Session log write failed")
            return result
        except ResultTransformError:
            self._agent.close_open_tool_calls("Result processing failed; remaining calls were not executed.")
            raise
        except (SessionStorageError, CloseTimeoutError, BudgetExceededError):
            raise
        except Exception as exc:
            self._agent.close_open_tool_calls("Execution failed; outcome may be incomplete.")
            raise ExecutionError(f"Execution failed ({type(exc).__name__})") from exc

    def run(self, prompt: Prompt, *, output: OutputSpec | None = None) -> RunResult:
        return self._start(prompt, output).result()

    def _next_event(self, future: Future):
        assert self._events is not None
        try:
            return self._events.get(timeout=0.05)
        except queue.Empty:
            return RunCompleted(future.result(), session_id=self.session_id, run_id=self._run_id) if future.done() else None

    def stream(self, prompt: Prompt, *, output: OutputSpec | None = None) -> Generator[UIEvent, None, None]:
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

    def steer(self, text: str) -> bool:
        """Queue text for the active parent turn; True does not mean consumed."""
        if not isinstance(text, str):
            raise ConfigurationError("steer requires text")
        with self._mutex:
            if self._closed or self._closing:
                raise SessionClosedError("Session is closed or closing")
            if not text.strip() or not self._steer_open or self._cancel.is_set():
                return False
            assert self._agent is not None
            self._agent.steer(text)
            return True

    def drain_steers(self) -> list[str]:
        """Recover unconsumed text after a turn, including after close."""
        with self._mutex:
            if self._future is not None and not self._future.done():
                raise SessionBusyError("Wait for the turn to finish before recovering input")
            items, self._undelivered_steers = self._undelivered_steers, []
            return items

    def notify(self, text: str, key: str = "", wake: bool = True) -> None:
        """Queue a system notification without starting a turn or granting permission."""
        if not isinstance(text, str) or not isinstance(key, str) or type(wake) is not bool:
            raise ConfigurationError("notify requires text, a string key and a boolean wake")
        with self._mutex:
            if self._closed or self._closing:
                raise SessionClosedError("Session is closed or closing")
            if self._control_name == "reset":
                raise SessionBusyError("Conversation reset is in progress")
            assert self._agent is not None
            self._agent.notify(text, key=key, wake=wake)

    def pending_notifications(self) -> tuple[Notification, ...]:
        """Detached non-destructive view, available while running and after close."""
        if self._agent is None:
            return ()
        return tuple(Notification(**item) for item in self._agent.pending_notifications())

    def watch_notifications(self) -> Generator[tuple[Notification, ...], None, None]:
        """Initial and coalesced pending snapshots; ends when closing begins."""
        subscription = self._notifications.subscribe()
        try:
            while subscription.queue.get():
                yield self.pending_notifications()
        finally:
            self._notifications.unsubscribe(subscription)

    def status(self) -> SessionState:
        """Lightweight lifecycle state; does not read mutable conversation history."""
        with self._mutex:
            if self._closed:
                return "closed"
            if self._closing:
                return "closing"
            if self._log is not None and self._log.broken_reason:
                return "broken"
            if self._control_error:
                return "broken"
            if self._tasks is not None and self._tasks.active():
                return "tasks"
            if self._stream_active or self._future is not None and not self._future.done():
                return "running"
            if any(not f.done() for f in self._callbacks) or any(not e.is_set() for e in self._async_settling):
                return "settling"
            return "idle"

    def snapshot(self) -> SessionSnapshot:
        """Immutable idle-only display view; never expose the live Agent or messages."""
        from xiaoyu import media
        with self._mutex:
            self._check_idle()
            assert self._agent is not None
            agent = self._agent
            by_model = tuple(ModelUsageSnapshot(name, *values) for name, values in agent.usage.snapshot().items())
            usage = UsageSnapshot(sum(m.calls for m in by_model), sum(m.prompt_tokens for m in by_model),
                                  sum(m.completion_tokens for m in by_model), by_model)
            history = tuple(MessageSnapshot(
                str(message.get("role", "")), media.text_of(message.get("content")),
                len(media.images_of(message.get("content"))),
                tuple(str(call.get("function", {}).get("name", "")) for call in (message.get("tool_calls") or ())),
                str(message.get("tool_call_id", "")),
            ) for message in agent.messages if message.get("role") != "system")
            return SessionSnapshot(self.session_id, agent.config.model, agent.mode, agent.config.budget_tokens,
                                   agent.context_tokens(), usage, history, agent.last_assistant_text(),
                                   self.pending_notifications(),
                                   tuple(PlanStep(item["step"], item["status"]) for item in agent.plan))

    def _restore_usage(self, records: list[dict[str, Any]]) -> None:
        assert self._agent is not None
        latest = next((r for r in reversed(records) if r.get("event") in {"usage", "sdk.usage"}), None)
        if latest is None:
            return
        by_model = latest.get("by_model")
        if (not isinstance(by_model, dict) or any(
                not isinstance(name, str) or not isinstance(entry, dict)
                or any(type(entry.get(key)) is not int or entry[key] < 0
                       for key in ("calls", "prompt_tokens", "completion_tokens"))
                for name, entry in by_model.items())):
            raise SessionStorageError("Stored cumulative usage is invalid")
        self._agent.usage.absorb(by_model)

    def _save_usage(self) -> None:
        if self._log is None or self._agent is None:
            return
        # Concurrent child completions must persist monotonically ordered totals.
        with self._usage_lock:
            self._journal("sdk.usage", **self._agent.usage.to_dict())

    def _apply_mode(self, mode: str) -> str:
        assert self._agent is not None
        agent = self._agent
        if mode == "plan" and self.session_path is None and self._plan_directory is None:
            from xiaoyu.agent import PLAN_MODE_ENTER_NOTE
            self._plan_directory = tempfile.TemporaryDirectory(prefix="xiaoyu-sdk-plan-")
            agent.plan_file = Path(self._plan_directory.name) / "plan.md"
            agent._plan_enter_note = PLAN_MODE_ENTER_NOTE.format(plan_file=agent.plan_file)
        result = agent.set_mode(mode)
        self.options = replace(self.options, mode=cast(SessionMode, agent.mode))
        return result

    def _begin_control(self, operation: str, value: Any = None) -> Future:
        if operation == "mode" and value not in ("default", "auto", "plan"):
            raise ConfigurationError("mode must be default, auto or plan")
        if operation == "model" and (not isinstance(value, str) or not value.strip() or value != value.strip()):
            raise ConfigurationError("model must be a nonempty name without surrounding whitespace")
        if operation == "budget" and value is not None and (type(value) is not int or value < 1):
            raise ConfigurationError("budget_tokens must be a positive integer or None")
        with self._mutex:
            self._check_idle()
            assert self._toolbox is not None
            if operation == "reset" and self._toolbox.tasks.shutdown_pending():
                raise SessionBusyError("Background processes or watchers are still active")
            self._control_name = operation
            self._events = None
            self._future = self._executor.submit(self._control, operation, value)
            return self._future

    def _control(self, operation: str, value: Any) -> Any:
        assert self._agent is not None
        agent = self._agent
        self._context.run_id = ""
        self._context.task_id = ""
        result = None
        try:
            if operation == "mode":
                result = self._apply_mode(value)
            elif operation == "model":
                agent.registry.resolve(value)  # Validate the existing route without a request.
                agent.switch_model(value)
                agent.config.summary_model = value
                agent.config.explore_model = value
                agent.compactor.context_limit = agent.config.context_limit
                agent._history_rewritten()
                self.options = replace(self.options, model=replace(self.options.model, model=value))
            elif operation == "budget":
                agent.set_budget_tokens(value)
                self.options = replace(self.options, budget_tokens=value)
            elif operation == "reset":
                with self._mutex:
                    agent.reset()
                    if self._questions is not None:
                        self._questions._reset()
                agent.structured_output = None
                agent.last_stop = ""
                self._undelivered_steers.clear()
                self.options = replace(self.options, mode=cast(SessionMode, agent.mode))
                # reset does not forgive already consumed tokens or request costs.
                self._save_usage()
            self._journal("sdk.control", operation=operation, value=value, mode=agent.mode)
            return result
        except SessionStorageError:
            self._control_error = True
            raise
        except Exception as exc:
            self._control_error = True
            raise ExecutionError(f"Control operation failed ({type(exc).__name__})") from exc
        finally:
            with self._mutex:
                self._control_name = ""
            self._notifications.changed()

    def reset(self) -> None:
        """Clear conversation state; retain identity, resources, task archives and usage."""
        self._begin_control("reset").result()

    def set_mode(self, mode: str) -> str:
        return self._begin_control("mode", mode).result()

    def switch_model(self, model: str) -> None:
        """Select a model on the existing endpoint/client; preserve transport ownership."""
        self._begin_control("model", model).result()

    def set_budget_tokens(self, budget: int | None) -> None:
        self._begin_control("budget", budget).result()

    def interrupt(self) -> None:
        self._cancel.set()
        if self._questions is not None:
            self._questions._wake()
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
            usage = self._agent.usage.to_dict()
            source_mode = self._agent.mode
        child = Session(options or self.options)
        try:
            assert child._agent is not None
            child._agent.restore(messages, source=str(self.session_path or "memory"))
            if child.options.mode == "plan":
                # The copied history can refer to the parent's plan path. End
                # with the child's current instruction after importing it.
                child._agent._record_operator(child._agent._plan_enter_note)
            elif source_mode == "plan":
                child._agent.mode = "plan"
                child._apply_mode(child.options.mode)
            child._agent.usage.absorb(usage["by_model"])
            child._save_usage()
            for archive in self.tasks.runs.archives.values():
                child.tasks.runs.persist(copy.deepcopy(archive))
            if self._ledger is not None and child._ledger is not None and child._ledger.options.history == "include":
                from dataclasses import asdict
                records = [{"event": "sdk.cost", "cost": asdict(entry)} for entry in self._ledger.snapshot().entries]
                for record in records:
                    child._journal("sdk.cost", cost=record["cost"])
                child._ledger.restore(records)
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
            if conversation and self._has_question_history:
                raise ConfigurationError("Conversation rewind with durable questions is not supported yet; use reset or fork")
            assert self._toolbox is not None and self._agent is not None
            return self._agent.rewind_result(index, conversation=conversation, files=files)

    def mcp_status(self) -> tuple[McpServerStatus, ...]:
        """Read immutable state snapshots; never include server error bodies/headers."""
        if self._mcp is None:
            return ()
        return tuple(McpServerStatus(name, state) for name, state in self._mcp.server_states().items())

    @property
    def questions(self) -> QuestionManager:
        if self._questions is None:
            raise ConfigurationError("Durable questions are not enabled")
        return self._questions

    @property
    def tasks(self) -> TaskManager:
        """Dependency tasks, stable handles and persisted child histories."""
        assert self._tasks is not None
        return self._tasks

    def _journal(self, kind: str, **fields: Any) -> None:
        if self._log is not None:
            self._log.event(kind, **fields)
            if self._log.broken_reason:
                raise SessionStorageError("Session persistence failed")

    def _lifecycle(self, event: str, *, blocking: bool = False, **fields: Any) -> None:
        decision = _Hooks(self, self.options.hooks).fire(event, fields)
        if decision.blocked:
            if blocking:
                raise ExecutionError(f"{event} hook blocked execution")
            self._hook_errors.append(event)

    def _ensure_started(self) -> None:
        if not self._started:
            self._lifecycle("SessionStart", blocking=True)
            self._started = True

    @property
    def hook_errors(self) -> tuple[str, ...]:
        return tuple(self._hook_errors)

    @property
    def cost(self) -> CostSnapshot | None:
        return self._ledger.snapshot() if self._ledger is not None else None

    @property
    def telemetry_status(self) -> dict[str, int]:
        return {"dropped": self._telemetry.dropped, "failures": self._telemetry.failures} if self._telemetry else {"dropped": 0, "failures": 0}

    def mcp_add(self, server: McpServer) -> None:
        from .mcp import McpPool
        McpPool.validate(server)
        with self._mutex:
            self._check_idle()
            self._future = self._executor.submit(self._mcp_add, server)
            future = self._future
        future.result()

    def _mcp_add(self, server: McpServer) -> None:
        from .mcp import McpPool
        if self._mcp is None:
            self._mcp = McpPool()
            self._mcp.acquire(self._mcp_owner)
            assert self._toolbox is not None
            self._toolbox._mcp = self._mcp
            self._toolbox._mcp_search = True
            self._toolbox._register_mcp_search()
        self._mcp.add(server, _owner=self._mcp_owner)

    def mcp_manage(self, name: str, action: str) -> str:
        """Idle-only start/stop/remove/reconnect/approve_changes; no implicit approval."""
        from .types import McpManagementError
        with self._mutex:
            self._check_idle()
            if action not in {"start", "stop", "remove", "reconnect", "approve_changes"}:
                raise ConfigurationError("Unknown MCP action")
            if self._mcp is None:
                raise McpManagementError("No MCP servers configured")
            def manage() -> str:
                getattr(self._mcp, action)(name, _owner=self._mcp_owner)
                return self._mcp.server_states().get(name, "removed")
            self._future = self._executor.submit(manage)
            future = self._future
        return future.result()

    def close(self) -> None:
        with self._close_mutex:
            self._close()

    def _close(self) -> None:
        with self._mutex:
            if self._closed:
                return
            self._closing = True
            self._notifications.close()
            if self._questions is not None:
                self._questions._changes.close()
            self.interrupt()
            future = self._future
        if future is not None:
            self._wait_turn(future)
        if self._tasks is not None:
            self._tasks.close()
        if self._started and not self._end_hook_sent:
            self._lifecycle("SessionEnd")
            self._end_hook_sent = True
        self._wait_callbacks()
        retrying_cleanup = self._cleanup_future is not None
        if self._cleanup_future is None:
            self._cleanup_future = self._executor.submit(self._release_resources)
        try:
            pending = self._cleanup_future.result(timeout=self.options.close_timeout)
        except CloseTimeoutError:
            self._cleanup_future = None
            if retrying_cleanup:
                self._close()
                return
            raise
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
            if self._mcp_borrowed:
                self._mcp.release(self._mcp_owner)
                self._mcp = None
            else:
                self._mcp.close(_owner=self._mcp_owner)
                pending += self._mcp.shutdown_pending()
        if pending:
            return pending
        if self._telemetry is not None:
            self._telemetry.close(self.options.close_timeout)
        if self._mcp_directory is not None:
            self._mcp_directory.cleanup()
        if self._plan_directory is not None:
            self._plan_directory.cleanup()
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

    def __init__(self, options: SessionOptions, *, resume_from: Path | None = None, resume_id: str | None = None) -> None:
        self._session = Session(options, resume_from=resume_from, resume_id=resume_id)

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        old = self._session._loop
        if old is not None and old is not loop:
            raise ConfigurationError("AsyncSession belongs to a different event loop")
        self._session._loop = loop

    @property
    def questions(self) -> AsyncQuestionManager:
        return AsyncQuestionManager(self._session.questions)

    @property
    def closed(self) -> bool:
        return self._session.closed

    @property
    def session_id(self) -> str:
        return self._session.session_id

    @property
    def session_path(self) -> Path | None:
        return self._session.session_path

    async def run(self, prompt: Prompt, *, output: OutputSpec | None = None) -> RunResult:
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

    async def stream(self, prompt: Prompt, *, output: OutputSpec | None = None) -> AsyncGenerator[UIEvent, None]:
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

    def steer(self, text: str) -> bool:
        return self._session.steer(text)

    def drain_steers(self) -> list[str]:
        return self._session.drain_steers()

    def notify(self, text: str, key: str = "", wake: bool = True) -> None:
        self._session.notify(text, key=key, wake=wake)

    def pending_notifications(self) -> tuple[Notification, ...]:
        return self._session.pending_notifications()

    async def watch_notifications(self) -> AsyncGenerator[tuple[Notification, ...], None]:
        self._bind_loop()
        subscription = self._session._notifications.subscribe(asyncio.get_running_loop())
        try:
            while await subscription.wait():
                yield self.pending_notifications()
        finally:
            self._session._notifications.unsubscribe(subscription)

    def status(self) -> SessionState:
        return self._session.status()

    async def snapshot(self) -> SessionSnapshot:
        self._bind_loop()
        return await asyncio.to_thread(self._session.snapshot)

    async def _control(self, operation: str, value: Any = None) -> Any:
        self._bind_loop()
        future = self._session._begin_control(operation, value)
        wrapped = asyncio.wrap_future(future)
        wrapped.add_done_callback(lambda f: f.exception() if not f.cancelled() else None)
        try:
            return await asyncio.shield(wrapped)
        except asyncio.CancelledError:
            # A control may already have written history; never report it rolled back.
            await asyncio.to_thread(self._session._wait_turn, future)
            raise

    async def reset(self) -> None:
        await self._control("reset")

    async def set_mode(self, mode: str) -> str:
        return await self._control("mode", mode)

    async def switch_model(self, model: str) -> None:
        await self._control("model", model)

    async def set_budget_tokens(self, budget: int | None) -> None:
        await self._control("budget", budget)

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

    async def mcp_add(self, server: McpServer) -> None:
        await asyncio.to_thread(self._session.mcp_add, server)

    async def mcp_manage(self, name: str, action: str) -> str:
        return await asyncio.to_thread(self._session.mcp_manage, name, action)

    async def submit_tasks(self, specs: tuple[TaskSpec, ...]) -> tuple[TaskHandle, ...]:
        self._bind_loop()
        return await asyncio.to_thread(self._session.tasks.submit, specs)

    async def wait_task(self, task_id: str, timeout: float | None = None) -> TaskSnapshot:
        return await asyncio.to_thread(self._session.tasks.wait, task_id, timeout)

    async def cancel_task(self, task_id: str) -> TaskSnapshot:
        return await asyncio.to_thread(self._session.tasks.cancel, task_id)

    def task_status(self) -> tuple[TaskSnapshot, ...]:
        return self._session.tasks.list()

    async def retry_task(self, task_id: str, *, allow_uncertain: bool = False, continue_history: bool = False) -> TaskHandle:
        self._bind_loop()
        return await asyncio.to_thread(self._session.tasks.retry, task_id, allow_uncertain=allow_uncertain, continue_history=continue_history)

    @property
    def cost(self) -> CostSnapshot | None:
        return self._session.cost

    @property
    def telemetry_status(self) -> dict[str, int]:
        return self._session.telemetry_status

    async def __aenter__(self) -> AsyncSession:
        self._bind_loop()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


def run(prompt: Prompt, options: SessionOptions, *, output: OutputSpec | None = None) -> RunResult:
    with Session(options) as session:
        return session.run(prompt, output=output)


async def run_async(prompt: Prompt, options: SessionOptions, *, output: OutputSpec | None = None) -> RunResult:
    async with AsyncSession(options) as session:
        return await session.run(prompt, output=output)


def list_sessions(directory: Path, *, limit: int = 20, workspace: Path | None = None) -> list[SessionInfo]:
    """List only the explicitly selected host session directory."""
    return _list_sessions(limit, str(workspace.resolve()) if workspace else None, directory=Path(directory))
