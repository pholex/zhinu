"""Host-driven observations; no polling, threads or model calls are created here."""
from __future__ import annotations

from dataclasses import dataclass
import math
import time
import uuid
from typing import Callable


@dataclass(frozen=True)
class Observation:
    subject: str
    fingerprint: str
    evidence: str
    next_check_at: float | None = None


class ObservationMonitor:
    """Call observe/flush serially from the host; the first sample establishes a baseline.

    Only fingerprint changes wake the model. A burst holds its first deadline and
    sends its latest evidence; returning to the baseline cancels the pending wake.
    next_check_at is host scheduling metadata, never an implicit timer.
    """

    def __init__(self, notify: Callable[..., None], *, coalesce_seconds: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not callable(notify) or type(coalesce_seconds) not in (int, float) or not math.isfinite(coalesce_seconds) or coalesce_seconds < 0:
            raise ValueError("A notifier and finite nonnegative coalescing window are required")
        self._notify, self._clock, self._window = notify, clock, coalesce_seconds
        self._latest: dict[str, Observation] = {}
        self._reported: dict[str, str] = {}
        self._pending: dict[str, tuple[float, str]] = {}

    def observe(self, observation: Observation) -> bool:
        if (not isinstance(observation, Observation)
                or not all(isinstance(v, str) and v.strip() for v in
                           (observation.subject, observation.fingerprint, observation.evidence))
                or observation.next_check_at is not None and
                   (type(observation.next_check_at) not in (int, float) or not math.isfinite(observation.next_check_at))):
            raise ValueError("Observation requires a subject, fingerprint and evidence")
        key = observation.subject
        self._latest[key] = observation
        if key not in self._reported:
            self._reported[key] = observation.fingerprint
            return False
        if observation.fingerprint == self._reported[key]:
            self._pending.pop(key, None)
            return False
        if key not in self._pending:
            self._pending[key] = (self._clock() + self._window, uuid.uuid4().hex)
        return True

    def observations(self) -> tuple[Observation, ...]:
        return tuple(self._latest.values())

    def forget(self, subject: str) -> None:
        self._latest.pop(subject, None)
        self._reported.pop(subject, None)
        self._pending.pop(subject, None)

    def flush(self) -> int:
        sent = 0
        now = self._clock()
        for subject, (due, ident) in tuple(self._pending.items()):
            if now < due:
                continue
            value = self._latest[subject]
            self._notify(f"[状态变化：{subject}]\n{value.evidence}", key=f"observation:{ident}", wake=True)
            self._reported[subject] = value.fingerprint
            del self._pending[subject]
            sent += 1
        return sent
