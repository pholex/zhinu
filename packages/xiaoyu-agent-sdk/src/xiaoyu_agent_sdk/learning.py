"""Bounded candidate extraction from an immutable, explicitly selected session span."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Callable, Awaitable, TYPE_CHECKING

from xiaoyu.memory import MemoryCandidate, MemoryRecord, MemoryScope, MemoryStore
from .types import ConfigurationError

if TYPE_CHECKING:
    from .session import Session
    from .observation import SessionSnapshot


@dataclass(frozen=True)
class MemoryExcerpt:
    source_ref: str
    role: str
    text: str


@dataclass(frozen=True)
class MemorySpan:
    session_id: str
    start: int
    end: int
    messages: tuple[MemoryExcerpt, ...]


MemoryExtractor = Callable[[MemorySpan], tuple[MemoryCandidate, ...] | Awaitable[tuple[MemoryCandidate, ...]]]


def select_span(snapshot: SessionSnapshot, start: int, max_chars: int) -> MemorySpan:
    if type(start) is not int or not 0 <= start < len(snapshot.history) or type(max_chars) is not int or not 256 <= max_chars <= 64000:
        raise ConfigurationError("Choose an existing history offset and a 256–64000 character budget")
    messages = []
    used = 0
    for index in range(start, len(snapshot.history)):
        message = snapshot.history[index]
        item = MemoryExcerpt(f"{snapshot.session_id}#message:{index}", message.role, message.text)
        size = len(json.dumps(asdict(item), ensure_ascii=False))
        if used + size > max_chars:
            break
        used += size
        messages.append(item)
    if not messages:
        raise ConfigurationError("First message exceeds extraction budget; choose a larger budget or another explicit offset")
    return MemorySpan(snapshot.session_id, start, start + len(messages), tuple(messages))


def extract(session: Session, store: MemoryStore, scope: MemoryScope, span: MemorySpan,
            extractor: MemoryExtractor, timeout: float) -> tuple[MemoryRecord, ...]:
    encoded = json.dumps(asdict(span), ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    source = f"{span.session_id}:{span.start}:{span.end}"
    prior = store.receipt(scope, source, digest)
    if prior is not None:
        return prior
    candidates = session._callback(extractor, span, timeout=timeout)
    if not isinstance(candidates, tuple) or len(candidates) > 20:
        raise ConfigurationError("Extractor must return a tuple of at most 20 candidates")
    sources = {m.source_ref: m.text for m in span.messages}
    for candidate in candidates:
        if (not isinstance(candidate, MemoryCandidate) or candidate.source_ref not in sources
                or not isinstance(candidate.evidence, str) or not candidate.evidence.strip()
                or candidate.evidence not in sources[candidate.source_ref]):
            raise ConfigurationError("Every candidate must quote evidence from its actual source message")
    return store.stage(scope, source=source, digest=digest, candidates=candidates)
