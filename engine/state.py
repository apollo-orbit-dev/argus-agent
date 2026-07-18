"""Single owner of session state.

Forward-compat Property 1: conversation/session state has ONE owner and flows
through ONE seam. A future memory system (persistence + retrieval) inserts here
— never scatter session state across the Telegram handler, dashboard, and loop.
In-memory for this phase.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Session:
    session_id: str
    conversation: list[dict] = field(default_factory=list)
    runs: list[str] = field(default_factory=list)


class SessionStore:
    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str) -> Session:
        s = self._sessions.get(session_id)
        if s is None:
            s = Session(session_id=session_id)
            self._sessions[session_id] = s
        return s

    def append_message(self, session_id: str, msg: dict) -> None:
        self.get_or_create(session_id).conversation.append(msg)

    def extend_messages(self, session_id: str, msgs: list[dict]) -> None:
        self.get_or_create(session_id).conversation.extend(msgs)

    def conversation(self, session_id: str) -> list[dict]:
        s = self._sessions.get(session_id)
        return list(s.conversation) if s else []

    def record_run(self, session_id: str, run_id: str) -> None:
        self.get_or_create(session_id).runs.append(run_id)

    def reset(self, session_id: str) -> None:
        s = self._sessions.get(session_id)
        if s:
            s.conversation.clear()
