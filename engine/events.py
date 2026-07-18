"""Structured step-log event stream — the one source of truth feeding both the
SSE endpoint (live dashboard traces) and the standard logger.

Every loop step emits exactly one StepEvent. Events are held in a per-session
ring buffer (so a late SSE subscriber can replay a run) and fanned out to live
subscribers via asyncio queues.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import AsyncIterator, Optional

log = logging.getLogger("argus.events")

# StepEvent.kind vocabulary (documented, not enforced):
#   info | model_request | model_response | tool_call | validation |
#   tool_result | final | error | reprompt | skill


@dataclass
class StepEvent:
    run_id: str
    session_id: str
    step: int
    kind: str
    data: dict = field(default_factory=dict)
    ts: float = 0.0

    def to_json(self) -> dict:
        return asdict(self)


class EventBus:
    def __init__(self, maxlen: int = 500):
        self._maxlen = maxlen
        self._history: dict[str, deque[StepEvent]] = defaultdict(lambda: deque(maxlen=maxlen))
        # subscribers: (session_filter_or_None, queue)
        self._subscribers: list[tuple[Optional[str], asyncio.Queue]] = []

    async def publish(self, ev: StepEvent) -> None:
        self._history[ev.session_id].append(ev)
        log.info("[%s step=%s %s] %s", ev.session_id, ev.step, ev.kind,
                 _short(ev.data))
        for session_filter, q in list(self._subscribers):
            if session_filter is None or session_filter == ev.session_id:
                q.put_nowait(ev)

    def recent(self, session_id: str) -> list[StepEvent]:
        return list(self._history.get(session_id, ()))

    def clear(self, session_id: str) -> None:
        """Drop a session's replay buffer — used by 'new session' so a reconnect doesn't replay
        the previous session's events."""
        self._history.pop(session_id, None)

    async def subscribe(self, session_id: Optional[str]) -> AsyncIterator[StepEvent]:
        """Yield NEW events (published after subscription). session_id=None => all."""
        q: asyncio.Queue = asyncio.Queue()
        entry = (session_id, q)
        self._subscribers.append(entry)
        try:
            while True:
                yield await q.get()
        finally:
            self._subscribers.remove(entry)


def _short(data: dict, limit: int = 300) -> str:
    s = str(data)
    return s if len(s) <= limit else s[:limit] + "…"
