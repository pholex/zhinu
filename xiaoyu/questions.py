"""Durable questions and coalesced observation under the session storage lease."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, replace
import json
import math
import threading
from time import monotonic
from typing import Any, AsyncGenerator, Generator, Literal, Callable, Protocol
import uuid

from ._observation import _Notifications


class QuestionError(Exception):
    pass


class ConfigurationError(QuestionError, ValueError):
    pass


class SessionClosedError(QuestionError):
    pass


class SessionBusyError(QuestionError):
    pass


class SessionStorageError(QuestionError):
    pass


class QuestionHost(Protocol):
    session_id: str
    question_lock: Any
    question_timeout: float

    def question_check(self, *, allow_reset: bool = False) -> None: ...
    def question_closed(self) -> bool: ...
    def question_cancelled(self) -> bool: ...
    def question_journal(self, kind: str, **fields: Any) -> None: ...
    def question_source_call(self) -> str: ...
    def question_delivery_allowed(self) -> bool: ...
    def question_agent(self) -> Any: ...
    def question_broken(self) -> None: ...


@dataclass(frozen=True)
class QuestionOptions:
    """Durable questions; zero preserves immediate pending, positive seconds wait."""

    foreground_timeout_seconds: float = 0

    def __post_init__(self) -> None:
        value = self.foreground_timeout_seconds
        try:
            valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
        except OverflowError:
            valid = False
        if not valid:
            raise ConfigurationError("foreground_timeout_seconds must be finite and nonnegative")


@dataclass(frozen=True)
class QuestionOption:
    label: str
    description: str = ""


@dataclass(frozen=True)
class QuestionItem:
    item_id: str
    question: str
    options: tuple[QuestionOption, ...]
    multi_select: bool = False


@dataclass(frozen=True)
class QuestionAnswer:
    item_id: str
    selected: tuple[str, ...] = ()
    custom: str = ""
    skipped: bool = False


@dataclass(frozen=True)
class QuestionSnapshot:
    question_id: str
    session_id: str
    generation: int
    tool_call_id: str
    items: tuple[QuestionItem, ...]
    state: Literal["open", "pending", "queued", "answered", "cancelled"] = "pending"
    version: int = 1
    answers: tuple[QuestionAnswer, ...] = ()
    answer_id: str = ""
    idempotency_key: str = ""


class QuestionConflictError(QuestionError):
    """The question was cancelled, superseded, or answered differently."""


class QuestionNotFoundError(QuestionError):
    pass


@dataclass(frozen=True)
class QuestionEvent:
    """Latest committed state; observers may coalesce intermediate versions."""

    kind: Literal["question.opened", "question.pending", "question.reply_queued", "question.answered", "question.cancelled"]
    question: QuestionSnapshot


@dataclass(frozen=True)
class QuestionErrors:
    configuration: type[Exception] = ConfigurationError
    storage: type[Exception] = SessionStorageError
    conflict: type[Exception] = QuestionConflictError
    not_found: type[Exception] = QuestionNotFoundError


class QuestionManager:
    errors = QuestionErrors()

    def __init__(self, host: QuestionHost, records: list[dict[str, Any]],
                 on_change: Callable[[QuestionSnapshot], None] | None = None) -> None:
        self._host = host
        self._on_change = on_change
        self._generation = 0
        self._items: dict[str, QuestionSnapshot] = {}
        self._changes = _Notifications()
        self._condition = threading.Condition(host.question_lock)
        try:
            for record in records:
                if record.get("event") == "clear":
                    self._reset()
                elif record.get("event") in {"sdk.question", "sdk.question.delivery"}:
                    data = record["question"]
                    items = tuple(QuestionItem(i["item_id"], i["question"],
                        tuple(QuestionOption(**o) for o in i["options"]), i["multi_select"]) for i in data["items"])
                    answers = tuple(QuestionAnswer(a["item_id"], tuple(a["selected"]), a["custom"], a["skipped"])
                                    for a in data["answers"])
                    current = QuestionSnapshot(**{**data, "items": items, "answers": answers})
                    self._validate_items(items)
                    previous = self._items.get(current.question_id)
                    if (current.session_id != host.session_id or current.generation != self._generation
                            or type(current.generation) is not int
                            or any(not isinstance(v, str) for v in (current.question_id, current.tool_call_id,
                                                                   current.answer_id, current.idempotency_key))
                            or type(current.version) is not int
                            or current.version != (previous.version + 1 if previous else 1)
                            or current.state not in {"open", "pending", "queued", "answered", "cancelled"}
                            or not current.question_id or not items):
                        raise ValueError("Invalid question record")
                    if previous is None:
                        if current.state not in {"open", "pending"} or current.answers or current.answer_id or current.idempotency_key:
                            raise ValueError("Invalid new question")
                    else:
                        if (current.items != previous.items or current.tool_call_id != previous.tool_call_id
                                or current.state not in ({"pending", "queued", "cancelled"} if previous.state == "open" else
                                                        {"queued", "cancelled"} if previous.state == "pending" else
                                                        {"answered", "cancelled"} if previous.state == "queued" else set())):
                            raise ValueError("Invalid question transition")
                        if previous.state == "queued" and (current.answers, current.answer_id, current.idempotency_key) != (
                                previous.answers, previous.answer_id, previous.idempotency_key):
                            raise ValueError("Answer changed after submission")
                    if current.state in {"open", "pending"} and answers:
                        raise ValueError("Unexpected answers before submission")
                    if not answers and (current.answer_id or current.idempotency_key):
                        raise ValueError("Unexpected answer identity")
                    if answers:
                        self._validate_answers(current, answers)
                        if not current.answer_id or not current.idempotency_key:
                            raise ValueError("Missing answer identity")
                    if current.state in {"queued", "answered"} and not answers:
                        raise ValueError("Missing answers")
                    delivery = record.get("event") == "sdk.question.delivery"
                    if delivery != (current.state == "answered") or delivery and record.get("message") != self._message(current):
                        raise ValueError("Invalid delivery record")
                    self._items[current.question_id] = current
        except Exception as exc:
            raise self.errors.storage("Cannot restore question state") from exc
        # A previous process's foreground wait cannot survive its storage lease.
        # Commit the transition, so subsequent resumes retain monotonic versions.
        with host.question_lock:
            for question in tuple(self._items.values()):
                if question.state == "open":
                    self._save(replace(question, state="pending", version=question.version + 1))

    def _check(self, *, allow_reset: bool = False) -> None:
        self._host.question_check(allow_reset=allow_reset)

    def get(self, question_id: str) -> QuestionSnapshot:
        if not isinstance(question_id, str) or not question_id:
            raise self.errors.configuration("A question ID is required")
        with self._host.question_lock:
            self._check()
            if question_id not in self._items:
                raise self.errors.not_found("Question not found in this session")
            return self._items[question_id]

    def list_pending(self) -> tuple[QuestionSnapshot, ...]:
        with self._host.question_lock:
            self._check()
            return tuple(q for q in self._items.values() if q.state in {"open", "pending", "queued"})

    def _observed(self, seen: dict[str, int]) -> tuple[QuestionEvent, ...]:
        with self._host.question_lock:
            if self._host.question_closed():
                return ()
            self._check(allow_reset=True)
            events = []
            for question in self._items.values():
                if seen.get(question.question_id, 0) >= question.version:
                    continue
                kinds: dict[str, Literal["question.opened", "question.pending", "question.reply_queued", "question.answered", "question.cancelled"]] = {
                    "open": "question.opened", "pending": "question.pending", "queued": "question.reply_queued",
                    "answered": "question.answered", "cancelled": "question.cancelled"}
                kind = kinds[question.state]
                events.append(QuestionEvent(kind, question))
                seen[question.question_id] = question.version
            return tuple(events)

    def watch(self) -> Generator[QuestionEvent, None, None]:
        """Current states (including terminal), then coalesced changes until close."""
        subscription = self._changes.subscribe()
        seen: dict[str, int] = {}
        try:
            while subscription.queue.get():
                yield from self._observed(seen)
        finally:
            self._changes.unsubscribe(subscription)

    def _save(self, question: QuestionSnapshot, *, message: dict[str, Any] | None = None) -> None:
        fields: dict[str, Any] = {"question": asdict(question)}
        if message is not None:
            fields["message"] = message
        try:
            self._host.question_journal("sdk.question.delivery" if message is not None else "sdk.question", **fields)
        finally:
            # A failed submission must also wake the foreground waiter to observe
            # the broken store instead of waiting for its entire timeout.
            self._condition.notify_all()
        self._items[question.question_id] = question
        self._changes.changed()
        if self._on_change is not None:
            self._on_change(question)

    def _ask(self, questions: Any = None, **extra: Any) -> str:
        from xiaoyu.agent import normalize_questions
        if extra:
            return "ERROR: ask_user accepts only questions"
        normalized = normalize_questions(questions)
        if isinstance(normalized, str):
            return "ERROR: " + normalized
        items = tuple(QuestionItem(uuid.uuid4().hex, q["question"],
            tuple(QuestionOption(o["label"], o["description"]) for o in q["options"]), q["multi_select"])
            for q in normalized)
        try:
            self._validate_items(items)
        except self.errors.configuration:
            return "ERROR: Questions are too long or contain duplicate option labels"
        with self._host.question_lock:
            self._check()
            timeout = self._host.question_timeout
            question = QuestionSnapshot(uuid.uuid4().hex, self._host.session_id, self._generation,
                                        self._host.question_source_call(), items,
                                        state="open" if timeout > 0 else "pending")
            self._save(question)
            if timeout > 0:
                question = self._wait_foreground(question.question_id, timeout)
        messages = {
            "pending": "尚未收到回答。仅继续与答案无关的工作；依赖答案的工作必须等待，不要重复提问。",
            "queued": "回答已持久化，等待本批工具收尾后作为用户消息接纳；不要重复提问。",
            "cancelled": "问题已取消，没有收到可接纳的回答；不要假定任何选择。",
        }
        return json.dumps({"status": question.state, "question_id": question.question_id,
                           "message": messages[question.state]}, ensure_ascii=False)

    def _wake(self) -> None:
        # interrupt() must remain nonblocking even during a slow storage write.
        # If the lock is busy, the waiter will see the flag on its bounded poll.
        if not self._condition.acquire(blocking=False):
            return
        try:
            self._condition.notify_all()
        finally:
            self._condition.release()

    def _wait_foreground(self, question_id: str, timeout: float) -> QuestionSnapshot:
        # Caller holds the session mutex; Condition.wait releases it for host
        # submissions. No extra timer thread or model request is needed.
        from xiaoyu.errors import Interrupted
        deadline = monotonic() + timeout
        while True:
            question = self._items[question_id]
            if self._host.question_cancelled():
                if question.state == "open":
                    self._save(replace(question, state="pending", version=question.version + 1))
                raise Interrupted("Foreground question wait interrupted")
            self._check()
            if question.state != "open":
                return question
            remaining = deadline - monotonic()
            if remaining <= 0:
                question = replace(question, state="pending", version=question.version + 1)
                self._save(question)
                return question
            self._condition.wait(min(remaining, 1.0))

    @classmethod
    def _validate_items(cls, items: tuple[QuestionItem, ...]) -> None:
        if not 1 <= len(items) <= 4 or len({i.item_id for i in items}) != len(items):
            raise cls.errors.configuration("Invalid question items")
        for item in items:
            if (not isinstance(item.item_id, str) or not item.item_id
                    or not isinstance(item.question, str) or not item.question.strip() or len(item.question) > 4000
                    or type(item.multi_select) is not bool or not 1 <= len(item.options) <= 9
                    or any(not isinstance(o.label, str) or not o.label.strip() or len(o.label) > 200
                           or not isinstance(o.description, str) or len(o.description) > 1000 for o in item.options)
                    or len({o.label for o in item.options}) != len(item.options)):
                raise cls.errors.configuration("Invalid question item or options")

    @classmethod
    def _validate_answers(cls, question: QuestionSnapshot, answers: tuple[QuestionAnswer, ...]) -> tuple[QuestionAnswer, ...]:
        if len(answers) != len(question.items) or not all(isinstance(a, QuestionAnswer) for a in answers):
            raise cls.errors.configuration("Every question item requires one explicit answer or skip")
        if any(not isinstance(a.item_id, str) for a in answers) or len({a.item_id for a in answers}) != len(answers):
            raise cls.errors.configuration("Duplicate answer item ID")
        by_id = {a.item_id: a for a in answers}
        result = []
        for item in question.items:
            answer = by_id.get(item.item_id)
            if answer is None or type(answer.skipped) is not bool or not isinstance(answer.custom, str):
                raise cls.errors.configuration("Invalid question answer")
            if (not isinstance(answer.selected, (tuple, list))
                    or any(not isinstance(s, str) or s not in {o.label for o in item.options} for s in answer.selected)
                    or len(set(answer.selected)) != len(answer.selected)
                    or not item.multi_select and len(answer.selected) > 1
                    or len(answer.custom) > 4000
                    or answer.skipped and (answer.selected or answer.custom)
                    or not answer.skipped and not (answer.selected or answer.custom.strip())):
                raise cls.errors.configuration("Invalid selection, custom text or skip")
            # Option order, not client checkbox order, defines idempotent content.
            selected = tuple(o.label for o in item.options if o.label in answer.selected)
            result.append(replace(answer, selected=selected))
        return tuple(result)

    def answer(self, question_id: str, answers: tuple[QuestionAnswer, ...], idempotency_key: str) -> QuestionSnapshot:
        if not isinstance(answers, (tuple, list)):
            raise self.errors.configuration("Answers must be a sequence of QuestionAnswer")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise self.errors.configuration("A nonempty idempotency key of at most 200 characters is required")
        with self._host.question_lock:
            question = self.get(question_id)
            if question.generation != self._generation or question.state == "cancelled":
                raise self.errors.conflict("Question was cancelled or belongs to an earlier generation")
            normalized = self._validate_answers(question, tuple(answers))
            if question.state not in {"open", "pending"}:
                if question.idempotency_key == idempotency_key and question.answers == normalized:
                    return question
                raise self.errors.conflict("Question already has a different submission")
            question = replace(question, state="queued", version=question.version + 1, answers=normalized,
                               answer_id=uuid.uuid4().hex, idempotency_key=idempotency_key)
            self._save(question)
            return question

    def cancel(self, question_id: str) -> QuestionSnapshot:
        with self._host.question_lock:
            question = self.get(question_id)
            if question.state == "answered":
                raise self.errors.conflict("An accepted answer cannot be withdrawn")
            if question.state != "cancelled":
                question = replace(question, state="cancelled", version=question.version + 1)
                self._save(question)
            return question

    @staticmethod
    def _message(question: QuestionSnapshot) -> dict[str, Any]:
        return {"role": "user", "content": "[用户对待答问题的回答；不是工具审批]\n" + json.dumps({
            "question_id": question.question_id, "answer_id": question.answer_id,
            "items": [asdict(i) for i in question.items], "answers": [asdict(a) for a in question.answers],
        }, ensure_ascii=False)}

    def _deliver(self) -> bool:
        with self._host.question_lock:
            if self._host.question_cancelled() or self._host.question_closed():
                return False
            self._check()
            agent = self._host.question_agent()
            if not self._host.question_delivery_allowed():
                return False
            delivered = False
            for question in tuple(self._items.values()):
                if question.state != "queued":
                    continue
                question = replace(question, state="answered", version=question.version + 1)
                message = self._message(question)
                # One acknowledged journal record contains acceptance and history.
                self._save(question, message=message)
                try:
                    agent.messages.append(message)
                    agent._history_rewritten()
                    delivered = True
                except BaseException:
                    self._host.question_broken()
                    raise
            return delivered

    def _cancel_pending(self) -> None:
        with self._host.question_lock:
            for question in tuple(self._items.values()):
                if question.state in {"open", "pending", "queued"}:
                    self._save(replace(question, state="cancelled", version=question.version + 1))

    def _reset(self) -> None:
        with self._condition:
            self._generation += 1
            self._items = {key: replace(q, state="cancelled", version=q.version + 1)
                           if q.state in {"open", "pending", "queued"} else q for key, q in self._items.items()}
            self._changes.changed()
            self._condition.notify_all()


class AsyncQuestionManager:
    def __init__(self, manager: QuestionManager) -> None:
        self._manager = manager

    async def get(self, question_id: str) -> QuestionSnapshot:
        return await asyncio.to_thread(self._manager.get, question_id)

    async def list_pending(self) -> tuple[QuestionSnapshot, ...]:
        return await asyncio.to_thread(self._manager.list_pending)

    async def answer(self, question_id: str, answers: tuple[QuestionAnswer, ...], idempotency_key: str) -> QuestionSnapshot:
        return await asyncio.to_thread(self._manager.answer, question_id, answers, idempotency_key)

    async def cancel(self, question_id: str) -> QuestionSnapshot:
        return await asyncio.to_thread(self._manager.cancel, question_id)

    async def watch(self) -> AsyncGenerator[QuestionEvent, None]:
        """Async observation without blocking the event loop or model execution."""
        subscription = self._manager._changes.subscribe(asyncio.get_running_loop())
        seen: dict[str, int] = {}
        try:
            while await subscription.wait():
                for event in await asyncio.to_thread(self._manager._observed, seen):
                    yield event
        finally:
            self._manager._changes.unsubscribe(subscription)
