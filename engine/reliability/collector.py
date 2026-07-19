"""ReliabilityCollector — maps StepEvents to outcome rows for the reliability harness.

Registered as an EventBus sink. Stateless except for a tiny bounded map used to pair a tool_call with
its tool_result to compute latency. Never raises into publish() (the sink wrapper guards it too)."""
from __future__ import annotations

from engine.events import StepEvent


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
            ok = bool(d.get("ok"))
            detail = "" if ok else str(d.get("result", d.get("error", "")))[:200]
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
