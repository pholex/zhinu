"""Request-boundary accounting shared by parent, summaries and child agents."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
import math
import threading
from types import SimpleNamespace
from typing import Any, Callable
import uuid

from .types import ConfigurationError, SDKError


class BudgetExceededError(SDKError):
    """No request was started: the configured accounting boundary rejected it."""


@dataclass(frozen=True)
class ModelPrice:
    input_per_million: float
    output_per_million: float
    cached_input_per_million: float
    source: str
    cache_creation_per_million: float | None = None


@dataclass(frozen=True)
class BudgetOptions:
    prices: dict[str, ModelPrice] = field(default_factory=dict)
    max_usd: float | None = None
    max_requests: int | None = None
    history: str = "include"  # include persisted accounting, or explicitly reset


@dataclass(frozen=True)
class RequestCost:
    request_id: str
    model: str
    run_id: str
    task_id: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_tokens: int | None = None
    usd: str | None = None
    price_source: str = ""
    state: str = "running"
    cache_creation_tokens: int | None = None


@dataclass(frozen=True)
class CostSnapshot:
    requests: int
    known_usd: str
    unknown_requests: int
    inflight: int
    entries: tuple[RequestCost, ...]


class _Ledger:
    def __init__(self, options: BudgetOptions, journal: Callable[..., None], context: threading.local) -> None:
        if options.history not in {"include", "reset"}:
            raise ConfigurationError("Budget history must be include or reset")
        if options.max_usd is not None and (not math.isfinite(options.max_usd) or options.max_usd <= 0):
            raise ConfigurationError("Dollar limit must be positive and finite")
        if options.max_requests is not None and (type(options.max_requests) is not int or options.max_requests < 1):
            raise ConfigurationError("Request limit must be a positive integer")
        for price in options.prices.values():
            rates: tuple[float, ...] = (price.input_per_million, price.output_per_million, price.cached_input_per_million)
            if price.cache_creation_per_million is not None:
                rates += (price.cache_creation_per_million,)
            if not price.source or any(not math.isfinite(v) or v < 0 for v in rates):
                raise ConfigurationError("Prices need nonnegative finite values and an explicit source/version")
        self.options, self.journal, self.context = options, journal, context
        self._lock = threading.RLock()
        self._entries: dict[str, RequestCost] = {}

    def restore(self, records: list[dict[str, Any]]) -> None:
        if self.options.history == "reset":
            return
        for record in records:
            if record.get("event") == "sdk.cost":
                entry = RequestCost(**record["cost"])
                if entry.state not in {"running", "measured", "unknown"} or not entry.request_id:
                    raise ConfigurationError("Invalid stored request accounting")
                if entry.usd is not None and (not Decimal(entry.usd).is_finite() or Decimal(entry.usd) < 0):
                    raise ConfigurationError("Invalid stored request cost")
                self._entries[entry.request_id] = entry
        from dataclasses import replace
        self._entries = {key: replace(value, state="unknown") if value.state == "running" else value
                         for key, value in self._entries.items()}

    def snapshot(self) -> CostSnapshot:
        with self._lock:
            entries = tuple(self._entries.values())
            return CostSnapshot(len(entries), str(sum((Decimal(e.usd) for e in entries if e.usd is not None), Decimal(0))),
                sum(e.usd is None and e.state != "running" for e in entries), sum(e.state == "running" for e in entries), entries)

    def start(self, model: str) -> RequestCost:
        with self._lock:
            snapshot = self.snapshot()
            if self.options.max_requests is not None and snapshot.requests >= self.options.max_requests:
                raise BudgetExceededError("Request budget exhausted")
            if self.options.max_usd is not None:
                if model not in self.options.prices or snapshot.unknown_requests:
                    raise BudgetExceededError("Dollar budget cannot authorize unknown pricing or usage")
                if Decimal(snapshot.known_usd) >= Decimal(str(self.options.max_usd)):
                    raise BudgetExceededError("Dollar budget exhausted")
            request_id = self.context.request_id if getattr(self.context, "request_pending", False) else uuid.uuid4().hex
            self.context.request_pending = False
            entry = RequestCost(request_id, model, getattr(self.context, "run_id", ""), getattr(self.context, "task_id", ""))
            self.journal("sdk.cost", cost=asdict(entry))
            self._entries[entry.request_id] = entry
            return entry

    def finish(self, entry: RequestCost, usage: Any) -> None:
        from dataclasses import replace
        with self._lock:
            if self._entries[entry.request_id].state != "running":
                return
            if not getattr(usage, "reported", True):
                usage = None
            input_tokens = getattr(usage, "prompt_tokens", None)
            output_tokens = getattr(usage, "completion_tokens", None)
            details = getattr(usage, "prompt_tokens_details", None)
            cached = getattr(details, "cached_tokens", 0) or 0
            created = getattr(details, "cache_creation_tokens", 0) or 0
            valid = (isinstance(input_tokens, int) and isinstance(output_tokens, int) and isinstance(cached, int)
                     and all(type(n) is int and n >= 0 for n in (input_tokens, output_tokens, cached, created)) and cached + created <= input_tokens)
            price = self.options.prices.get(entry.model)
            usd = None
            if valid and price and (not created or price.cache_creation_per_million is not None):
                assert isinstance(input_tokens, int) and isinstance(output_tokens, int)
                usd = str((Decimal(input_tokens - cached - created) * Decimal(str(price.input_per_million)) +
                           Decimal(cached) * Decimal(str(price.cached_input_per_million)) +
                           Decimal(created) * Decimal(str(price.cache_creation_per_million or 0)) +
                           Decimal(output_tokens) * Decimal(str(price.output_per_million))) / Decimal(1_000_000))
            finished = replace(entry, input_tokens=input_tokens if valid else None, output_tokens=output_tokens if valid else None,
                cached_tokens=cached if valid else None, usd=usd, price_source=price.source if price else "",
                cache_creation_tokens=created if valid else None,
                state="measured" if valid else "unknown")
            self.journal("sdk.cost", cost=asdict(finished))
            self._entries[entry.request_id] = finished


class _MeteredClient:
    def __init__(self, inner: Any, ledger: _Ledger, telemetry: Any = None) -> None:
        self._inner, self._ledger = inner, ledger
        self._telemetry = telemetry
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _create(self, **kwargs: Any) -> Any:
        entry = self._ledger.start(kwargs["model"])
        token = self._telemetry.begin("model.request", request_id=entry.request_id, model=entry.model) if self._telemetry else None
        def finish(usage: Any, failed: bool = False) -> None:
            try:
                self._ledger.finish(entry, usage)
            finally:
                if self._telemetry:
                    self._telemetry.end(token, failed=failed)
        try:
            result = self._inner.chat.completions.create(**kwargs)
        except BaseException:
            finish(None, True)
            raise
        if kwargs.get("stream"):
            return _MeteredStream(result, finish)
        finish(getattr(result, "usage", None))
        return result


class _MeteredStream:
    def __init__(self, inner: Any, finish: Callable[..., None]) -> None:
        self.inner, self.finish = inner, finish
        self.usage = None
        self.closed = False
        self.failed = False

    def __iter__(self):
        try:
            for chunk in self.inner:
                if getattr(chunk, "usage", None) is not None:
                    self.usage = chunk.usage
                yield chunk
        except BaseException:
            self.failed = True
            raise
        finally:
            self.close()

    def close(self) -> None:
        if not self.closed:
            try:
                if hasattr(self.inner, "close"):
                    self.inner.close()
            finally:
                self.finish(self.usage, self.failed)
                self.closed = True
