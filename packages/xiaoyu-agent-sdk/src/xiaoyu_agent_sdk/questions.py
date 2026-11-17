"""SDK bindings for the shared durable question state machine."""
from __future__ import annotations

from decimal import Decimal
from typing import Any, TYPE_CHECKING

from xiaoyu import questions as core
from xiaoyu.questions import (
    QuestionOption as QuestionOption, QuestionItem as QuestionItem,
    QuestionAnswer as QuestionAnswer, QuestionSnapshot as QuestionSnapshot,
    QuestionEvent as QuestionEvent, AsyncQuestionManager as AsyncQuestionManager,
)
from .types import ConfigurationError, SDKError, SessionBusyError, SessionClosedError, SessionStorageError

if TYPE_CHECKING:
    from .session import Session


class QuestionOptions(core.QuestionOptions):
    def __post_init__(self) -> None:
        try:
            super().__post_init__()
        except core.ConfigurationError as exc:
            raise ConfigurationError(str(exc)) from exc


class QuestionConflictError(SDKError):
    """The question was cancelled, superseded, or answered differently."""


class QuestionNotFoundError(SDKError):
    pass


class _Host:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.session_id = session.session_id
        self.question_lock = session._mutex
        assert session.options.questions is not None
        self.question_timeout = session.options.questions.foreground_timeout_seconds

    def question_check(self, *, allow_reset: bool = False) -> None:
        session = self.session
        if self.question_closed():
            raise SessionClosedError("Session is closed or closing")
        if session._control_error or session._log is None or session._log.broken_reason:
            raise SessionStorageError("Question storage is unavailable")
        if session._control_name == "reset" and not allow_reset:
            raise SessionBusyError("Session reset is in progress")

    def question_closed(self) -> bool:
        return self.session._closed or self.session._closing

    def question_cancelled(self) -> bool:
        return self.session._cancel.is_set() or self.question_closed()

    def question_journal(self, kind: str, **fields: Any) -> None:
        self.session._journal(kind, **fields)

    def question_source_call(self) -> str:
        return getattr(self.session._context, "tool_call_id", "")

    def question_agent(self) -> Any:
        assert self.session._agent is not None
        return self.session._agent

    def question_delivery_allowed(self) -> bool:
        agent = self.question_agent()
        if agent._budget_exhausted():
            return False
        ledger = self.session._ledger
        if ledger is not None:
            cost, budget = ledger.snapshot(), ledger.options
            if (budget.max_requests is not None and cost.requests >= budget.max_requests
                    or budget.max_usd is not None and (agent.config.model not in budget.prices
                        or cost.unknown_requests or Decimal(cost.known_usd) >= Decimal(str(budget.max_usd)))):
                return False
        return True

    def question_broken(self) -> None:
        self.session._control_error = True


class QuestionManager(core.QuestionManager):
    errors = core.QuestionErrors(ConfigurationError, SessionStorageError, QuestionConflictError, QuestionNotFoundError)

    def __init__(self, session: Session, records: list[dict[str, Any]]) -> None:
        super().__init__(_Host(session), records)
