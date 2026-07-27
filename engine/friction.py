"""Append-only record of the places the agent gives up.

The loop already DETECTS its own dead ends — it stops on `stuck_repeating` and it stops
after a second unparseable response — but both signals are fire-and-forget events that die
with the turn. This module writes one durable record at each of those moments, so ordinary
daily use accumulates a ranked list of the things Argus cannot do and cannot explain (the
stdlib-only sandbox, a blocked egress proxy, an unreadable time format — all found by hand
until now). `argus friction` reads it back grouped by (kind, tool), most frequent first.

Two rules govern everything here:

1. **It records, it does not intervene.** No call into this module may change what a turn
   returns or how it gets there. Every failure — unwritable path, full disk, a malformed
   file from an older build — is swallowed and logged at DEBUG. A friction log that can
   break a turn is strictly worse than no friction log at all.
2. **`detail` holds user content.** It is the tool output the model kept hitting, which may
   quote whatever the user asked about. The file therefore lives in the instance's data dir
   next to sessions.db and is gitignored; it must never be committable.

Persistence follows engine/experimental/dep_store.py: plain JSON on disk, loaded tolerantly,
rewritten whole on each append.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Optional

log = logging.getLogger("argus.friction")

DETAIL_MAX = 400        # chars of the repeated error text kept per record
MAX_RECORDS = 5000      # oldest are dropped past this, so an unattended instance can't grow forever


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class FrictionLog:
    """Append-only log of give-up moments. Never raises into the caller."""

    def __init__(self, path: str):
        self.path = path
        self.records: list[dict] = []
        self._load()

    # ---- persistence ----
    def _load(self) -> None:
        try:
            if os.path.exists(self.path):
                data = json.load(open(self.path, encoding="utf-8"))
                recs = data.get("records") if isinstance(data, dict) else data
                self.records = [r for r in (recs or []) if isinstance(r, dict)]
        except Exception:
            # A truncated or hand-edited file must not stop the engine from starting, and must
            # not stop the NEXT record from being written — start from empty and carry on.
            log.debug("could not load friction log from %s", self.path, exc_info=True)
            self.records = []

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump({"records": self.records}, fh, indent=2)

    # ---- mutations ----
    def record(self, kind: str, session_id: str = "", tool: Optional[str] = None,
               attempts: int = 0, detail: str = "", model: str = "") -> Optional[dict]:
        """Append one give-up record. Returns it, or None if anything at all went wrong.

        The whole body is guarded: building the record touches caller-supplied values that
        could be any type, and the write touches a filesystem that may be full or read-only.
        Neither may surface in the turn that called this."""
        try:
            rec = {"at": _now(), "session_id": str(session_id or ""), "kind": str(kind),
                   "tool": str(tool) if tool else None, "attempts": int(attempts or 0),
                   "detail": str(detail or "")[:DETAIL_MAX], "model": str(model or "")}
            self.records.append(rec)
            if len(self.records) > MAX_RECORDS:
                del self.records[:-MAX_RECORDS]
            self._save()
            return rec
        except Exception:
            log.debug("could not write friction record to %s", self.path, exc_info=True)
            return None

    # ---- queries ----
    def summary(self) -> list[dict]:
        """Records grouped by (kind, tool), most frequent first — the ranked gap list.

        The top row is the next thing to fix, so ties break on most-recent-first to keep a
        live problem above a stale one of the same size. `detail` on a group is the most
        recent one seen, which is the error text worth reading."""
        groups: dict[tuple, dict] = {}
        for r in self.records:
            key = (r.get("kind", ""), r.get("tool"))
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"kind": key[0], "tool": key[1], "count": 0,
                                   "attempts_max": 0, "last_at": "", "detail": "", "model": ""}
            g["count"] += 1
            g["attempts_max"] = max(g["attempts_max"], int(r.get("attempts") or 0))
            at = str(r.get("at") or "")
            if at >= g["last_at"]:      # ISO-8601 sorts lexicographically
                g["last_at"] = at
                g["detail"] = str(r.get("detail") or "")
                g["model"] = str(r.get("model") or "")
        out = sorted(groups.values(), key=lambda g: g["last_at"], reverse=True)   # tie-break
        out.sort(key=lambda g: g["count"], reverse=True)                          # stable: count wins
        return out
