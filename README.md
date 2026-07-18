# Argus

A harness that makes a small (or modest) LLM reliably run multi-step agentic tasks — tools,
skills, memory, scheduling — with a live-trace control dashboard so you can watch, and steer,
every step it takes.

Argus isn't a wrapper around a frontier model's own agentic ability. It's built on the assumption
that the model driving it is small, cheap, or self-hosted, and needs real scaffolding — tight
tool contracts, deterministic execution paths where it matters, and a verifier watching for
over-claiming — to be dependable. Point it at a frontier model over an API and it works great too;
point it at a 3B model on your own GPU and the harness is what keeps it honest.

![Argus dashboard screenshot placeholder](docs/screenshot.png)

## Features

- **Live trace dashboard** ("the Observatory") — watch every tool call, model turn, and decision
  in real time; Console, Automation, Data, Memory, Developer, and Settings views.
- **Tools + agent-created tools** — a library of built-in tools (search, fetch/crawl, weather,
  conversions, time, calculator, and more), plus an experimental `create_tool` that lets the model
  author new tools at runtime inside a soft, AST-gated sandbox with an SSRF egress guard (see
  [SECURITY.md](SECURITY.md)).
- **Skills + deterministic execution** — markdown-defined procedural knowledge layered on top of
  tools; a skill can embed a structured `steps` block that runs deterministically through the
  routines engine instead of relying on free-form generation every time.
- **SQL tables + `ask_data`** — a structured table store with a safe, read-only query/aggregate
  surface (filter, SUM/AVG, GROUP BY, date ranges), plus natural-language-to-SQL question
  answering with schema grounding and self-repair.
- **`exec_python`** — a sandboxed Python REPL for one-off computation, sharing the same soft
  sandbox as created tools, with a persistent per-session namespace.
- **Memory** — persistent facts about the user, with keyword or semantic (embedding-based) recall,
  auto-extraction from conversation, and configurable global (cross-interface) or per-session
  scoping.
- **Routines and scheduling** — named, ordered multi-step sequences runnable on command or on a
  schedule, so a recurring task's plan stays pinned instead of being re-derived every time.
- **Watches** — poll a URL or feed and get alerted when it changes, with a model-written summary
  of what's new.
- **Knowledge base (RAG)** — add documents/notes to an embedded chunk store and search them by
  meaning, not just keyword.
- **Telegram + email/push** — talk to Argus from Telegram, and let it reach you (the owner) via
  SMTP email or [ntfy](https://ntfy.sh) push for scheduled results and watch alerts.
- **Multi-model roles** — separate model connections for chat vs. embeddings, targeting
  OpenRouter, any OpenAI-compatible API, or a local vLLM/Ollama server.

## Quickstart

**macOS / Linux:**

```
curl -fsSL https://raw.githubusercontent.com/apollo-orbit-dev/argus-agent/main/install.sh | bash
```

**Windows (PowerShell):**

```powershell
irm https://raw.githubusercontent.com/apollo-orbit-dev/argus-agent/main/install.ps1 | iex
```

The installer clones the repo, creates a virtualenv, installs Argus, and copies `.env.example` to
`.env`. Add your model API key, then:

```
cd argus
source .venv/bin/activate       # Windows: .venv\Scripts\Activate.ps1
argus start
```

Open http://localhost:8700.

> **Optional extras** (imported lazily — the base install runs fine without them): PDF export
> (`pip install -e ".[pdf]"`, needs native GTK/Pango/cairo) and OCR of scanned PDFs
> (`pip install -e ".[ocr]"`, needs the Tesseract binary). Skip unless you want those features.

### Manual install

```bash
git clone https://github.com/apollo-orbit-dev/argus-agent
cd argus
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
cp .env.example .env
# edit .env: set MODEL_BASE_URL / MODEL_NAME / MODEL_API_KEY (OpenRouter is the easy default)
argus start
```

Then open http://localhost:8700.

## CLI

The `argus` command is installed as a console entry point (`pip install -e .`) and manages the
server as a background process (pidfile + port based; cross-platform):

| Command         | What it does                                             |
|-----------------|----------------------------------------------------------|
| `argus start`   | start the server in the background                       |
| `argus stop`    | stop the running server (`exit` is an alias)             |
| `argus restart` | stop then start                                          |
| `argus status`  | show whether it's running                                |
| `argus logs`    | tail the server log (Ctrl-C to quit)                     |
| `argus run`     | run in the foreground (Ctrl-C to quit)                   |
| `argus version` | print the installed version                              |

## Configuration

All configuration is environment-driven — see [`.env.example`](.env.example) for the full list of
settings, grouped by area, with comments explaining each one: the model connection, feature flags
for every built-in tool, memory/embedding settings, Telegram, notifications (SMTP/ntfy), the
scheduler, and admin/security options. Copy it to `.env` and edit; every setting has a sane
default in `config.py` if you leave it out.

## Models

Argus defaults to working against **OpenRouter** — set `MODEL_API_KEY` to an OpenRouter key,
`MODEL_BASE_URL=https://openrouter.ai/api/v1`, and `MODEL_NAME` to any model you have access to
(e.g. `anthropic/claude-sonnet-4.5`), and you're running. Any OpenAI-compatible endpoint works the
same way (set `MODEL_PROVIDER=openai` and point at `api.openai.com` or a compatible provider).

For a fully private/local setup, point `MODEL_BASE_URL` at your own **Ollama** or **vLLM** server
(`MODEL_PROVIDER=vllm`) — this is what Argus was originally built and tuned against for small
self-hosted models. Memory's semantic recall similarly accepts any OpenAI-compatible
`/embeddings` endpoint via `EMBEDDING_BASE_URL`.

## Requirements

- Python 3.11+
- (optional, for full document/OCR support) a system install of `tesseract-ocr`

## Security

Read [SECURITY.md](SECURITY.md) before enabling agent-created tools/`exec_python`, or before
exposing the dashboard beyond localhost/a trusted LAN — the sandbox is language-level, not a
container, and the dashboard has no built-in auth beyond an optional admin token.

## License

[MIT](LICENSE)
