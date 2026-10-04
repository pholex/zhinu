"""HTTP ownership adapter for the shared durable question state machine."""
from __future__ import annotations

import asyncio
import contextlib
from dataclasses import asdict, replace
import json
import threading
from typing import Any

from .questions import (
    ConfigurationError, QuestionAnswer, QuestionManager, QuestionOptions,
    SessionClosedError, SessionStorageError,
)
from .session_log import load_messages


def parse_options(value: Any, *, persist: bool) -> dict[str, Any] | None:
    if value is None:
        return None
    if not persist:
        raise ConfigurationError("Questions require persistent serve sessions")
    if not isinstance(value, dict) or set(value) - {"foreground_timeout_seconds"}:
        raise ConfigurationError("Expected questions: {foreground_timeout_seconds: number}")
    return asdict(QuestionOptions(**value))


def parse_answer(body: Any) -> tuple[tuple[QuestionAnswer, ...], str]:
    if not isinstance(body, dict) or set(body) != {"answers", "idempotency_key"}:
        raise ConfigurationError("Expected answers and idempotency_key")
    rows = body["answers"]
    if not isinstance(rows, list) or not 1 <= len(rows) <= 4:
        raise ConfigurationError("Expected one to four explicit answers")
    replies = []
    for row in rows:
        if (not isinstance(row, dict) or "item_id" not in row
                or set(row) - {"item_id", "selected", "custom", "skipped"}
                or not isinstance(row.get("selected", []), list)):
            raise ConfigurationError("Invalid answer fields")
        replies.append(QuestionAnswer(row["item_id"], tuple(row.get("selected", [])),
                                      row.get("custom", ""), row.get("skipped", False)))
    return tuple(replies), body["idempotency_key"]


class _Host:
    def __init__(self, session: Any, options: dict[str, Any]) -> None:
        self.session = session
        self.session_id = session.id
        self.question_lock = threading.RLock()
        self.question_timeout = options["foreground_timeout_seconds"]
        self.broken = False

    def question_check(self, *, allow_reset: bool = False) -> None:
        if self.question_closed():
            raise SessionClosedError("Session is closing")
        log = self.session.agent.session_log
        if self.broken or log is None or not log.locked or not log.complete:
            raise SessionStorageError("Question storage is unavailable")

    def question_closed(self) -> bool:
        return self.session.question_closing

    def question_cancelled(self) -> bool:
        return self.question_closed() or self.session.agent._interrupt_flag.is_set()

    def question_journal(self, kind: str, **fields: Any) -> None:
        try:
            self.session.agent.session_log.commit_event(kind, **fields)
        except OSError as exc:
            self.broken = True
            raise SessionStorageError("Question persistence failed") from exc

    def question_source_call(self) -> str:
        answered = set()
        for message in reversed(self.session.agent.messages):
            if message.get("role") == "tool":
                answered.add(message.get("tool_call_id"))
            if message.get("role") == "assistant":
                for call in message.get("tool_calls", []):
                    if call.get("function", {}).get("name") == "ask_user" and call.get("id") not in answered:
                        return str(call.get("id", ""))
                break
        return ""

    def question_agent(self) -> Any:
        return self.session.agent

    def question_delivery_allowed(self) -> bool:
        return not self.session.agent._budget_exhausted() and not self.session.check_budget()

    def question_broken(self) -> None:
        self.broken = True


def attach(session: Any, options: dict[str, Any]) -> None:
    host = _Host(session, options)
    host.question_check()
    log = session.agent.session_log
    if load_messages(log.path).corrupt_lines:
        raise SessionStorageError("Question journal contains corrupt records")
    records = []
    for line in log.path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # load_messages already verified the sealed torn-tail contract.
        if not isinstance(record, dict):
            raise SessionStorageError("Question journal contains an invalid record")
        records.append(record)
    loop = asyncio.get_running_loop()

    def changed(question):
        # Scheduling cannot fail a committed transaction when the loop is stopping.
        kind = {"open": "question.opened", "pending": "question.pending", "queued": "question.reply_queued",
                "answered": "question.answered", "cancelled": "question.cancelled"}[question.state]
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(lambda: session.publish(kind, question=asdict(question)))

    manager = QuestionManager(host, records, on_change=changed)
    session.questions = manager
    session.question_options = options
    session.agent._on_step_input = manager._deliver
    session.agent._on_turn_input = manager._deliver
    tool = session.agent.toolbox.get("ask_user")
    if tool is None:
        raise ConfigurationError("ask_user is unavailable")
    session.agent.toolbox.register(replace(tool, handler=manager._ask, check_fn=lambda: True,
        description=tool.description + " 问题由宿主收集回答；超时返回 pending，回答经安全边界接纳。"))


def snapshots(manager: QuestionManager, include_terminal: bool) -> list[dict[str, Any]]:
    with manager._host.question_lock:
        manager._check()
        questions = tuple(manager._items.values()) if include_terminal else manager.list_pending()
        return [asdict(q) for q in questions]
