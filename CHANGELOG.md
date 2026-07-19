# Changelog

All notable changes to this project are documented here.

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
