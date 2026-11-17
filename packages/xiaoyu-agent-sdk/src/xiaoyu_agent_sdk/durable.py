"""Serial host-driven work: persisted waits, bounded retries and explicit acceptance."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import json
import math
from pathlib import Path
import threading
import time
from typing import Callable, Mapping
import uuid

from xiaoyu._state_db import StateDB, StateError
from xiaoyu.session_log import SessionWriteLock
from .tasks import TaskAcceptance


@dataclass(frozen=True)
class WorkStep:
    name: str
    handler: str
    payload: str = ""
    probe: str = ""
    evaluator: str = ""
    retry_safe: bool = False
    max_retries: int = 3
    max_repairs: int = 2
    interval: float = 60.0
    max_unchanged: int = 0


@dataclass(frozen=True)
class WorkResult:
    status: str  # succeeded / waiting / retry / actionable / failed / blocked
    evidence: str
    checkpoint: str = ""
    fingerprint: str = ""
    next_check_at: float | None = None


@dataclass(frozen=True)
class WorkContext:
    run_id: str
    step: WorkStep
    attempt: int
    checkpoint: str
    evidence: str
    idempotency_key: str


@dataclass(frozen=True)
class WorkStepSnapshot:
    spec: WorkStep
    state: str = "ready"
    attempt: int = 0
    retries: int = 0
    repairs: int = 0
    recoveries: int = 0
    checkpoint: str = ""
    evidence: str = ""
    fingerprint: str = ""
    unchanged: int = 0
    next_check_at: float = 0.0
    acceptance: str = "not_requested"


@dataclass(frozen=True)
class WorkRun:
    id: str
    state: str
    steps: tuple[WorkStepSnapshot, ...]
    deadline: float
    max_calls: int
    calls: int = 0
    reason: str = ""


_PHASES = {"ready", "running", "waiting", "probing", "verifying", "evaluating", "succeeded"}
_RUN_STATES = {"active", "paused", "uncertain", "completed", "failed", "cancelled"}


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)  # type: ignore[arg-type]


def _validate_step(step: WorkStep) -> None:
    if not isinstance(step, WorkStep) or any(not isinstance(getattr(step, k), str) for k in ("name", "handler", "payload", "probe", "evaluator")):
        raise ValueError("Invalid work step")
    if not step.name.strip() or not step.handler.strip() or len(step.payload) > 64000 or max(len(step.name), len(step.handler), len(step.probe), len(step.evaluator)) > 256:
        raise ValueError("Work steps require bounded names and payloads")
    if type(step.retry_safe) is not bool or any(type(v) is not int or not 0 <= v <= 1000 for v in (step.max_retries, step.max_repairs, step.max_unchanged)):
        raise ValueError("Invalid step budgets")
    if not _finite(step.interval) or step.interval <= 0:
        raise ValueError("Probe/retry interval must be finite and positive")


class DurableTaskRunner:
    """One exclusive local writer; tick performs at most one callback, with no timer.

    Handler/probe/evaluator names are rebound by the host at each process start.
    Probes and evaluators must be read-only. Callbacks must bound their own I/O;
    deadlines stop admission, not an already-running synchronous callback.
    """

    def __init__(self, path: Path, *, handlers: Mapping[str, Callable[[WorkContext], WorkResult]],
                 probes: Mapping[str, Callable[[WorkContext], WorkResult]] | None = None,
                 evaluators: Mapping[str, Callable[[WorkContext], TaskAcceptance]] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._mutex = threading.Lock()
        self._closed = False
        self._broken = False
        self._clock = clock
        self.handlers, self.probes, self.evaluators = dict(handlers), dict(probes or {}), dict(evaluators or {})
        self._lock = SessionWriteLock(Path(str(Path(path).absolute()) + ".owner"))
        try:
            self._db = StateDB(path, "durable-work", (
                "CREATE TABLE work_runs (id TEXT PRIMARY KEY, body TEXT NOT NULL)",
                "CREATE TABLE work_events (seq INTEGER PRIMARY KEY, id TEXT, body TEXT NOT NULL)",
            ))
            for run in self.list():
                recovered = []
                uncertain = False
                for step in run.steps:
                    if step.state in {"running", "probing", "evaluating"}:
                        safe = step.state != "running" or step.spec.retry_safe
                        allowed = safe and step.recoveries < step.spec.max_retries
                        phase = {"running": "ready", "probing": "waiting", "evaluating": "verifying"}[step.state]
                        step = replace(step, state=phase, recoveries=step.recoveries + 1,
                                       acceptance="unknown" if phase == "verifying" else step.acceptance)
                        uncertain |= not allowed
                    recovered.append(step)
                if tuple(recovered) != run.steps:
                    self._save(replace(run, steps=tuple(recovered), state="uncertain" if uncertain else run.state,
                                       reason="Interrupted callback recovered; inspect uncertain effects" if uncertain else "Recovered checkpoint"))
        except BaseException:
            self._lock.close()
            raise

    def __enter__(self) -> DurableTaskRunner:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def _enter(self) -> None:
        if not self._mutex.acquire(blocking=False):
            raise RuntimeError("Work runner is busy; calls must be serial")
        if self._closed or self._broken:
            self._mutex.release()
            raise StateError("Work runner is closed or persistence failed")

    def close(self) -> None:
        if self._closed:
            return
        if not self._mutex.acquire(blocking=False):
            raise RuntimeError("Work runner is busy; calls must be serial")
        try:
            self._lock.close()
            self._closed = True
        finally:
            self._mutex.release()

    def _decode(self, body: str) -> WorkRun:
        try:
            raw = json.loads(body)
            raw["steps"] = tuple(WorkStepSnapshot(**{**s, "spec": WorkStep(**s["spec"])}) for s in raw["steps"])
            run = WorkRun(**raw)
            if run.state not in _RUN_STATES or not run.steps or not _finite(run.deadline) or type(run.max_calls) is not int or run.max_calls < 1 or type(run.calls) is not int or run.calls < 0:
                raise ValueError("Invalid run state")
            if len({s.spec.name for s in run.steps}) != len(run.steps):
                raise ValueError("Duplicate step names")
            for s in run.steps:
                _validate_step(s.spec)
                if s.state not in _PHASES or not _finite(s.next_check_at) or any(type(n) is not int or n < 0 for n in (s.attempt, s.retries, s.repairs, s.recoveries, s.unchanged)):
                    raise ValueError("Invalid step state")
                if not all(isinstance(v, str) for v in (s.checkpoint, s.evidence, s.fingerprint)):
                    raise ValueError("Invalid step data")
            return run
        except (TypeError, ValueError, KeyError) as exc:
            raise StateError("Invalid persisted work record") from exc

    def get(self, run_id: str) -> WorkRun:
        with self._db.connect() as db:
            row = db.execute("SELECT body FROM work_runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError("Unknown work run")
        return self._decode(row[0])

    def list(self) -> tuple[WorkRun, ...]:
        with self._db.connect() as db:
            return tuple(self._decode(body) for (body,) in db.execute("SELECT body FROM work_runs ORDER BY rowid"))

    def history(self, run_id: str) -> tuple[dict, ...]:
        """Detached transition summaries; snapshots/checkpoints live in get()."""
        self.get(run_id)
        with self._db.connect() as db:
            return tuple(json.loads(body) for (body,) in db.execute("SELECT body FROM work_events WHERE id=? ORDER BY seq", (run_id,)))

    def _save(self, run: WorkRun) -> WorkRun:
        body = json.dumps(asdict(run), ensure_ascii=False, allow_nan=False, sort_keys=True)
        event = json.dumps({"state": run.state, "calls": run.calls, "reason": run.reason,
                            "at": self._clock(), "steps": [{"name": s.spec.name, "state": s.state,
                                "attempt": s.attempt, "retries": s.retries, "repairs": s.repairs,
                                "acceptance": s.acceptance} for s in run.steps]}, allow_nan=False)
        try:
            with self._db.connect(write=True) as db:
                db.execute("INSERT OR REPLACE INTO work_runs VALUES (?,?)", (run.id, body))
                db.execute("INSERT INTO work_events(id, body) VALUES (?,?)", (run.id, event))
        except BaseException:
            self._broken = True
            raise
        return run

    def create(self, steps: tuple[WorkStep, ...], *, timeout: float = 86400, max_calls: int = 100) -> WorkRun:
        if not steps or len(steps) > 100 or len({s.name for s in steps}) != len(steps):
            raise ValueError("Require 1–100 uniquely named steps")
        for s in steps:
            _validate_step(s)
        if not _finite(timeout) or timeout <= 0 or type(max_calls) is not int or not 1 <= max_calls <= 100000:
            raise ValueError("Finite positive work budgets required")
        self._enter()
        try:
            return self._save(WorkRun(uuid.uuid4().hex, "active", tuple(WorkStepSnapshot(s) for s in steps), self._clock() + timeout, max_calls))
        finally:
            self._mutex.release()

    def cancel(self, run_id: str) -> WorkRun:
        self._enter()
        try:
            run = self.get(run_id)
            return run if run.state in {"completed", "cancelled", "failed"} else self._save(replace(run, state="cancelled", reason="Cancelled by host"))
        finally:
            self._mutex.release()

    def resume(self, run_id: str, *, allow_uncertain: bool = False) -> WorkRun:
        self._enter()
        try:
            run = self.get(run_id)
            if run.state not in {"paused", "uncertain"} or run.state == "uncertain" and allow_uncertain is not True:
                raise ValueError("Resume requires a paused run or explicit uncertain-effects approval")
            # Deliberately retains budgets and successful steps.
            return self._save(replace(run, state="active", reason="Resumed by host"))
        finally:
            self._mutex.release()

    def _updated(self, run: WorkRun, index: int, step: WorkStepSnapshot, **changes) -> WorkRun:
        steps = list(run.steps)
        steps[index] = step
        if all(s.state == "succeeded" for s in steps):
            changes["state"] = "completed"
        return self._save(replace(run, steps=tuple(steps), **changes))

    def tick(self, run_id: str) -> WorkRun:
        self._enter()
        try:
            run = self.get(run_id)
            if run.state != "active":
                return run
            now = self._clock()
            if now >= run.deadline or run.calls >= run.max_calls:
                return self._save(replace(run, state="failed", reason="Work deadline or callback budget exhausted"))
            index = next(i for i, s in enumerate(run.steps) if s.state != "succeeded")
            step = run.steps[index]
            if now < step.next_check_at:
                return run
            phase = step.state
            if phase not in {"ready", "waiting", "verifying"}:
                return self._save(replace(run, state="uncertain", reason="Unsettled callback; reopen and inspect"))
            name = {"ready": step.spec.handler, "waiting": step.spec.probe, "verifying": step.spec.evaluator}[phase]
            callback: Callable[[WorkContext], WorkResult | TaskAcceptance] | None
            if phase == "ready":
                callback = self.handlers.get(name)
            elif phase == "waiting":
                callback = self.probes.get(name)
            else:
                callback = self.evaluators.get(name)
            if not callable(callback):
                return self._save(replace(run, state="paused", reason=f"Missing host callback: {name}"))
            step = replace(step, state={"ready": "running", "waiting": "probing", "verifying": "evaluating"}[phase],
                           acceptance="pending" if phase == "verifying" else step.acceptance,
                           attempt=step.attempt + (phase == "ready"))
            run = self._updated(run, index, step, calls=run.calls + 1)
            context = WorkContext(run.id, step.spec, step.attempt, step.checkpoint, step.evidence, f"{run.id}:{step.spec.name}:{step.repairs}")
            try:
                result = callback(context)
                if phase == "verifying":
                    if not isinstance(result, TaskAcceptance) or result.status not in {"passed", "failed", "unknown"} or not isinstance(result.evidence, str) or not result.evidence.strip():
                        raise ValueError("Invalid acceptance evidence")
                else:
                    if not isinstance(result, WorkResult) or result.status not in {"succeeded", "waiting", "retry", "actionable", "failed", "blocked"}:
                        raise ValueError("Invalid step result")
                    if not all(isinstance(v, str) for v in (result.evidence, result.checkpoint, result.fingerprint)) or not result.evidence.strip() or max(len(result.evidence), len(result.checkpoint), len(result.fingerprint)) > 64000:
                        raise ValueError("Invalid result evidence/checkpoint")
                    if result.next_check_at is not None and (not _finite(result.next_check_at) or result.next_check_at <= now):
                        raise ValueError("Next check must be in the future")
            except Exception:
                # An exception says nothing about whether the handler's effect happened.
                if phase == "ready":
                    return self._updated(run, index, replace(step, state="ready"), state="uncertain", reason="Handler failed without a trustworthy outcome")
                result = TaskAcceptance("unknown", "Evaluator failed") if phase == "verifying" else WorkResult("retry", "Probe unavailable")
            if isinstance(result, TaskAcceptance):
                step = replace(step, state="verifying", acceptance=result.status, evidence=result.evidence)
                if result.status == "passed":
                    return self._updated(run, index, replace(step, state="succeeded"), reason="Acceptance passed")
                if result.status == "unknown":
                    return self._updated(run, index, step, state="paused", reason="Acceptance needs host evidence")
                return self._repair(run, index, step, now)
            step = replace(step, state=phase, evidence=result.evidence, checkpoint=result.checkpoint or step.checkpoint)
            if result.status == "succeeded":
                return self._updated(run, index, replace(step, state="verifying" if step.spec.evaluator else "succeeded", next_check_at=0), reason="Execution completed")
            if result.status == "failed":
                return self._updated(run, index, step, state="failed", reason="Step reported failure")
            if result.status == "blocked":
                return self._updated(run, index, step, state="paused", reason="Waiting for host intervention")
            if result.status == "actionable":
                return self._repair(run, index, step, now)
            if result.status == "retry":
                if phase == "ready" and not step.spec.retry_safe:
                    return self._updated(run, index, step, state="uncertain", reason="Retry safety has not been declared")
                step = replace(step, retries=step.retries + 1)
                if step.retries > step.spec.max_retries:
                    return self._updated(run, index, step, state="failed", reason="Transient retry budget exhausted")
                due = result.next_check_at or now + step.spec.interval * min(2 ** (step.retries - 1), 64)
                return self._updated(run, index, replace(step, next_check_at=due), reason="Backing off")
            if not step.spec.probe:
                return self._updated(run, index, step, state="paused", reason="Waiting result requires a read-only probe")
            unchanged = step.unchanged + 1 if result.fingerprint and result.fingerprint == step.fingerprint else 0
            step = replace(step, state="waiting", fingerprint=result.fingerprint, unchanged=unchanged,
                           next_check_at=result.next_check_at or now + step.spec.interval)
            stopped = step.spec.max_unchanged > 0 and unchanged >= step.spec.max_unchanged
            return self._updated(run, index, step, state="paused" if stopped else "active", reason="No progress; host review required" if stopped else "Waiting for external state")
        finally:
            self._mutex.release()

    def _repair(self, run: WorkRun, index: int, step: WorkStepSnapshot, now: float) -> WorkRun:
        if not step.spec.retry_safe:
            return self._updated(run, index, step, state="paused", reason="Repair requires a repeatable handler")
        step = replace(step, repairs=step.repairs + 1)
        if step.repairs > step.spec.max_repairs:
            return self._updated(run, index, step, state="failed", reason="Repair budget exhausted")
        return self._updated(run, index, replace(step, state="ready", next_check_at=now + step.spec.interval), reason="Repair scheduled from evidence")
