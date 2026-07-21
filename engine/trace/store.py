"""TraceStore — durable SQLite record of the run event-trace (#42), so Runs survive a restart.

Its record() IS the EventBus sink. Skips the redundant `model_request` payload (the whole conversation,
re-emitted every step, already in sessions.db) and caps each event's data so one huge tool_result can't
bloat a row. Own events.db, physically separate from sessions.db so the disk-heavy trace can be turned
off / deleted independently.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time

_TRUNC = "…[truncated]"


class TraceStore:
    def __init__(self, path: str, event_max_bytes: int = 16384, replay_runs: int = 50,
                 retention_mode: str = "age+runs", retention_days: int = 30,
                 keep_runs_per_session: int = 200, prune_interval_s: float = 3600.0):
        self.path = path
        self.event_max_bytes = event_max_bytes
        self.replay_runs = replay_runs
        self.retention_mode = retention_mode
        self.retention_days = retention_days
        self.keep_runs_per_session = keep_runs_per_session
        self.prune_interval_s = prune_interval_s
        self._last_prune = time.time()          # avoid an unwanted cold-start prune on the first record()
        self._lock = threading.Lock()
        self._rw = sqlite3.connect(path, check_same_thread=False)
        self._rw.row_factory = sqlite3.Row
        self._rw.execute("PRAGMA journal_mode=WAL")
        self._rw.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL, run_id TEXT NOT NULL, step INTEGER,
                kind TEXT NOT NULL, data TEXT, ts REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_events_session_ts ON events(session_id, ts);
            CREATE INDEX IF NOT EXISTS ix_events_run ON events(run_id);
            """
        )
        self._rw.commit()

    # ---- write (the EventBus sink) ----
    def record(self, ev) -> None:
        if ev.kind == "model_request":                       # redundant with sessions.db; the bloat
            return
        try:
            data = json.dumps(ev.data or {}, default=str)
        except Exception:
            data = "{}"
        if len(data) > self.event_max_bytes:
            data = data[: self.event_max_bytes] + _TRUNC
        with self._lock:
            self._rw.execute(
                "INSERT INTO events(session_id, run_id, step, kind, data, ts) VALUES (?,?,?,?,?,?)",
                (ev.session_id, ev.run_id, ev.step, ev.kind, data, ev.ts),
            )
            self._rw.commit()
        self._maybe_prune()          # OUTSIDE the lock — prune() takes it (threading.Lock isn't reentrant)

    def _maybe_prune(self) -> None:
        """Opportunistic retention, same shape as ReliabilityStore's `_last_prune` — no background task
        to schedule. Retention settings are read at construction, so a change applies on restart (same
        as the on/off flag)."""
        now = time.time()
        if now - self._last_prune < self.prune_interval_s:
            return
        self._last_prune = now
        try:
            self.prune(self.retention_mode, self.retention_days, self.keep_runs_per_session)
        except Exception:
            pass                      # retention must never break a turn

    # ---- read (replay) ----
    def recent(self, session_id: str) -> list[dict]:
        with self._lock:
            runs = [r["run_id"] for r in self._rw.execute(
                "SELECT run_id, MAX(ts) m FROM events WHERE session_id=? GROUP BY run_id "
                "ORDER BY m DESC LIMIT ?", (session_id, self.replay_runs))]
            if not runs:
                return []
            ph = ",".join("?" for _ in runs)
            rows = self._rw.execute(
                f"SELECT session_id, run_id, step, kind, data, ts FROM events "
                f"WHERE session_id=? AND run_id IN ({ph}) ORDER BY ts",
                (session_id, *runs)).fetchall()
        out = []
        for r in rows:
            try:
                d = json.loads(r["data"] or "{}")
            except Exception:
                d = {"_raw": r["data"]}
            out.append({"run_id": r["run_id"], "session_id": r["session_id"], "step": r["step"],
                        "kind": r["kind"], "data": d, "ts": r["ts"]})
        return out

    # ---- retention ----
    def prune(self, mode: str, days: int, keep_runs: int) -> None:
        if mode == "off":
            return
        with self._lock:
            if mode in ("age", "age+runs"):
                self._rw.execute("DELETE FROM events WHERE ts < ?", (time.time() - days * 86400,))
            if mode in ("runs", "age+runs"):
                # run_id is only 40 bits (engine.py mints uuid4().hex[:10]) from one pool shared by all
                # sessions, so collisions across sessions are plausible over a long-lived deployment —
                # match on the (session_id, run_id) pair so the delete stays scoped per-session.
                self._rw.execute(
                    "DELETE FROM events WHERE (session_id, run_id) IN ("
                    "  SELECT session_id, run_id FROM ("
                    "    SELECT session_id, run_id,"
                    "           ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY m DESC) rn"
                    "    FROM (SELECT session_id, run_id, MAX(ts) m FROM events GROUP BY session_id, run_id)"
                    "  ) WHERE rn > ?)", (keep_runs,))
            self._rw.commit()
