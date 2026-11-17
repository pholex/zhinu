"""Immutable host views and bounded notification observation signals."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SessionState = Literal["idle", "running", "tasks", "settling", "broken", "closing", "closed"]


@dataclass(frozen=True)
class Notification:
    key: str
    text: str
    wake: bool


@dataclass(frozen=True)
class ModelUsageSnapshot:
    model: str
    calls: int
    prompt_tokens: int
    completion_tokens: int


@dataclass(frozen=True)
class UsageSnapshot:
    """Measured totals including an explicitly restored or forked usage checkpoint."""

    model_calls: int
    prompt_tokens: int
    completion_tokens: int
    by_model: tuple[ModelUsageSnapshot, ...]


@dataclass(frozen=True)
class MessageSnapshot:
    """Display projection, not a wire message or a replay format."""

    role: str
    text: str
    image_count: int
    tool_names: tuple[str, ...]
    tool_call_id: str


@dataclass(frozen=True)
class PlanStep:
    step: str
    status: str


@dataclass(frozen=True)
class SessionSnapshot:
    session_id: str
    model: str
    mode: str
    budget_tokens: int | None
    context_tokens: int
    usage: UsageSnapshot
    history: tuple[MessageSnapshot, ...]
    last_assistant_text: str
    pending_notifications: tuple[Notification, ...]
    plan: tuple[PlanStep, ...] = ()


from xiaoyu._observation import _Notifications as _Notifications, _Subscription as _Subscription
