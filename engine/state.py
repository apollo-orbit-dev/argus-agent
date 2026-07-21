"""Single owner of session state — durable (SQLite) conversation + full raw transcript log.

Two persisted structures per session:
- working_set: the model-facing compacted conversation (what conversation() returns and the loop sends),
  restored exactly on restart.
- messages: an append-only RAW log of every message, never compacted — the owner's full transcript.

Ephemeral sessions (id starting with '__', e.g. '__routine__:...') are scratch: in-memory only.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_ephemeral(session_id: str) -> bool:
    return session_id.startswith("__")


@dataclass
class Session:
    session_id: str
    conversation: list[dict] = field(default_factory=list)   # the working set (model-facing)
    runs: list[str] = field(default_factory=list)


class SessionStore:
    def __init__(self, path: str | None = None):
        self.path = path
        self._lock = threading.Lock()
        self._sessions: dict[str, Session] = {}
        self._db: sqlite3.Connection | None = None
        if path:
            self._db = sqlite3.connect(path, check_same_thread=False)
            self._db.row_factory = sqlite3.Row
            self._db.executescript(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "  id TEXT PRIMARY KEY, name TEXT, origin TEXT, working_set TEXT, runs TEXT,"
                "  created TEXT, updated TEXT, archived INTEGER DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS messages ("
                "  session_id TEXT, seq INTEGER, role TEXT, content TEXT, ts TEXT,"
                "  PRIMARY KEY (session_id, seq));")
            self._db.commit()

    # ---- persistence helpers ----
    def _persist(self, s: Session) -> None:
        if self._db is None or _is_ephemeral(s.session_id):
            return
        self._db.execute(
            "INSERT INTO sessions (id, name, origin, working_set, runs, created, updated) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET working_set=excluded.working_set, runs=excluded.runs, "
            "updated=excluded.updated",
            (s.session_id, s.session_id, None, json.dumps(s.conversation), json.dumps(s.runs),
             _now(), _now()))
        self._db.commit()

    def _log(self, session_id: str, msgs: list[dict]) -> None:
        if self._db is None or _is_ephemeral(session_id):
            return
        row = self._db.execute("SELECT COALESCE(MAX(seq), -1)+1 AS n FROM messages WHERE session_id=?",
                               (session_id,)).fetchone()
        seq = row["n"]
        for m in msgs:
            content = m.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, default=str)
            self._db.execute(
                "INSERT INTO messages (session_id, seq, role, content, ts) VALUES (?,?,?,?,?)",
                (session_id, seq, m.get("role", ""), content, _now()))
            seq += 1
        self._db.commit()

    def _load(self, session_id: str) -> Session | None:
        if self._db is None or _is_ephemeral(session_id):
            return None
        row = self._db.execute("SELECT working_set, runs FROM sessions WHERE id=?",
                               (session_id,)).fetchone()
        if row is None:
            return None
        return Session(session_id=session_id,
                       conversation=json.loads(row["working_set"] or "[]"),
                       runs=json.loads(row["runs"] or "[]"))

    # ---- public API (caller-visible semantics unchanged) ----
    def get_or_create(self, session_id: str) -> Session:
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                s = self._load(session_id)
                if s is None:
                    s = Session(session_id=session_id)
                    self._persist(s)
                self._sessions[session_id] = s
            return s

    def append_message(self, session_id: str, msg: dict) -> None:
        s = self.get_or_create(session_id)
        with self._lock:
            s.conversation.append(msg)
            self._log(session_id, [msg])
            self._persist(s)

    def extend_messages(self, session_id: str, msgs: list[dict]) -> None:
        s = self.get_or_create(session_id)
        with self._lock:
            s.conversation.extend(msgs)
            self._log(session_id, msgs)
            self._persist(s)

    def set_working_set(self, session_id: str, msgs: list[dict]) -> None:
        """Replace the model-facing working set (compaction) WITHOUT touching the raw log."""
        s = self.get_or_create(session_id)
        with self._lock:
            s.conversation = list(msgs)
            self._persist(s)

    def conversation(self, session_id: str) -> list[dict]:
        # Pure read: load-if-exists (and cache), but never create/persist a row. Lock-guarded so the
        # DB read is consistent with concurrent writes (keeps the "all DB access under self._lock"
        # invariant literally true, even for a future non-asyncio caller).
        with self._lock:
            s = self._sessions.get(session_id)
            if s is None:
                s = self._load(session_id)           # from DB if it exists, else None
                if s is not None:
                    self._sessions[session_id] = s   # cache; do NOT create/persist a row
            return list(s.conversation) if s else []

    def record_run(self, session_id: str, run_id: str) -> None:
        s = self.get_or_create(session_id)
        with self._lock:
            s.runs.append(run_id)
            self._persist(s)

    def reset(self, session_id: str) -> None:
        s = self.get_or_create(session_id)
        with self._lock:
            s.conversation.clear()
            self._persist(s)            # working set cleared; raw log untouched

    def session_messages(self, session_id: str, limit: int = 200, offset: int = 0) -> dict:
        if self._db is None:
            return {"session_id": session_id, "total": 0, "messages": [], "limit": limit, "offset": offset}
        limit = max(1, min(int(limit), 1000)); offset = max(0, int(offset))
        with self._lock:
            total = self._db.execute("SELECT COUNT(*) c FROM messages WHERE session_id=?",
                                     (session_id,)).fetchone()["c"]
            cur = self._db.execute("SELECT seq, role, content, ts FROM messages WHERE session_id=? "
                                   "ORDER BY seq LIMIT ? OFFSET ?", (session_id, limit, offset))
            messages = [dict(r) for r in cur.fetchall()]
        return {"session_id": session_id, "total": total,
                "messages": messages, "limit": limit, "offset": offset}

    def list_sessions(self) -> list[dict]:
        if self._db is None:
            return [{"id": sid, "name": sid, "origin": None, "updated": None, "message_count": 0}
                    for sid in self._sessions if not _is_ephemeral(sid)]
        with self._lock:
            rows = self._db.execute(
                "SELECT s.id, s.name, s.origin, s.updated, "
                "  (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id) AS message_count "
                "FROM sessions s WHERE s.archived = 0 ORDER BY s.updated DESC")
            return [dict(r) for r in rows]

    def create_session(self, name: str | None = None) -> str:
        # id-style label like a run id (ses_<hex>); a real title can be set later via rename_session
        # (or the future auto-title feature). Default display name IS the id until renamed.
        sid = "ses_" + uuid.uuid4().hex[:10]
        with self._lock:
            self._sessions[sid] = Session(session_id=sid)
            if self._db is not None:
                self._db.execute(
                    "INSERT INTO sessions (id, name, origin, working_set, runs, created, updated) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (sid, name or sid, "dashboard", "[]", "[]", _now(), _now()))
                self._db.commit()
        return sid

    def rename_session(self, session_id: str, name: str) -> None:
        with self._lock:
            if self._db is not None:
                self._db.execute("UPDATE sessions SET name=?, updated=? WHERE id=?",
                                 (name, _now(), session_id))
                self._db.commit()

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)
            if self._db is not None:
                self._db.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
                self._db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
                self._db.commit()
