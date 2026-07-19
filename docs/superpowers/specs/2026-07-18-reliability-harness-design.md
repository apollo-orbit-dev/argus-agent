# Reliability Harness — Design Spec

**Status:** design approved (brainstorm 2026-07-18); ready for implementation planning.

**Goal:** Give Argus a passive, always-on instrument that turns "is my agent actually reliable?"
into a number — per-tool success rates, routine completion, and small-model loop-health — surfaced
on a new dashboard **Reliability** page. Zero extra model cost; observes signal that already flows.

**Thesis fit:** Argus's whole premise is *reliability on a small model*. Today there is no
instrument measuring it. This is the missing gauge.

**Tech stack:** Python (stdlib `sqlite3`), FastAPI (existing backend), the existing static dashboard
(index.html / app.js / styles.css), the existing `EventBus`/`StepEvent` stream.

## Global constraints

- **No model cost.** The harness only *observes* events already emitted during normal runs. It never
  makes a model call, runs a probe task, or re-executes a tool. (Active probes are a later branch.)
- **No hot-path latency.** Recording an outcome is a synchronous SQLite write of a few integers/short
  strings; it must not block or slow a run. Failures in the collector must never break a run
  (swallow-and-log).
- **Dashboard-only surface.** No agent-facing `reliability` tool this branch. No alerts, no
  self-adapt. (All deferred — see Out of Scope.)
- **Its own store.** A dedicated `reliability.db`, mirroring how `memory.db` / `tables.db` /
  `knowledge.db` are separate SQLite files. Gitignored like the other `*.db`.
- **Config-gated.** A `enable_reliability` flag (default `true`); when off, the sink is not
  registered and the page shows a disabled state.

## Architecture overview

```
run_task / routine executor / engine
        │  (already emits StepEvents)
        ▼
   EventBus.publish(ev) ──► existing history + SSE subscribers   (unchanged)
        │
        └─► NEW: sinks[]  ──► ReliabilityCollector.record(ev)
                                     │  maps event → outcome
                                     ▼
                             ReliabilityStore (reliability.db)
                               • outcomes  (raw rows, 30-day retention)
                               • daily      (incremental rollups, kept forever)
                                     ▲
                                     │  aggregate queries
                          Backend /reliability/* endpoints
                                     ▲
                                     │  fetch
                        Dashboard "Reliability" page (rail item)
```

Five units, each independently testable:

1. **`EventBus` sink hook** — a generic fan-out point.
2. **`ReliabilityCollector`** — event → outcome mapping (pure, plus latency pairing).
3. **`ReliabilityStore`** — schema, writes, retention, aggregate queries.
4. **Backend endpoints** — read-only aggregation surface.
5. **Dashboard page** — the surface.

---

## 1. EventBus sink hook

Add a synchronous sink list to `EventBus`, kept separate from SSE subscribers so the reliability
concern never touches the streaming path.

**Interface (engine/events.py):**
- `add_sink(fn: Callable[[StepEvent], None]) -> None` — register a listener called on every publish.
- In `publish(ev)`, after the existing history/log/subscriber fan-out, call each sink inside a
  `try/except` that logs and swallows (a sink must never break a run or the stream).

Sinks are synchronous and cheap (a SQLite write). The collector owns its own connection; no `await`.

**Why here:** every `StepEvent` from `run_task`, the routine executor, and engine-level `emit()`
funnels through `publish()`. One hook = uniform coverage of tools, routines, and loop-health with no
per-call-site instrumentation.

**Consumes:** nothing. **Produces:** `add_sink()` used by the engine at startup.

---

## 2. ReliabilityCollector

Registered as a sink at engine construction (when `enable_reliability`). Maps each relevant
`StepEvent` to an outcome row and writes it via the store. Ignores irrelevant kinds
(`info`, `model_request`, `model_response`).

**Event → outcome mapping:**

| StepEvent | Recorded as | Fields |
|---|---|---|
| `tool_result` | tool execution outcome | entity=`data.tool`, ok=`data.ok`, ms=(see latency), detail=short error if `!ok` |
| `validation` (ok=False) | model arg-validation failure | entity=`data.tool`, kind=`validation_fail`, detail=`data.error` |
| `reprompt` | loop friction | kind=`reprompt`, detail=`data.reason` |
| `error` (`data.kind=="parse_failure"`) | loop friction | kind=`parse_fail`, detail=`data.reason` |
| `routine_result` (NEW event, see below) | routine outcome | entity=`data.name`, ok=`data.ok`, ms=`data.ms`, detail=`data.error` / delivery |

**Latency pairing:** `tool_call` and `tool_result` are separate events. The collector keeps a tiny
in-memory map `(run_id, step) -> tool_call.ts`; on the matching `tool_result` it computes
`ms = int((result.ts - call.ts)*1000)` and clears the entry. The map is bounded (entries dropped on
`final`/`error` for that run, and a hard cap of N pending) so it can't leak.
*(Alternative considered: add `ms` to the `tool_result` event in the loop — simpler, avoids collector
state. Chosen the pairing approach to keep the event schema untouched; the plan may revisit if
pairing proves fiddly.)*

**New event — `routine_result`:** the one outcome not currently observable as a clean event. The
`RoutineExecutor.run()` (and `_scheduled_routine_run`) already computes `RoutineResult{ok, delivered,
delivery_error, output, steps}`. Emit a `StepEvent(kind="routine_result", data={name, ok, delivered,
delivery_error, ms, steps_ok, steps_total})` at the end of a routine run. Minimal, generally useful,
and keeps the collector purely event-driven.

**Metric semantics (so the score isn't hand-wavy):**
- A tool call is a **success** when its `tool_result.ok` is true. A tool that ran fine but returned a
  `no-data` / `CANNOT` sentinel is `ok=true` — an *honest* outcome, counted as success (the
  anti-fabrication path working, not a failure).
- A **validation failure** (`validation.ok=false`) is a *model* failure (bad args), tracked
  separately from tool *execution* failure — one measures the small model, the other the tool.
- A routine **completes** when `routine_result.ok` is true (reached its output step).
- **Loop-health** = per-run rates of parse failures, reprompts, and validation failures — the most
  direct measure of the small model driving the harness cleanly.

**Consumes:** `StepEvent`. **Produces:** writes to `ReliabilityStore`. Pure mapping logic is unit-
testable without a DB (feed events, assert emitted outcome dicts).

---

## 3. ReliabilityStore (reliability.db)

Two tables. Raw rows give drill-down within a window; incremental daily rollups give cheap
forever-trends without a cron job.

**`outcomes`** (raw, bounded retention ~30 days):
```
id INTEGER PK, ts REAL, day TEXT,           -- day = 'YYYY-MM-DD' (local)
kind TEXT,                                    -- tool | validation_fail | reprompt | parse_fail | routine
entity TEXT,                                  -- tool/routine name; '' for loop-health
ok INTEGER,                                   -- 1 | 0 | NULL (loop-health has no ok)
ms INTEGER,                                    -- latency; NULL when N/A
detail TEXT                                    -- short error/reason snippet, capped ~200 chars
```
Indexed on `(day)` and `(entity, day)`. Retention: on write (throttled, e.g. once/hour) delete
`outcomes` older than `reliability_raw_retention_days` (default 30).

**`daily`** (rollups, kept forever), upserted incrementally on every recorded outcome:
```
day TEXT, kind TEXT, entity TEXT,
calls INTEGER, ok_count INTEGER, err_count INTEGER,
sum_ms INTEGER, cnt_ms INTEGER,               -- mean latency = sum_ms/cnt_ms
PRIMARY KEY (day, kind, entity)
```
Incremental upsert (`INSERT ... ON CONFLICT DO UPDATE SET calls=calls+1, ...`) means no nightly job
and O(1) writes. Percentiles (p50/p95) are computed from `outcomes` on demand (drill-down only);
the top-line and trends use `daily` means.

**Interface (engine/reliability/store.py):**
- `record(kind, entity, ok, ms, detail, ts)` — one raw insert + one daily upsert (single tx).
- `summary(days=30) -> dict` — top-line: overall tool success %, total calls, worst tools, routine
  completion %, loop-health rates.
- `per_tool(days=30) -> list` — per tool: calls, success %, mean ms, last error, a per-day success
  sparkline series.
- `per_routine(days=30) -> list` — per routine: runs, completion %, last outcome.
- `loop_health(days=30) -> dict` — daily series for parse-fail / reprompt / validation-fail rates.
- `recent_failures(entity=None, limit=20) -> list` — raw drill-down from `outcomes`.

Reads use a read-only connection; writes a separate connection (same pattern as `TableStore`).

**Consumes:** outcome tuples. **Produces:** aggregate dicts for the backend.

---

## 4. Backend endpoints (backend/app.py)

Read-only, admin-gated (same `_require_admin` as `/logs`), thin wrappers over the store:
- `GET /reliability/summary?days=30`
- `GET /reliability/tools?days=30`
- `GET /reliability/routines?days=30`
- `GET /reliability/loop?days=30`
- `GET /reliability/failures?entity=&limit=20`

All return JSON. `engine.reliability_*` methods delegate to the store. When `enable_reliability` is
false, endpoints return `{ "enabled": false }` and the page renders a disabled state.

---

## 5. Dashboard "Reliability" page

New rail item **Reliability** (between Logs and Settings), `data-page="reliability"`, `#page-reliability`.
Observatory tokens, matching the existing pages. Fetches on nav-in; a manual refresh + a day-range
selector (7 / 30 / 90).

Layout (top to bottom):
- **Top-line score card** — big number: 30-day tool success % (e.g. `98.2%`), with total calls and a
  delta vs the prior period. Secondary: routine completion %, and a loop-health "friction" figure
  (reprompts + parse-fails per 100 runs).
- **Tools table** — one row per tool: name, calls, success % (with a small colored bar), mean
  latency, last error (hover for full), and a per-day success **sparkline**. Sorted worst-first so
  flaky tools surface. Click a row → drill-down of that tool's recent failures (`/reliability/failures`).
- **Routines** — compact list: name, runs, completion %, last outcome (✓/✗ + delivered).
- **Loop-health strip** — three tiny sparklines (parse-failure / reprompt / validation-failure rate
  over time) — the small-model-driving-cleanly gauge.

Empty state (no data yet): "No runs recorded yet — run a task and this fills in." Disabled state
(flag off): a note pointing at the Settings toggle.

Sparklines: inline SVG (no dependency), same self-contained approach as the rest of the dashboard.

**Consumes:** the `/reliability/*` endpoints. **Produces:** the surface.

---

## Data flow (worked example)

1. User runs "what's the weather in Atlanta". Loop emits `tool_call{weather}` → `validation{ok}` →
   `tool_result{weather, ok:true}` → `final`.
2. `publish()` fans each to SSE (unchanged) **and** to `ReliabilityCollector`.
3. Collector: on `tool_call` stores `(run,step)->ts`; on `tool_result` computes `ms`, calls
   `store.record("tool", "weather", ok=1, ms=..., detail="")`.
4. Store: inserts one `outcomes` row + upserts `daily(today,"tool","weather")` counters.
5. Later, the Reliability page calls `/reliability/tools?days=30` → `store.per_tool()` → weather shows
   `N calls · 100% · 420ms mean` with a sparkline.

## Out of scope (explicitly deferred to later branches)

- **Alerts** — notifying when a success rate drops (needs a threshold model; Notifier already exists).
- **Self-adapt** — flagging a flaky tool to the model, or auto-quarantining a created tool whose test
  fails.
- **Active probe suite** — curated tasks with expected outputs, run on a schedule; costs model tokens.
- **Proactively re-running created-tool baked-in tests** — semi-active; a natural follow-on.
- **Agent-facing `reliability` query tool** — dashboard-only this branch (user decision).
- **Per-session / per-profile attribution** — single global instrument for now.

## Testing strategy

- **Collector mapping (unit, no DB):** feed synthetic `StepEvent`s → assert the outcome tuples
  (tool ok/err, validation_fail, reprompt, parse_fail, routine_result, latency pairing, no-data still
  counts as success).
- **Store (unit, tmp db):** `record()` then `summary()`/`per_tool()`/`per_routine()`/`loop_health()`
  return correct counts, means, and success %; retention prune deletes old raw rows but keeps `daily`;
  incremental rollup matches a recomputed-from-raw baseline.
- **Sink safety:** a sink that raises does not break `publish()` (the event still reaches history +
  subscribers).
- **Endpoints (TestClient):** each `/reliability/*` returns the expected shape; disabled state when
  the flag is off; admin gate enforced.
- **No live model calls in any test** (matches the repo's offline-test constraint).

## Open questions / risks

- **Latency pairing vs event `ms`:** if the collector-side `(run,step)->ts` map proves fiddly under
  concurrent runs, fall back to adding `ms` to the `tool_result` event (simpler, tiny schema change).
  The plan should pick one early.
- **Retention default (30 days raw):** fine for a self-hosted single user; revisit if `outcomes`
  grows large under heavy use (rollups are unbounded but tiny — one row per day per entity).
- **"Success" for tools that return soft errors in `ok=true` text:** we deliberately count `ok=true`
  as success even when the content is a `no-data`/`CANNOT` sentinel. If that hides real breakage,
  a later refinement can classify sentinel outcomes as a third "empty" bucket (not this branch).
