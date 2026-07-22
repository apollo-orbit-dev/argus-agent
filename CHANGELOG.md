# Changelog

All notable changes to this project are documented here.

## Unreleased

### Added
- **README: "Small-model scaffolding"** — a section naming the layers that exist specifically to make
  a small model dependable (the loop-health Observer, switchable tool-calling modes, deterministic
  skill steps, explicit-first skill selection, tight tool contracts, the post-action verifier,
  `clarify`, the reliability instrument, standing rules + memory) and the failure each one counters.
- **README: "Measuring it"** — documents the two eval harnesses and how to run them: the
  cross-model capability benchmark (`python -m engine.eval.benchmark`) with the founding run's
  numbers, and the pass^k skill A/B (`scripts/skill_eval.py`) with its KEEP / no-lift / REGRESSION
  and over-fire verdicts.
- **The skill-eval harness now ships.** `scripts/skill_eval.py` and the A/B batteries + fixtures
  under `docs/ab/` were previously gitignored, leaving `engine/eval/` public with no entry point.
  `.gitignore` now un-ignores exactly those (the personal deploy/probe scripts and the local A/B
  reports stay out).

## 0.8.1

### Added
- **Durable run traces** — the tool/step trace behind the Runs card now persists to a SQLite-backed
  `events.db` sink when `enable_trace_persistence` is on (default), so runs survive a server restart
  instead of vanishing with process memory. `model_request` events are excluded from the sink to keep
  it lean. Retention is config-driven via `trace_retention_mode` (`age+runs` / `age` / `runs` / `off`),
  `trace_retention_days`, and `trace_keep_runs_per_session`; `/status` now reports `trace_persistence`
  so clients can tell whether the current process has it wired up.
- **Dashboard: trace-persistence controls** — a new "Run trace persistence" card on the **Settings**
  page (under Runtime limits) exposes the on/off switch (labelled as applying on restart, since the
  sink registers at startup), the retention-mode select, and the retention-days / keep-runs number fields,
  all reflecting from and PATCHing `/config` like the other runtime toggles. The Runs card's
  empty-state copy is now conditional on `/status`'s `trace_persistence`: "No runs yet" when
  persistence is on (runs really do survive a restart), the existing "not kept across a restart"
  wording when it's off.

## 0.8.0

### Added
- **Durable sessions** — conversations now persist to a SQLite-backed `SessionStore` (raw message
  log + working set) instead of living only in process memory, so a restart no longer discards
  history. New `/sessions` endpoints back it: `GET /sessions` (list, with per-session message
  counts), `POST /sessions` / `PATCH /sessions/{id}` / `DELETE /sessions/{id}` (admin-gated
  create/rename/delete), and `GET /sessions/{id}/messages` (paginated transcript read). Ephemeral
  (`__`-prefixed) sessions are excluded from the store and never appear in the list.
- **Dashboard Sessions sidebar** — the Console page now has a sessions rail (left of Runs) to
  create, switch, rename, and delete durable sessions. Switching a session re-subscribes the live
  trace (`/events?session_id=`) and reloads that session's persisted transcript into the trace
  viewer, so the runs list, live trace, and history all scope to whichever session is selected;
  the active session persists across reloads via `localStorage`. The implicit `"dashboard"`
  session remains the default, and it's the fallback if the active session is deleted.
- **Session transcript view** — the conversation is now a first-class **Transcript card** stacked
  above the Runs card; it's the default view, and a new turn no longer clobbers it (the run streams
  in the Runs card and the transcript refreshes when the turn completes). Click a run to drill into
  its tool-trace, click Transcript to return. The transcript renders as a **chat/messaging view**
  (your messages right, Argus's left, tool output as a muted note), with Argus's replies rendered as
  **Markdown** — bold, lists, tables, code, headings, links — sanitized with DOMPurify. Empty
  tool-call turns are hidden from the view.

## 0.7.5

### Added
- **`evolve_table` skill** — guidance so a small model *reaches for* the table-mutation tools when
  changing a table that already exists: `add_column` in place (not rebuild-and-port), `copy_table` for a
  bulk copy (not a `query` + `insert_row` loop), `rename_column`/`drop_column`/`rename_table` for schema
  changes, and `update_rows` for bulk value changes. Measured A/B on the 3B model (pass^k, Opus quality
  judge): chain-correctness 4/6 → 5/6 and judge 1.83 → 2.61 (Δ+0.78), driven mainly by the add-a-column
  case (0/3 → 3/3), with no over-fire on off-target prompts.

## 0.7.4

### Added
- **Table-mutation tools** — six new validated table tools so in-place schema changes and bulk data
  moves no longer degenerate into hundreds of one-row `insert_row` calls (which could overflow the
  step budget and crash the turn):
  - **`add_column`** — add a column (`name:type`, e.g. `sleep_start:text`) to an existing table
    without recreating it; existing rows get `NULL` there.
  - **`rename_column`**, **`drop_column`**, **`rename_table`** — the rest of the ALTER family.
  - **`copy_table`** — copy rows from one table into another in a single call: it creates the
    destination (mirroring the source's columns, types, and primary key) when it doesn't exist, or
    copies the shared columns into an existing one; an optional `where` filter copies just a subset.
    Whole-table copies run as one server-side statement (no row cap); filtered copies run the filter
    on the read-only connection and then parameterized-insert the results, so no SQL fragment ever
    touches a write path.
  - **`update_rows`** — set columns on every row matching an equality filter, fully parameterized;
    an empty match (which would rewrite the whole table) is refused.
  The four destructive operations (`drop_column`, `rename_column`, `rename_table`, `update_rows`)
  default to **Ask** in the per-tool approval matrix; `add_column`/`copy_table` default to Allow.

## 0.7.3

Internal/testbed release — a new developer instrument, no user-facing behavior change.

### Added
- **Model-capability benchmark** (`python -m engine.eval.benchmark`) — a committed, reproducible
  instrument that runs a frozen, difficulty-graded task battery per model under the standard config,
  scores each task (deterministic tool-chain + a model-graded quality judge), accumulates labeled JSON
  results, and plots a per-tier metric-vs-model-size curve — for measuring how well Argus performs as
  the driving model shrinks (finding the small-model "capability shelf"). The founding run (a ~35B vs a
  3B) shows the shelf is difficulty-dependent: small models hold on trivial single-tool tasks and fall
  off on structured/multi-step ones.

## 0.7.2

### Added
- **Update-available indicator** — the dashboard now checks whether a newer release is published on
  GitHub and shows an "↑ vX.Y.Z" badge next to the version in the footer (backed by a `/updates`
  endpoint; cached, and it degrades silently if GitHub is unreachable).
- **Clarification choice buttons** — when the agent asks a clarifying question with options
  (`ask_user` with `options`), the dashboard renders them as one-tap buttons; clicking one sends it as
  your next message instead of making you type it.

### Changed
- **Install scripts pin to the latest release** — `install.sh` / `install.ps1` now check out the latest
  release tag after cloning, rather than landing on the moving `main` branch, so a fresh install is a
  stable versioned release.

## 0.7.1

### Changed
- **`design_table` skill acts instead of interrogating** — on a clear-enough request ("track my daily
  coffee in a table") it now infers a sensible schema and builds the table, rather than stopping to ask
  the user how to structure it; a focused clarifying question is reserved for genuinely ambiguous
  requests. (Measured: the previously over-asking cases now build a well-designed table, with schema
  quality held.)

### Added
- **`native_finish` on the dashboard tool-calling-mode toggle** — the mode is now selectable in the UI
  (`native` / `manual` / `finish`), not just via `.env`/API.

## 0.7.0

### Added
- **`native_finish` tool-calling mode** (opt-in, `TOOL_CALLING_MODE=native_finish`) — native
  tool-calling with `tool_choice=required` plus a synthetic `final_answer` tool, so the model must emit
  a structured tool-or-finish decision every turn. This makes plain-prose "slips" impossible and lets a
  guided-decoding backend (vLLM) produce valid tool-call JSON, while keeping server-side parsing. A
  third option alongside `native` (default) and `manual`; `chat()` now accepts a `tool_choice` param
  (defaults to `auto`, unchanged for the other modes).

## 0.6.1

Internal/testbed release — no user-facing behavior change.

### Added
- **Model-graded judge** (`engine/eval/judge.py`) for the skill-eval harness — a pure prompt-builder +
  reply-parser that scores a run's output QUALITY (0–3) against per-case rubric criteria, complementing
  the deterministic chain-scorer (which can't see, e.g., a correct clarifying question or a
  chain-passing-but-low-quality result). Judge-model-agnostic; the developer runner grades on-target
  cases via the local model or `claude -p` (Opus). Unit-tested and blind to arm/skill.

## 0.6.0

Internal/testbed release — no user-facing behavior change.

### Changed
- **Full store isolation via `data_dir`** — every persistent store (tables, memory, knowledge,
  workspace, artifacts, created tools/skills, watches, routines, scheduled jobs, model presets, …) now
  resolves through the engine's `data_dir` argument instead of hardcoding the project root. Production
  is byte-identical (the default is the project root); passing a `data_dir` isolates an entire Engine
  in-process. Fixes latent test pollution and is the foundation for the skill-eval harness.

### Added
- **Deterministic skill-eval scorer** (`engine/eval/scoring.py`) — a pure, chain-based scorer
  (`tools_in_order` / `min_counts` / `activates` / `skill_not` / `schema_has`) used by the internal
  `pass^k` A/B harness that validates skills across models. (The harness runner itself is developer
  tooling and ships outside the package.)

## 0.5.0

### Added
- **Structured-data skills** — two skills that teach a small model to work with tables well, the first
  of the skills-led push (skills as the guidance layer that gets more out of the existing tools):
  - **`design_table`** — designs a sound schema *before* creating a table: real column types (not
    all-text), a `json` column for list/nested fields (ingredients, tags, line-items), the embed-vs-
    split judgment, and a primary key for natural ids. Fixes the small-model default of flat, all-`TEXT`
    tables with lists buried in text blobs.
  - **`extract_to_table`** — pulls records out of a document, file, or pasted text into a queryable
    table: reads the source with the right tool (`read_document` incl. OCR, `read_file`, or
    `download_file` for a document URL), designs a typed schema, then inserts one row per record —
    instead of returning the content as prose or one giant text column.
- **`json` / `list` column-type alias** for `create_table` — a list or nested field can be declared
  `field:json` (stored as JSON text, queryable with `json_extract`), so the schema self-documents the
  intent. Additive: existing schemas are unaffected.

## 0.4.0

### Added
- **Interactive blocking approvals** — sensitive agent actions now *pause the turn and wait* for a
  human decision instead of proceeding unattended. Each gated action has a visible per-action policy
  (**Allow / Ask / Deny**) you set from the Developer page, and when a policy is *Ask* the action
  blocks for a configurable window (`APPROVAL_WINDOW_SECONDS`, default 60): decide in time and the
  same turn resumes seamlessly; miss the window and it becomes a pending item you can approve later
  (which resumes the work). Prompts appear as inline **Approve / Deny** buttons in the dashboard live
  trace or as Telegram inline buttons, on whichever channel started the turn. **Every tool** has an
  Allow / Ask / Deny toggle on the Developer page — most default Allow and run exactly as before,
  while the sensitive ones (dependency installs, SOUL edits, `exec_python`, `forget`, `delete_row`)
  default Ask. Enforcement is a single check in the loop before any tool runs: Allow runs it, Deny
  refuses it (and Argus adapts), Ask pauses for your decision. Gated by `ENABLE_INTERACTIVE_APPROVALS`
  (on by default); off restores the previous record-and-continue behavior exactly.
- **More calculator functions** — `calculator` now supports `sqrt`, `cbrt`, `pow`, `abs`, `round`,
  `min`, `max`, `floor`, `ceil`, `trunc`, `exp`, `log`/`log2`/`log10`, the trig functions, `hypot`,
  `degrees`/`radians`, and the constants `pi`, `e`, `tau` — still evaluated through the safe
  AST whitelist (no `eval`), with the same runaway-exponent guard applied to `pow`.

### Added
- **Standing behavioral rules** — a durable, owner-managed set of "how to behave" directives
  ("always confirm before deleting", "never use emoji") that persist across sessions. Enabled rules
  are injected into every turn as a distinct "Standing instructions from your owner" block (separate
  from factual memory and from persona/SOUL). Rules can be captured three ways: the agent auto-drafts
  them from owner corrections (a background, cue-gated aux-model pass — "don't do that again" survives
  the session and the owner is notified with an undo hint), the model saves them explicitly
  (`save_rule` / `list_rules` / `remove_rule` tools), or they're managed on a new dashboard **Rules**
  page (add / enable-disable / delete, admin-gated). Backed by a small `rules.json` state file.
  Gated by `ENABLE_RULES` (on) and `ENABLE_RULES_AUTODETECT` (on).

## 0.2.1

### Fixed
- **Reliability metric honesty** — the Reliability page counted a tool call as a success whenever it
  didn't raise, but most tools catch their own errors and *return* an error string (so `ok` stays
  true). Tools that returned `"Error fetching…"` every call were scored 100%. Error-shaped results
  (`Error…` / `Traceback` / `looks WRONG` / fetch+parse errors) are now counted as failures; honest
  `no-data` / `CANNOT` outcomes still count as successes.
- Dashboard: the delete (`✕`) button on created-tool and created-skill rows was dropping to its own
  line; it's now inline on the right of each row.

## 0.2.0

### Added
- **Reliability harness** — a passive, always-on instrument that records tool, routine, and
  loop-health outcomes from the existing event stream into a dedicated `reliability.db`, surfaced on
  a new dashboard **Reliability** page: a top-line tool-success score, a worst-first per-tool table
  (success %, latency, sparkline, last-error drill-down), routine completion, and a loop-health strip
  (parse-failure / reprompt / validation-failure rates). Costs no model calls — it only observes.
  Gated by `ENABLE_RELIABILITY` (on by default).

### Fixed
- Scheduling tools (`list_scheduled_tasks`, `cancel_scheduled_task`, `update_scheduled_task`) are now
  **owner-wide** instead of session-scoped — a task created from Telegram is visible and manageable
  from the dashboard and vice-versa (Argus is single-user with global identity). Jobs still remember
  their origin session for delivery.
- `GET /version` (and the FastAPI app version) now derive from `pyproject.toml` via a single source,
  so the reported version can no longer drift from the package version.

## 0.1.0

Initial public release.

- Agent loop with native and manual tool-calling modes (A/B configurable via `TOOL_CALLING_MODE`).
- Live trace dashboard (the "Observatory") with Console, Automation, Data, Memory, Developer, and
  Settings views.
- Built-in tool library: calculator, unit/currency conversion, weather, geocoding, dictionary,
  Wikipedia, crypto price, time tools, web search (SearXNG) and page fetch/crawl (Firecrawl).
- Agent-created tools (`create_tool`) and a code interpreter (`exec_python`) behind a soft,
  language-level sandbox (AST-gated restricted exec + SSRF egress guard), gated by feature flags
  and off by default.
- Approval-gated dependency installs for created tools, and an opt-in trusted-tool tier for
  human-approved unsandboxed code.
- Skills system: markdown-defined procedural knowledge on top of tools, with an optional
  deterministic `steps` block executed by the routines engine instead of free-form generation.
- Structured data: a SQL-backed table store with a safe read-only query/aggregate surface, plus
  `ask_data` (natural-language question -> SQL -> answer, with schema grounding and self-repair).
- Persistent memory with keyword and semantic (embedding-based) recall, auto-extraction, and
  configurable global/session scoping.
- Routines and a task scheduler for recurring or timed multi-step jobs.
- URL/feed watches with change alerts.
- Knowledge base (RAG) via `add_to_knowledge` / `search_knowledge` over an embedded chunk store.
- Document reader for PDF/DOCX/XLSX, including OCR for scanned PDFs.
- Charts (PNG/SVG) and dependency-free ASCII charts for inline rendering.
- Artifacts (self-contained HTML pages) and PDF export (WeasyPrint) built by the agent.
- Outbound notifications to the owner via Telegram, email (SMTP), and push (ntfy).
- Multi-model roles: separate model connections for chat vs. embeddings; works against OpenRouter,
  OpenAI-compatible APIs, or a local vLLM/Ollama server.
- `argus` CLI: `start`, `stop`, `restart`, `status`, `logs`, `run`, `version`.
