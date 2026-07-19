# Reliability Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A passive, always-on instrument that records tool/routine/loop-health outcomes from the
existing event stream into `reliability.db` and surfaces them on a new dashboard **Reliability** page.

**Architecture:** `EventBus.publish()` gains a synchronous, fail-safe `sink` fan-out. A
`ReliabilityCollector` (registered as a sink) maps `StepEvent`s → outcome rows and writes them to a
`ReliabilityStore` (SQLite: raw `outcomes` + incremental `daily` rollups). Read-only, admin-gated
`/reliability/*` endpoints serve aggregates to a new dashboard page. Zero model cost.

**Tech Stack:** Python stdlib `sqlite3`, FastAPI, the static dashboard (index.html/app.js/styles.css).

## Global Constraints

- No model calls anywhere in the harness or its tests (repo offline-test rule).
- The sink must never break a run: `publish()` wraps each sink in try/except, logs, and continues.
- Dedicated `reliability.db` (covered by the existing `*.db` gitignore). Its own connection + lock.
- Config-gated by `enable_reliability` (default `true`); when off, no sink is registered and endpoints
  return `{"enabled": false}`.
- Follow existing patterns: SQLite store like `TableStore` (rw `sqlite3.connect` + `threading.Lock` +
  `row_factory = sqlite3.Row`); db path via `Path(__file__).resolve().parents[1] / "reliability.db"`;
  endpoints admin-gated via the existing `_require_admin(request)`.
- Timestamps: `StepEvent.ts` is a float epoch seconds (`time.time()`); local day string via
  `time.strftime("%Y-%m-%d", time.localtime(ts))`.

---

### Task 1: EventBus sink hook

**Files:**
- Modify: `engine/events.py` (class `EventBus`)
- Test: `tests/test_events_sink.py`

**Interfaces:**
- Produces: `EventBus.add_sink(fn: Callable[[StepEvent], None]) -> None`; `publish()` now also invokes
  every registered sink synchronously, isolated by try/except.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_events_sink.py
import asyncio
from engine.events import EventBus, StepEvent


def _ev(kind="tool_result", sid="s"):
    return StepEvent(run_id="r", session_id=sid, step=1, kind=kind, data={"ok": True}, ts=1.0)


def test_sink_receives_published_events():
    bus = EventBus()
    seen = []
    bus.add_sink(seen.append)
    asyncio.run(bus.publish(_ev()))
    assert len(seen) == 1 and seen[0].kind == "tool_result"


def test_raising_sink_does_not_break_publish():
    bus = EventBus()
    def boom(ev): raise RuntimeError("sink failure")
    ok = []
    bus.add_sink(boom)
    bus.add_sink(ok.append)          # a later good sink still runs
    asyncio.run(bus.publish(_ev()))  # must not raise
    assert len(ok) == 1
    assert bus.recent("s")           # event still reached history
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_events_sink.py -v`
Expected: FAIL — `EventBus` has no attribute `add_sink`.

- [ ] **Step 3: Implement the sink hook**

In `engine/events.py`, in `EventBus.__init__` add `self._sinks: list = []`, and add the method +
fan-out. Full changed pieces:

```python
    def __init__(self, maxlen: int = 500):
        self._maxlen = maxlen
        self._history: dict[str, deque[StepEvent]] = defaultdict(lambda: deque(maxlen=maxlen))
        self._subscribers: list[tuple[Optional[str], asyncio.Queue]] = []
        self._sinks: list = []                      # synchronous, fail-safe outcome listeners

    def add_sink(self, fn) -> None:
        """Register a synchronous listener invoked on every publish (e.g. the reliability collector).
        Sinks must be cheap and non-blocking; exceptions are logged and swallowed."""
        self._sinks.append(fn)

    async def publish(self, ev: StepEvent) -> None:
        self._history[ev.session_id].append(ev)
        log.info("[%s step=%s %s] %s", ev.session_id, ev.step, ev.kind, _short(ev.data))
        for session_filter, q in list(self._subscribers):
            if session_filter is None or session_filter == ev.session_id:
                q.put_nowait(ev)
        for sink in self._sinks:
            try:
                sink(ev)
            except Exception:
                log.exception("event sink failed (ignored)")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_events_sink.py -v`
Expected: PASS (both tests).

- [ ] **Step 5: Commit**

```bash
git add engine/events.py tests/test_events_sink.py
git commit -m "feat(reliability): add fail-safe sink hook to EventBus.publish"
```

---

### Task 2: ReliabilityStore + config retention flag

**Files:**
- Create: `engine/reliability/__init__.py` (empty)
- Create: `engine/reliability/store.py`
- Modify: `config.py` (add `reliability_raw_retention_days`)
- Test: `tests/test_reliability_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ReliabilityStore(path, retention_days=30)` with
  `record(kind, entity, ok, ms, detail, ts)`, `summary(days=30)`, `per_tool(days=30)`,
  `per_routine(days=30)`, `loop_health(days=30)`, `recent_failures(entity=None, limit=20)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reliability_store.py
import time
from engine.reliability.store import ReliabilityStore

DAY = 86400


def _store(tmp_path):
    return ReliabilityStore(str(tmp_path / "rel.db"), retention_days=30)


def test_record_and_summary(tmp_path):
    s = _store(tmp_path)
    now = 1_700_000_000.0
    s.record("tool", "weather", ok=1, ms=100, detail="", ts=now)
    s.record("tool", "weather", ok=1, ms=300, detail="", ts=now)
    s.record("tool", "web_search", ok=0, ms=50, detail="timeout", ts=now)
    s.record("validation_fail", "web_search", ok=0, ms=None, detail="bad args", ts=now)
    s.record("reprompt", "", ok=None, ms=None, detail="no tool call", ts=now)
    out = s.summary(days=30)
    assert out["tool_calls"] == 3
    assert out["tool_success_pct"] == round(2 / 3 * 100, 1)
    tools = {t["entity"]: t for t in s.per_tool(days=30)}
    assert tools["weather"]["success_pct"] == 100.0 and tools["weather"]["mean_ms"] == 200
    assert tools["web_search"]["success_pct"] == 0.0
    lh = s.loop_health(days=30)
    assert lh["reprompt"]["total"] == 1 and lh["validation_fail"]["total"] == 1


def test_routine_completion(tmp_path):
    s = _store(tmp_path)
    now = 1_700_000_000.0
    s.record("routine", "morning", ok=1, ms=5000, detail="", ts=now)
    s.record("routine", "morning", ok=0, ms=100, detail="step 'x' failed", ts=now)
    r = {x["entity"]: x for x in s.per_routine(days=30)}["morning"]
    assert r["runs"] == 2 and r["completion_pct"] == 50.0


def test_retention_prunes_raw_but_keeps_rollup(tmp_path):
    s = _store(tmp_path)
    old = 1_700_000_000.0 - 40 * DAY
    new = 1_700_000_000.0
    s.record("tool", "weather", ok=1, ms=10, detail="", ts=old)
    s.record("tool", "weather", ok=1, ms=10, detail="", ts=new)
    s.prune(now=new)
    # raw drill-down only has the recent row...
    assert len(s.recent_failures(limit=50)) >= 0            # no failures, but query works
    raw = s._rw.execute("SELECT COUNT(*) AS n FROM outcomes").fetchone()["n"]
    assert raw == 1                                          # old raw row pruned
    daily = s._rw.execute("SELECT COUNT(*) AS n FROM daily").fetchone()["n"]
    assert daily == 2                                        # both days' rollups kept forever


def test_recent_failures_filters_by_entity(tmp_path):
    s = _store(tmp_path)
    now = 1_700_000_000.0
    s.record("tool", "web_search", ok=0, ms=5, detail="HTTP 500", ts=now)
    s.record("tool", "weather", ok=0, ms=5, detail="nope", ts=now)
    fails = s.recent_failures(entity="web_search", limit=10)
    assert len(fails) == 1 and fails[0]["detail"] == "HTTP 500"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reliability_store.py -v`
Expected: FAIL — module `engine.reliability.store` does not exist.

- [ ] **Step 3: Create the package + store**

Create empty `engine/reliability/__init__.py`. Create `engine/reliability/store.py`:

```python
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
        okc = 1 if ok == 1 or ok is True else 0
        errc = 1 if ok == 0 or ok is False else 0
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
    def _cutoff_day(self, days: int) -> str:
        return _day(time.time() - days * 86400)

    def summary(self, days: int = 30) -> dict:
        since = self._cutoff_day(days)
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

    def per_tool(self, days: int = 30) -> list:
        since = self._cutoff_day(days)
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

    def per_routine(self, days: int = 30) -> list:
        since = self._cutoff_day(days)
        rows = self._rw.execute(
            "SELECT entity, SUM(calls) runs, SUM(ok_count) ok FROM daily "
            "WHERE kind='routine' AND day>=? GROUP BY entity", (since,)).fetchall()
        return [{
            "entity": r["entity"], "runs": r["runs"],
            "completion_pct": round(r["ok"] / r["runs"] * 100, 1) if r["runs"] else None,
        } for r in rows]

    def loop_health(self, days: int = 30) -> dict:
        since = self._cutoff_day(days)
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
```

In `config.py`, add the field near the other feature settings and to `_ENV_FIELDS`:

```python
    # Reliability harness: passive instrument of tool/routine/loop outcomes (dashboard only).
    enable_reliability: bool = True
    reliability_raw_retention_days: int = 30
```
Add `"enable_reliability", "reliability_raw_retention_days",` to the `_ENV_FIELDS` tuple.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reliability_store.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/reliability/__init__.py engine/reliability/store.py config.py tests/test_reliability_store.py
git commit -m "feat(reliability): ReliabilityStore (raw outcomes + daily rollups) + config"
```

---

### Task 3: ReliabilityCollector

**Files:**
- Create: `engine/reliability/collector.py`
- Test: `tests/test_reliability_collector.py`

**Interfaces:**
- Consumes: `StepEvent` (from Task 1 sink), `ReliabilityStore.record` (Task 2).
- Produces: `ReliabilityCollector(store)` with `record(ev: StepEvent) -> None` (the sink callable).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reliability_collector.py
from engine.events import StepEvent
from engine.reliability.collector import ReliabilityCollector


class _FakeStore:
    def __init__(self): self.rows = []
    def record(self, kind, entity, ok, ms, detail, ts):
        self.rows.append({"kind": kind, "entity": entity, "ok": ok, "ms": ms, "detail": detail, "ts": ts})


def _ev(kind, data, step=1, ts=1.0):
    return StepEvent(run_id="r", session_id="s", step=step, kind=kind, data=data, ts=ts)


def test_tool_success_with_latency_pairing():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_call", {"tool": "weather"}, ts=10.0))
    c.record(_ev("tool_result", {"tool": "weather", "ok": True}, ts=10.4))
    assert st.rows == [{"kind": "tool", "entity": "weather", "ok": True, "ms": 400, "detail": "", "ts": 10.4}]


def test_tool_failure_records_detail():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"}, ts=2.0))
    assert st.rows[0]["kind"] == "tool" and st.rows[0]["ok"] is False and "HTTP 500" in st.rows[0]["detail"]


def test_no_data_ok_result_counts_as_success():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_result", {"tool": "ask_data", "ok": True, "result": "CANNOT"}, ts=1.0))
    assert st.rows[0]["ok"] is True                           # honest outcome, not a failure


def test_validation_failure_recorded_separately():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("validation", {"tool": "weather", "ok": False, "error": "missing 'location'"}))
    assert st.rows[0]["kind"] == "validation_fail" and st.rows[0]["entity"] == "weather"


def test_valid_validation_is_not_recorded():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("validation", {"tool": "weather", "ok": True}))
    assert st.rows == []                                       # only failures are loop-health signal


def test_reprompt_and_parse_fail_are_loop_health():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("reprompt", {"reason": "no tool call"}))
    c.record(_ev("error", {"kind": "parse_failure", "reason": "bad json"}))
    kinds = [r["kind"] for r in st.rows]
    assert kinds == ["reprompt", "parse_fail"]


def test_generic_error_is_ignored():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("error", {"error": "model call failed"}))     # not a parse_failure
    assert st.rows == []


def test_routine_result_recorded():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("routine_result", {"name": "morning", "ok": True, "ms": 5000}))
    assert st.rows[0] == {"kind": "routine", "entity": "morning", "ok": True, "ms": 5000, "detail": "", "ts": 1.0}


def test_ignored_kinds_do_nothing():
    st = _FakeStore(); c = ReliabilityCollector(st)
    for k in ("info", "model_request", "model_response", "final", "skill"):
        c.record(_ev(k, {}))
    assert st.rows == []


def test_pending_map_bounded():
    st = _FakeStore(); c = ReliabilityCollector(st, max_pending=2)
    for i in range(5):
        c.record(_ev("tool_call", {"tool": "t"}, step=i, ts=float(i)))
    assert len(c._pending) <= 2                                # never leaks
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reliability_collector.py -v`
Expected: FAIL — module does not exist.

- [ ] **Step 3: Implement the collector**

```python
# engine/reliability/collector.py
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reliability_collector.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add engine/reliability/collector.py tests/test_reliability_collector.py
git commit -m "feat(reliability): ReliabilityCollector event->outcome mapping"
```

---

### Task 4: Emit `routine_result` from the executor

**Files:**
- Modify: `engine/routines/executor.py` (`RoutineExecutor.run`)
- Test: `tests/test_routines.py` (add a case)

**Interfaces:**
- Consumes: the executor already has `emit` (an optional callback passed to `run`) and computes
  `RoutineResult`.
- Produces: a `routine_result` StepEvent is emitted at the end of a run when `emit` is provided.

**Note:** `RoutineExecutor.run(..., emit=None, ...)` currently calls `emit(sid, typ, ok, ms, preview)`
per step (a positional bridge). The reliability signal needs a *whole-routine* outcome. Add a second,
optional structured emitter `on_result` used only for the final outcome, wired by the engine to
`EventBus.publish` of a `routine_result` event. This keeps the per-step `emit` bridge unchanged.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_routines.py
def test_executor_emits_routine_result():
    ex = _exec({"comprehensive": "X"})
    events = []
    r = _routine(steps=[{"type": "tool", "id": "comprehensive", "tool": "comprehensive", "args": {}}])
    res = asyncio.run(ex.run(r, "sess", deliver=False, on_result=lambda payload: events.append(payload)))
    assert res.ok
    assert len(events) == 1
    p = events[0]
    assert p["name"] == r.name and p["ok"] is True
    assert p["steps_total"] == 1 and p["steps_ok"] == 1 and isinstance(p["ms"], int)
```
(`_exec`/`_routine` helpers already exist in the file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_routines.py::test_executor_emits_routine_result -v`
Expected: FAIL — `run()` has no `on_result` parameter.

- [ ] **Step 3: Implement the emit**

In `engine/routines/executor.py`, add `on_result=None` to `RoutineExecutor.run(...)`'s signature, and
just before each `return RoutineResult(...)` build and emit the payload. Extract a helper so all
return paths are covered:

```python
    async def run(self, routine, session_id: str, *, source: str = "on_demand",
                  emit=None, deliver: bool = True, seed=None, on_result=None) -> RoutineResult:
        start = time.time()
        # ... existing body unchanged, but replace each `return RoutineResult(...)` with
        #     `return self._finish(RoutineResult(...), on_result, start, results)`
```

Add the helper:

```python
    def _finish(self, result, on_result, start, results):
        if on_result:
            try:
                on_result({
                    "name": result.name,
                    "ok": result.ok,
                    "delivered": result.delivered,
                    "delivery_error": result.delivery_error,
                    "ms": int((time.time() - start) * 1000),
                    "steps_ok": sum(1 for s in results if s.ok),
                    "steps_total": len(results),
                })
            except Exception:
                log.warning("routine on_result emit failed", exc_info=True)
        return result
```

Apply `_finish(...)` to all three `return RoutineResult(...)` sites (early failure, timeout, success),
passing the `results` list and `start` available in scope.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_routines.py -v`
Expected: PASS (existing + new).

- [ ] **Step 5: Commit**

```bash
git add engine/routines/executor.py tests/test_routines.py
git commit -m "feat(reliability): emit routine_result outcome from RoutineExecutor"
```

---

### Task 5: Engine wiring + config flag

**Files:**
- Modify: `config.py` (add `enable_reliability` — done in Task 2 if not already; ensure present)
- Modify: `engine/engine.py` (construct store + collector, register sink, wire routine on_result,
  add `reliability_*` methods)
- Test: `tests/test_reliability_engine.py`

**Interfaces:**
- Consumes: `ReliabilityStore`, `ReliabilityCollector`, `EventBus.add_sink`, executor `on_result`.
- Produces: `engine.reliability_summary/tools/routines/loop/failures(...)` delegating to the store;
  `engine._reliability` (store or None when disabled).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reliability_engine.py
import asyncio
from config import Config
from engine.engine import Engine


def _engine(tmp_path, enabled=True):
    return Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                         enable_reliability=enabled))


def test_engine_records_tool_outcomes_via_sink(tmp_path):
    e = _engine(tmp_path)
    from engine.events import StepEvent
    asyncio.run(e.events.publish(StepEvent("r", "s", 1, "tool_call", {"tool": "weather"}, 1.0)))
    asyncio.run(e.events.publish(StepEvent("r", "s", 1, "tool_result", {"tool": "weather", "ok": True}, 1.2)))
    out = e.reliability_summary(days=30)
    assert out["enabled"] and out["tool_calls"] == 1 and out["tool_success_pct"] == 100.0


def test_disabled_reliability_returns_disabled(tmp_path):
    e = _engine(tmp_path, enabled=False)
    assert e.reliability_summary()["enabled"] is False
    assert e._reliability is None
```
(If `Engine(Config(...))` writes `reliability.db` into the repo dir, the test should point the db at a
tmp path — see implementation note: use a config-overridable db dir or monkeypatch `parents[1]`. The
plan's implementer should route the reliability db path through the same root the other stores use and
have the test use `tmp_path` via an env/arg. Simplest: accept an optional `data_dir` on Engine for
tests, defaulting to the repo root — matching how other stores resolve their paths.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reliability_engine.py -v`
Expected: FAIL — `Engine` has no `reliability_summary` / `_reliability`.

- [ ] **Step 3: Implement engine wiring**

In `engine/engine.py.__init__`, after the EventBus is created and after the routine executor is set
up, add:

```python
        # Reliability harness (passive; dashboard-only). Consumes the event stream via a sink.
        self._reliability = None
        if config.enable_reliability:
            from engine.reliability.store import ReliabilityStore
            from engine.reliability.collector import ReliabilityCollector
            self._reliability = ReliabilityStore(
                str(Path(__file__).resolve().parents[1] / "reliability.db"),
                retention_days=config.reliability_raw_retention_days)
            self.events.add_sink(ReliabilityCollector(self._reliability).record)
```

Wire the routine outcome: wherever routines run through the executor
(`run_routine_now` and `_scheduled_routine_run`), pass
`on_result=self._emit_routine_result` and add the emitter:

```python
    def _emit_routine_result(self, payload: dict) -> None:
        if self._reliability is None:
            return
        import time as _t
        # publish as a StepEvent so the collector (and future consumers) see it uniformly
        ev = StepEvent(run_id="routine", session_id="__routine__", step=0,
                       kind="routine_result", data=payload, ts=_t.time())
        # publish is async; schedule it without blocking the executor
        try:
            asyncio.get_running_loop().create_task(self.events.publish(ev))
        except RuntimeError:
            asyncio.run(self.events.publish(ev))
```

Add the delegate methods:

```python
    def reliability_summary(self, days: int = 30) -> dict:
        return self._reliability.summary(days) if self._reliability else {"enabled": False}

    def reliability_tools(self, days: int = 30) -> list:
        return self._reliability.per_tool(days) if self._reliability else []

    def reliability_routines(self, days: int = 30) -> list:
        return self._reliability.per_routine(days) if self._reliability else []

    def reliability_loop(self, days: int = 30) -> dict:
        return self._reliability.loop_health(days) if self._reliability else {}

    def reliability_failures(self, entity: str = None, limit: int = 20) -> list:
        return self._reliability.recent_failures(entity, limit) if self._reliability else []
```

**Implementation note (test db path):** so `test_reliability_engine.py` doesn't write into the repo,
give `Engine.__init__` an optional `data_dir: Optional[str] = None` that defaults to
`Path(__file__).resolve().parents[1]` and route the reliability db (and only it, for this branch)
through it; the test passes `data_dir=str(tmp_path)`. Keep it minimal.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reliability_engine.py tests/test_routines.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/engine.py config.py tests/test_reliability_engine.py
git commit -m "feat(reliability): wire store+collector into engine, routine_result publish"
```

---

### Task 6: Backend `/reliability/*` endpoints

**Files:**
- Modify: `backend/app.py`
- Test: `tests/test_reliability_api.py`

**Interfaces:**
- Consumes: `engine.reliability_*` (Task 5).
- Produces: admin-gated `GET /reliability/{summary,tools,routines,loop,failures}`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_reliability_api.py — follow the existing TestClient pattern in tests/test_config_admin.py
import asyncio
import httpx
from httpx import ASGITransport
from config import Config
from engine.engine import Engine
from backend.app import create_app


def _client(tmp_path, enabled=True):
    eng = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                        admin_token="", enable_reliability=enabled))
    app = create_app(eng)
    return eng, httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def test_summary_endpoint(tmp_path):
    async def go():
        eng, c = _client(tmp_path)
        from engine.events import StepEvent
        await eng.events.publish(StepEvent("r", "s", 1, "tool_call", {"tool": "weather"}, 1.0))
        await eng.events.publish(StepEvent("r", "s", 1, "tool_result", {"tool": "weather", "ok": True}, 1.1))
        r = await c.get("/reliability/summary")
        assert r.status_code == 200
        assert r.json()["enabled"] and r.json()["tool_calls"] == 1
        await c.aclose()
    asyncio.run(go())


def test_disabled_returns_enabled_false(tmp_path):
    async def go():
        eng, c = _client(tmp_path, enabled=False)
        r = await c.get("/reliability/summary")
        assert r.status_code == 200 and r.json()["enabled"] is False
        await c.aclose()
    asyncio.run(go())
```
(Match the admin-token handling that `tests/test_config_admin.py` already uses; if `admin_token` is
set, add the `X-Admin-Token` header.)

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_reliability_api.py -v`
Expected: FAIL — routes 404.

- [ ] **Step 3: Implement endpoints**

In `backend/app.py`, next to the `/logs` routes (same `_require_admin` gate):

```python
    @app.get("/reliability/summary")
    async def reliability_summary(request: Request, days: int = 30):
        _require_admin(request)
        return engine.reliability_summary(days)

    @app.get("/reliability/tools")
    async def reliability_tools(request: Request, days: int = 30):
        _require_admin(request)
        return engine.reliability_tools(days)

    @app.get("/reliability/routines")
    async def reliability_routines(request: Request, days: int = 30):
        _require_admin(request)
        return engine.reliability_routines(days)

    @app.get("/reliability/loop")
    async def reliability_loop(request: Request, days: int = 30):
        _require_admin(request)
        return engine.reliability_loop(days)

    @app.get("/reliability/failures")
    async def reliability_failures(request: Request, entity: str = "", limit: int = 20):
        _require_admin(request)
        return engine.reliability_failures(entity or None, limit)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_reliability_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app.py tests/test_reliability_api.py
git commit -m "feat(reliability): admin-gated /reliability/* endpoints"
```

---

### Task 7: Dashboard "Reliability" page

**Files:**
- Modify: `dashboard/index.html` (rail item + `#page-reliability`)
- Modify: `dashboard/app.js` (page load + fetch + render + inline-SVG sparkline)
- Modify: `dashboard/styles.css` (page styles)
- Test: manual + Playwright smoke (`scratchpad/qa/reliability.js`)

**Interfaces:**
- Consumes: `/reliability/{summary,tools,routines,loop,failures}`.
- Produces: a navigable Reliability page.

**Note:** mirror the existing Logs page wiring exactly (rail `data-page`, `#page-*` section, a
`loadX()` called on nav-in from the existing page-router in `app.js`). Look at how `#page-logs` is
registered and how `connectLogs()` is invoked on navigation, and follow that pattern.

- [ ] **Step 1: Add the rail item + page section (index.html)**

Add a rail item between Logs and Settings:
```html
<a class="rail-item" data-page="reliability"><svg ...></svg><span>Reliability</span></a>
```
Add the page section (mirror another `<section class="page" id="page-...">`):
```html
<section class="page" id="page-reliability">
  <div class="page-head">
    <div><h1 class="page-title">Reliability</h1>
      <span class="page-sub">How often tools and routines actually work.</span></div>
    <div class="row-inline">
      <select id="relRange"><option value="7">7d</option><option value="30" selected>30d</option>
        <option value="90">90d</option></select>
      <button class="btn btn-sm" id="relRefresh">Refresh</button>
    </div>
  </div>
  <div id="relDisabled" class="empty" style="display:none;">Reliability is off — enable it in Settings.</div>
  <div id="relScore" class="grid grid-3"></div>
  <div class="card"><div class="card-head"><span class="card-title">Tools</span></div>
    <div id="relTools"></div></div>
  <div class="grid grid-2">
    <div class="card"><div class="card-head"><span class="card-title">Routines</span></div>
      <div id="relRoutines"></div></div>
    <div class="card"><div class="card-head"><span class="card-title">Loop health</span></div>
      <div id="relLoop"></div></div>
  </div>
</section>
```

- [ ] **Step 2: Implement fetch + render + sparkline (app.js)**

Add a `loadReliability()` invoked when the router switches to `data-page="reliability"` (wire it in
the same place `connectLogs()`/`loadNotify()` are dispatched). Include an inline-SVG sparkline helper:

```javascript
  function sparkline(vals, w, h){
    if (!vals || !vals.length) return '';
    var max = 100, min = 0, n = vals.length;
    var pts = vals.map(function(v,i){
      var x = n<2 ? 0 : (i/(n-1))*(w-2)+1;
      var y = h - 1 - ((v-min)/(max-min))*(h-2);
      return x.toFixed(1)+','+y.toFixed(1);
    }).join(' ');
    return '<svg class="spark" width="'+w+'" height="'+h+'" viewBox="0 0 '+w+' '+h+'">'
      + '<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="'+pts+'"/></svg>';
  }
  async function loadReliability(){
    var days = $('relRange').value || '30';
    var s = await (await fetch('/reliability/summary?days='+days)).json();
    if (!s.enabled){ $('relDisabled').style.display=''; $('relScore').innerHTML=''; return; }
    $('relDisabled').style.display='none';
    $('relScore').innerHTML =
      scoreCard('Tool success', s.tool_success_pct==null?'—':s.tool_success_pct+'%', s.tool_calls+' calls')
      + scoreCard('Routine completion', s.routine_completion_pct==null?'—':s.routine_completion_pct+'%', s.routine_runs+' runs')
      + scoreCard('Loop friction', s.friction_events, 'reprompts + parse-fails');
    var tools = await (await fetch('/reliability/tools?days='+days)).json();
    $('relTools').innerHTML = tools.length ? tools.map(toolRow).join('')
      : '<div class="empty">No tool calls recorded yet.</div>';
    var routines = await (await fetch('/reliability/routines?days='+days)).json();
    $('relRoutines').innerHTML = routines.length ? routines.map(function(r){
      return '<div class="rel-line"><span>'+esc(r.entity)+'</span><span>'+r.runs+' runs · '
        + (r.completion_pct==null?'—':r.completion_pct+'%')+'</span></div>'; }).join('')
      : '<div class="empty">No routine runs yet.</div>';
    var loop = await (await fetch('/reliability/loop?days='+days)).json();
    $('relLoop').innerHTML = ['parse_fail','reprompt','validation_fail'].map(function(k){
      var d = loop[k]||{total:0,series:[]};
      return '<div class="rel-line"><span>'+k.replace('_',' ')+'</span><span>'+d.total
        + ' '+sparkline((d.series||[]).map(function(x){return Math.max(0,100-x.n*10);}),80,18)+'</span></div>';
    }).join('');
  }
  function scoreCard(label, big, sub){
    return '<div class="card rel-score"><div class="rel-big">'+esc(String(big))+'</div>'
      + '<div class="rel-label">'+esc(label)+'</div><div class="rel-sub">'+esc(sub)+'</div></div>';
  }
  function toolRow(t){
    var pct = t.success_pct==null?'—':t.success_pct+'%';
    var cls = t.success_pct==null?'':(t.success_pct>=95?'ok':(t.success_pct>=80?'warn':'bad'));
    return '<div class="rel-tool"><span class="rt-name">'+esc(t.entity)+'</span>'
      + '<span class="rt-spark '+cls+'">'+sparkline(t.spark,64,16)+'</span>'
      + '<span class="rt-pct '+cls+'">'+pct+'</span>'
      + '<span class="rt-meta">'+t.calls+' calls'+(t.mean_ms!=null?' · '+t.mean_ms+'ms':'')+'</span>'
      + '<span class="rt-err" title="'+esc(t.last_error||'')+'">'+esc(t.last_error||'')+'</span></div>';
  }
```
Wire `$('relRefresh')` and `$('relRange')` change to call `loadReliability()`.

- [ ] **Step 3: Add styles (styles.css)**

```css
  .rel-score{ text-align:center; padding:14px; }
  .rel-big{ font:600 26px/1 var(--font-mono); color:var(--ink); }
  .rel-label{ font-size:11px; color:var(--muted); margin-top:4px; }
  .rel-sub{ font-size:10.5px; color:var(--faint); }
  .rel-tool{ display:grid; grid-template-columns:1fr auto 56px auto; gap:10px; align-items:center;
    padding:6px 0; border-bottom:1px solid var(--border); font-size:12px; }
  .rt-name{ font-weight:600; color:var(--ink); }
  .rt-pct{ font-family:var(--font-mono); text-align:right; }
  .rt-meta{ color:var(--faint); font-family:var(--font-mono); font-size:11px; }
  .rt-err{ color:var(--danger); font-size:11px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; max-width:200px; }
  .ok{ color:var(--ok); } .warn{ color:var(--amber); } .bad{ color:var(--danger); }
  .rel-line{ display:flex; justify-content:space-between; align-items:center; padding:5px 0;
    font-size:12px; color:var(--muted); border-bottom:1px solid var(--border); }
  .spark{ vertical-align:middle; }
```

- [ ] **Step 4: Verify (node syntax + Playwright smoke)**

Run: `node --check dashboard/app.js`
Expected: OK.

Write `scratchpad/qa/reliability.js` (Playwright, mirror the existing QA scripts): load a locally-run
instance, click `[data-page="reliability"]`, assert `#relScore` renders 3 cards (or the empty/disabled
state on a fresh db), and 0 console errors. Run it against a throwaway local instance (Telegram +
autoextract off). Screenshot `reliability.png`.

- [ ] **Step 5: Commit**

```bash
git add dashboard/index.html dashboard/app.js dashboard/styles.css
git commit -m "feat(reliability): dashboard Reliability page"
```

---

## Self-review checklist (run before implementing)

- **Spec coverage:** collection (Task 1), store+retention (Task 2), mapping+latency (Task 3),
  routine outcome (Task 4), wiring+config+disabled path (Task 5), endpoints+admin gate (Task 6),
  dashboard surface (Task 7). All spec sections covered.
- **Type consistency:** `record(kind, entity, ok, ms, detail, ts)` signature is identical across the
  collector calls, the store impl, and its tests. `summary()` keys (`enabled`, `tool_calls`,
  `tool_success_pct`, `routine_runs`, `routine_completion_pct`, `friction_events`) match between store,
  engine delegate, endpoint, and the dashboard's `scoreCard` reads.
- **No placeholders:** every step has runnable code.
- **Latency decision made:** collector-side `(run_id, step)` pairing (event schema untouched), bounded
  by `max_pending`.

## Execution note

Ships behind `enable_reliability` (default on); it's additive and passive, so it can merge to `main`
without touching existing behavior. Do NOT push the feature branch until the user asks.
