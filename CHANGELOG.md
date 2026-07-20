# Changelog

All notable changes to this project are documented here.

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
