"""ReliabilityStore — SQLite record of run outcomes for the reliability harness.

Two tables: `outcomes` (raw rows, bounded retention → drill-down) and `daily` (incremental per-day
rollups, kept forever → cheap trends). All writes go through record(); a single rw connection guarded
by a lock (reads here are internal/trusted aggregates, not user SQL, so no read-only surface needed).
"""
from __future__ import annotations

import sqlite3
import threading
import time

_DETAIL_CAP = 200


def _day(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


class ReliabilityStore:
    def __init__(self, path: str, retention_days: int = 30):
        self.path = path
        self.retention_days = retention_days
        self._lock = threading.Lock()
        self._last_prune = 0.0
        self._rw = sqlite3.connect(path, check_same_thread=False)
        self._rw.row_factory = sqlite3.Row
        self._rw.executescript(
            """
            CREATE TABLE IF NOT EXISTS outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL, day TEXT, kind TEXT, entity TEXT,
                ok INTEGER, ms INTEGER, detail TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_outcomes_day ON outcomes(day);
            CREATE INDEX IF NOT EXISTS ix_outcomes_entity_day ON outcomes(entity, day);
            CREATE TABLE IF NOT EXISTS daily (
                day TEXT, kind TEXT, entity TEXT,
                calls INTEGER DEFAULT 0, ok_count INTEGER DEFAULT 0, err_count INTEGER DEFAULT 0,
                sum_ms INTEGER DEFAULT 0, cnt_ms INTEGER DEFAULT 0,
                PRIMARY KEY (day, kind, entity)
            );
            """
        )
        self._rw.commit()

    # ---- write ----
    def record(self, kind: str, entity: str, ok, ms, detail: str, ts: float) -> None:
        entity = entity or ""
        detail = (detail or "")[:_DETAIL_CAP]
        day = _day(ts)
        okc = 1 if ok in (1, True) else 0
        errc = 1 if ok in (0, False) else 0
        sms = int(ms) if isinstance(ms, (int, float)) else 0
        cms = 1 if isinstance(ms, (int, float)) else 0
        with self._lock:
            self._rw.execute(
                "INSERT INTO outcomes(ts,day,kind,entity,ok,ms,detail) VALUES (?,?,?,?,?,?,?)",
                (ts, day, kind, entity, (None if ok is None else okc), (int(ms) if cms else None), detail),
            )
            self._rw.execute(
                """INSERT INTO daily(day,kind,entity,calls,ok_count,err_count,sum_ms,cnt_ms)
                   VALUES (?,?,?,1,?,?,?,?)
                   ON CONFLICT(day,kind,entity) DO UPDATE SET
                     calls=calls+1, ok_count=ok_count+?, err_count=err_count+?,
                     sum_ms=sum_ms+?, cnt_ms=cnt_ms+?""",
                (day, kind, entity, okc, errc, sms, cms, okc, errc, sms, cms),
            )
            self._rw.commit()
        self._maybe_prune(ts)

    # ---- retention ----
    def _maybe_prune(self, ts: float) -> None:
        if ts - self._last_prune > 3600:            # at most once/hour
            self._last_prune = ts
            self.prune(now=ts)

    def prune(self, now: float) -> None:
        cutoff = now - self.retention_days * 86400
        with self._lock:
            self._rw.execute("DELETE FROM outcomes WHERE ts < ?", (cutoff,))
            self._rw.commit()

    # ---- reads (aggregate) ----
    def _cutoff_day(self, days: int, now: float = None) -> str:
        return _day((now if now is not None else time.time()) - days * 86400)

    def summary(self, days: int = 30, now: float = None) -> dict:
        since = self._cutoff_day(days, now)
        row = self._rw.execute(
            "SELECT COALESCE(SUM(calls),0) c, COALESCE(SUM(ok_count),0) ok "
            "FROM daily WHERE kind='tool' AND day>=?", (since,)).fetchone()
        calls, ok = row["c"], row["ok"]
        rt = self._rw.execute(
            "SELECT COALESCE(SUM(calls),0) c, COALESCE(SUM(ok_count),0) ok "
            "FROM daily WHERE kind='routine' AND day>=?", (since,)).fetchone()
        friction = self._rw.execute(
            "SELECT COALESCE(SUM(calls),0) n FROM daily "
            "WHERE kind IN ('reprompt','parse_fail') AND day>=?", (since,)).fetchone()["n"]
        return {
            "enabled": True,
            "days": days,
            "tool_calls": calls,
            "tool_success_pct": (round(ok / calls * 100, 1) if calls else None),
            "routine_runs": rt["c"],
            "routine_completion_pct": (round(rt["ok"] / rt["c"] * 100, 1) if rt["c"] else None),
            "friction_events": friction,
        }

    def per_tool(self, days: int = 30, now: float = None) -> list:
        since = self._cutoff_day(days, now)
        rows = self._rw.execute(
            "SELECT entity, SUM(calls) calls, SUM(ok_count) ok, SUM(sum_ms) sms, SUM(cnt_ms) cms "
            "FROM daily WHERE kind='tool' AND day>=? GROUP BY entity", (since,)).fetchall()
        out = []
        for r in rows:
            spark = self._rw.execute(
                "SELECT day, calls, ok_count FROM daily "
                "WHERE kind='tool' AND entity=? AND day>=? ORDER BY day", (r["entity"], since)).fetchall()
            last_err = self._rw.execute(
                "SELECT detail FROM outcomes WHERE kind='tool' AND entity=? AND ok=0 "
                "ORDER BY ts DESC LIMIT 1", (r["entity"],)).fetchone()
            out.append({
                "entity": r["entity"],
                "calls": r["calls"],
                "success_pct": round(r["ok"] / r["calls"] * 100, 1) if r["calls"] else None,
                "mean_ms": round(r["sms"] / r["cms"]) if r["cms"] else None,
                "last_error": (last_err["detail"] if last_err else ""),
                "spark": [round(s["ok_count"] / s["calls"] * 100) if s["calls"] else 0 for s in spark],
            })
        out.sort(key=lambda t: (t["success_pct"] if t["success_pct"] is not None else 100))
        return out

    def per_routine(self, days: int = 30, now: float = None) -> list:
        since = self._cutoff_day(days, now)
        rows = self._rw.execute(
            "SELECT entity, SUM(calls) runs, SUM(ok_count) ok FROM daily "
            "WHERE kind='routine' AND day>=? GROUP BY entity", (since,)).fetchall()
        return [{
            "entity": r["entity"], "runs": r["runs"],
            "completion_pct": round(r["ok"] / r["runs"] * 100, 1) if r["runs"] else None,
        } for r in rows]

    def loop_health(self, days: int = 30, now: float = None) -> dict:
        since = self._cutoff_day(days, now)
        out = {}
        for kind in ("parse_fail", "reprompt", "validation_fail"):
            series = self._rw.execute(
                "SELECT day, SUM(calls) n FROM daily WHERE kind=? AND day>=? GROUP BY day ORDER BY day",
                (kind, since)).fetchall()
            out[kind] = {"total": sum(s["n"] for s in series),
                         "series": [{"day": s["day"], "n": s["n"]} for s in series]}
        return out

    def recent_failures(self, entity: str = None, limit: int = 20) -> list:
        if entity:
            rows = self._rw.execute(
                "SELECT ts, kind, entity, detail FROM outcomes WHERE ok=0 AND entity=? "
                "ORDER BY ts DESC LIMIT ?", (entity, limit)).fetchall()
        else:
            rows = self._rw.execute(
                "SELECT ts, kind, entity, detail FROM outcomes WHERE ok=0 "
                "ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
