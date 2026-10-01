"""Bounded, content-free completed traces; exporter calls never run on agents."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import queue
import threading
import time
from typing import Any, Callable, Iterator
import uuid

from .types import CloseTimeoutError, ConfigurationError


@dataclass(frozen=True)
class SpanRecord:
    span_id: str
    parent_id: str
    name: str
    start_ns: int
    end_ns: int
    failed: bool
    attributes: dict[str, str]


@dataclass(frozen=True)
class TraceRecord:
    spans: tuple[SpanRecord, ...]


@dataclass(frozen=True)
class TelemetryOptions:
    exporter: Callable[[TraceRecord], None] = field(repr=False)
    queue_size: int = 32
    max_spans: int = 512


class OpenTelemetryExporter:
    """Use the host's provider; never set globals, flush or close its provider.

    Install opentelemetry-api in the host environment. Completed traces are
    reconstructed with original timestamps, so no live OTel spans survive a
    dropped trace or an interrupted agent.
    """
    def __init__(self, provider: Any) -> None:
        import importlib
        self._trace = importlib.import_module("opentelemetry.trace")
        self._tracer = provider.get_tracer("xiaoyu-agent-sdk")

    def __call__(self, record: TraceRecord) -> None:
        contexts: dict[str, Any] = {}
        # Parents were entered first, but append on completion may be reversed.
        pending = list(record.spans)
        while pending:
            ready = [r for r in pending if not r.parent_id or r.parent_id in contexts]
            if not ready:
                raise ValueError("Invalid telemetry parent graph")
            for item in ready:
                span = self._tracer.start_span(item.name, context=contexts.get(item.parent_id),
                    attributes=item.attributes, start_time=item.start_ns)
                try:
                    contexts[item.span_id] = self._trace.set_span_in_context(span)
                    if item.failed:
                        span.set_status(self._trace.Status(self._trace.StatusCode.ERROR))
                finally:
                    span.end(end_time=item.end_ns)
                pending.remove(item)


class _Telemetry:
    def __init__(self, options: TelemetryOptions) -> None:
        if any(type(v) is not int or v < 1 for v in (options.queue_size, options.max_spans)):
            raise ConfigurationError("Telemetry queue/span limits must be positive integers")
        self.options = options
        self.local = threading.local()
        self.queue: queue.Queue[TraceRecord] = queue.Queue(options.queue_size)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.dropped = 0
        self.failures = 0
        self._worker = threading.Thread(target=self._export, name="xiaoyu-telemetry", daemon=True)
        self._worker.start()

    def _drop(self) -> None:
        with self._lock:
            self.dropped += 1

    def _export(self) -> None:
        while not self._stop.is_set() or not self.queue.empty():
            try:
                record = self.queue.get(timeout=.05)
            except queue.Empty:
                continue
            try:
                self.options.exporter(record)
            except Exception:
                with self._lock:
                    self.failures += 1
            finally:
                self.queue.task_done()

    @contextmanager
    def trace(self, attributes: dict[str, str]) -> Iterator[None]:
        self.local.records = []
        self.local.stack = []
        self.local.events = {}
        self.local.attributes = dict(attributes)
        try:
            with self.span("agent.run"):
                try:
                    yield
                finally:
                    for token in reversed(list(self.local.stack[1:])):
                        self.end(token, failed=True)
        finally:
            record = TraceRecord(tuple(self.local.records))
            try:
                self.queue.put_nowait(record)
            except queue.Full:
                self._drop()
            self.local.records = []
            self.local.events = {}

    def begin(self, operation: str, **attributes: str) -> dict[str, Any] | None:
        stack = getattr(self.local, "stack", None)
        if stack is None or len(self.local.records) + len(stack) >= self.options.max_spans:
            self._drop()
            return None
        token = {"id": uuid.uuid4().hex, "parent": stack[-1]["id"] if stack else "", "name": operation,
                 "start": time.time_ns(), "attributes": {**self.local.attributes, **attributes}}
        stack.append(token)
        return token

    def end(self, token: dict[str, Any] | None, *, failed: bool = False) -> None:
        if token is None:
            return
        stack = self.local.stack
        if token not in stack:
            return
        stack.remove(token)
        self.local.records.append(SpanRecord(token["id"], token["parent"], token["name"],
            token["start"], time.time_ns(), failed or token.get("failed", False), token["attributes"]))

    def fail_current(self) -> None:
        stack = getattr(self.local, "stack", [])
        if stack:
            stack[0]["failed"] = True

    @contextmanager
    def span(self, name: str, **attributes: str) -> Iterator[None]:
        token = self.begin(name, **attributes)
        failed = False
        try:
            yield
        except BaseException:
            failed = True
            raise
        finally:
            self.end(token, failed=failed)

    def observe(self, kind: str, *, name: str = "", failed: bool = False, tool_call_id: str = "") -> None:
        events = getattr(self.local, "events", None)
        if events is None:
            return
        starts = {"request.started": "model.request", "tool.running": "tool.call", "SubagentStart": "agent.child",
                  "BeforeCompact": "context.compact"}
        ends = {"request.ended": "request.started", "tool.completed": "tool.running", "SubagentEnd": "SubagentStart",
                "AfterCompact": "BeforeCompact"}
        if kind in starts:
            token = self.begin(starts[kind], name=name, tool_call_id=tool_call_id)
            events.setdefault(kind, []).append(token)
        elif kind in ends:
            tokens = events.get(ends[kind], [])
            if tokens:
                self.end(tokens.pop(), failed=failed)

    def close(self, timeout: float) -> None:
        self._stop.set()
        self._worker.join(timeout)
        if self._worker.is_alive():
            raise CloseTimeoutError("Telemetry exporter still running; retry close")
