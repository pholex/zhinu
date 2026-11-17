"""Private, live-only controls for exact batch execution generations."""
from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .events import QixiangExecutionUpdated, SteerAccepted


class WeaveControlError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class _Execution:
    batch_id: str
    item_id: str
    execution_id: str
    phase: str
    label: str
    stop: threading.Event
    item_stop: threading.Event
    upstream_stop: Callable[[], bool]
    agent: Any = None
    ready: bool = False
    closed: bool = False
    revision: int = 0
    model: str = ""
    workspace: str = ""
    records: list[dict[str, Any]] = field(default_factory=list)


class _InputSink:
    def __init__(self, target: Any, controls: Any, execution: _Execution):
        self.target, self.controls, self.execution = target, controls, execution

    def emit(self, event: Any) -> None:
        self.target.emit(event)
        if isinstance(event, SteerAccepted):
            self.controls._accepted(self.execution, event.text)


class WeaveControls:
    """No durable authority: the host persists receipts before calling control()."""

    def __init__(self, sink: Any):
        self.sink = sink
        self.item_approver: Any = None
        self._lock = threading.RLock()
        self._publish_lock = threading.RLock()
        self._executions: dict[str, _Execution] = {}
        self._current: dict[tuple[str, str], str] = {}
        self._records: dict[str, dict[str, Any]] = {}

    def begin(self, batch_id: str, item_id: str, phase: str, label: str,
              stop: threading.Event, upstream_stop: Callable[[], bool]) -> _Execution:
        execution = _Execution(batch_id, item_id, uuid.uuid4().hex, phase, label[:400], threading.Event(), stop, upstream_stop)
        with self._lock:
            self._executions[execution.execution_id] = execution
            self._current[batch_id, item_id] = execution.execution_id
        return execution

    def register(self, execution: _Execution, agent: Any) -> None:
        with self._lock:
            execution.agent = agent
            execution.model = str(agent.config.model or "")
            execution.workspace = str(agent.config.workspace)
            agent.sink = _InputSink(agent.sink, self, execution)
            previous = agent._on_turn_input
            def ready() -> bool:
                consumed = bool(previous()) if previous is not None else False
                self._ready(execution)
                return consumed
            agent._on_turn_input = ready
        self._publish(execution)

    def stopped(self, execution: _Execution) -> bool:
        return execution.stop.is_set() or execution.item_stop.is_set() or execution.upstream_stop()

    def _ready(self, execution: _Execution) -> None:
        with self._lock:
            execution.ready = True
            if not execution.closed and not self.stopped(execution):
                for record in execution.records:
                    if record["status"] == "queued":
                        execution.agent.steer(record["text"])
        self._publish(execution)

    def finish(self, execution: _Execution) -> None:
        with self._lock:
            if execution.closed:
                return
            execution.closed = True
            if execution.agent is not None:
                execution.model = str(execution.agent.config.model or "")
            execution.agent = None
            for record in execution.records:
                if record["status"] in {"queued", "requested"}:
                    record["status"] = "not_consumed" if record["kind"] == "message" else "settled"
                    record["reason"] = "执行已结束，消息未进入上下文" if record["kind"] == "message" else "执行实例已结束"
                    record["updated_at"] = _now()
        self._publish(execution)

    def _accepted(self, execution: _Execution, text: str) -> None:
        with self._lock:
            record = next((r for r in execution.records if r["kind"] == "message"
                           and r["status"] == "queued" and r["text"] == text), None)
            if record is None:
                return
            record.update(status="accepted", updated_at=_now(), reason="已进入当前执行实例的上下文")
        self._publish(execution)

    def _snapshot(self, execution: _Execution) -> dict[str, Any]:
        pending = [r for r in execution.records if r["status"] in {"queued", "requested"}]
        settled = [r for r in execution.records if r["status"] not in {"queued", "requested"}]
        visible = pending + (settled[-(128-len(pending)):] if len(pending) < 128 else [])
        return dict(batch_id=execution.batch_id, item_id=execution.item_id,
                    execution_id=execution.execution_id, execution_phase=execution.phase,
                    revision=execution.revision,
                    execution_state="settled" if execution.closed else "running" if execution.ready else "starting",
                    stop_requested=execution.stop.is_set(), model=execution.model, workspace=execution.workspace,
                    live=not execution.closed and execution.agent is not None
                         and self._current.get((execution.batch_id, execution.item_id)) == execution.execution_id,
                    controls=copy.deepcopy(visible), controls_omitted=len(execution.records)-len(visible))

    def snapshot(self, batch_id: str, item_id: str, execution_id: str | None = None) -> dict[str, Any] | None:
        with self._lock:
            key = execution_id or self._current.get((batch_id, item_id), "")
            execution = self._executions.get(key)
            if execution is None or (execution.batch_id, execution.item_id) != (batch_id, item_id):
                return None
            return self._snapshot(execution)

    def _publish(self, execution: _Execution) -> None:
        # No runtime lock is held while a host sink performs persistence or callbacks.
        with self._publish_lock:
            with self._lock:
                execution.revision += 1
                value = self._snapshot(execution)
            try:
                self.sink.emit(QixiangExecutionUpdated(**value))
            except Exception:
                pass  # Observation cannot change a completed tool or input operation.

    def control(self, kind: str, *, batch_id: str, item_id: str, execution_id: str,
                operation_id: str, text: str = "") -> dict[str, Any]:
        if (kind not in {"message", "stop"} or not isinstance(operation_id, str)
                or not operation_id or len(operation_id) > 200):
            raise WeaveControlError("无效的批量任务操作")
        if (not isinstance(text, str) or (kind == "message" and (not text.strip() or len(text) > 8000))
                or (kind == "stop" and text)):
            raise WeaveControlError("补充消息须为 1–8000 字符，停止操作不携带消息")
        text = text.strip()
        with self._lock:
            old = self._records.get(operation_id)
            if old is not None:
                if tuple(old[k] for k in ("kind", "batch_id", "item_id", "execution_id", "text")) != (kind, batch_id, item_id, execution_id, text):
                    raise WeaveControlError("操作标识已用于另一条操作")
                return copy.deepcopy(old)
            execution = self._executions.get(execution_id)
            if (execution is None or (execution.batch_id, execution.item_id) != (batch_id, item_id)
                    or not self._snapshot(execution)["live"]):
                raise WeaveControlError("执行实例已结束或不属于当前任务项")
            if kind == "message" and self.stopped(execution):
                raise WeaveControlError("执行正在停止，不能再接收补充消息")
            if kind == "stop" and any(r["kind"] == "stop" for r in execution.records):
                raise WeaveControlError("该执行实例已请求停止，请查看已有回执")
            if kind == "message" and (len(self._records) >= 1000 or sum(r["status"] in {"queued", "requested"} for r in self._records.values()) >= 128):
                raise WeaveControlError("操作已达上限，请等待当前操作结算或开启新会话")
            if kind == "message" and sum(r["status"] == "queued" for r in execution.records) >= 64:
                raise WeaveControlError("本执行实例的待处理消息过多")
            now = _now()
            record = dict(operation_id=operation_id, kind=kind, batch_id=batch_id, item_id=item_id,
                          execution_id=execution_id, text=text, status="queued" if kind == "message" else "requested",
                          created_at=now, updated_at=now, reason="")
            self._records[operation_id] = record
            execution.records.append(record)
            if kind == "message" and execution.ready:
                execution.agent.steer(text)
            elif kind == "stop":
                execution.stop.set()
                execution.item_stop.set()
            result = copy.deepcopy(record)
        self._publish(execution)
        return result

    def approve(self, execution: _Execution, name: str, args: dict[str, Any], fallback: Any) -> Any:
        identity = dict(kind="qixiang", batch_id=execution.batch_id, item_id=execution.item_id,
                        execution_id=execution.execution_id, label=execution.label, workspace=execution.workspace)
        if self.item_approver is not None:
            return self.item_approver(name, args, identity, lambda: self.stopped(execution))
        return fallback(name, args)
