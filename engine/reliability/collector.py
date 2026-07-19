"""ReliabilityCollector — maps StepEvents to outcome rows for the reliability harness.

Registered as an EventBus sink. Stateless except for a tiny bounded map used to pair a tool_call with
its tool_result to compute latency. Never raises into publish() (the sink wrapper guards it too)."""
from __future__ import annotations

from engine.events import StepEvent

# A tool_result with ok=True still counts as a FAILURE when its text is error-shaped: many tools
# (created tools especially) CATCH their own exception and RETURN an error string instead of raising,
# so `ok` only means "didn't crash," not "worked." Without this, a tool that returns
# "Error fetching model info: 307 redirect" every call would score 100%. Honest no-data / CANNOT
# sentinels are deliberately NOT flagged here — those are successful (empty) outcomes, not failures.
_ERROR_MARKERS = (
    " error:", "traceback (most recent call last)", "looks wrong",
    "error fetching", "unable to fetch", "could not fetch", "failed to fetch",
    "unable to parse", "could not parse", "failed to parse", "couldn't parse",
)


def _looks_like_error(text: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if t.startswith("error"):                       # "Error: ...", "Error fetching ..."
        return True
    return any(m in t for m in _ERROR_MARKERS)


class ReliabilityCollector:
    def __init__(self, store, max_pending: int = 512):
        self.store = store
        self.max_pending = max_pending
        self._pending: dict[tuple, float] = {}          # (run_id, step) -> tool_call ts

    def record(self, ev: StepEvent) -> None:
        d = ev.data or {}
        k = ev.kind
        if k == "tool_call":
            if len(self._pending) >= self.max_pending:
                self._pending.clear()                    # hard cap; drop stale pairing state
            self._pending[(ev.run_id, ev.step)] = ev.ts
            return
        if k == "tool_result":
            call_ts = self._pending.pop((ev.run_id, ev.step), None)
            ms = int((ev.ts - call_ts) * 1000) if call_ts is not None else None
            result = str(d.get("result", d.get("error", "")))
            ok = bool(d.get("ok")) and not _looks_like_error(result)   # ran but returned an error string = failure
            detail = "" if ok else result[:200]
            self.store.record("tool", d.get("tool", ""), ok, ms, detail, ev.ts)
            return
        if k == "validation" and d.get("ok") is False:
            self.store.record("validation_fail", d.get("tool", ""), False, None,
                              str(d.get("error", "")), ev.ts)
            return
        if k == "reprompt":
            self.store.record("reprompt", "", None, None, str(d.get("reason", "")), ev.ts)
            return
        if k == "error" and d.get("kind") == "parse_failure":
            self.store.record("parse_fail", "", None, None, str(d.get("reason", "")), ev.ts)
            return
        if k == "routine_result":
            self.store.record("routine", d.get("name", ""), bool(d.get("ok")),
                              d.get("ms"), str(d.get("delivery_error") or d.get("error") or "")[:200], ev.ts)
            return
        # final/skill/info/model_* → ignored (also clears any dangling pairing for this run on final)
        if k == "final":
            self._pending.pop((ev.run_id, ev.step), None)
