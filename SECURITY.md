# Security

Argus is designed to run on localhost or a trusted LAN, under the control of the person who
configures it. Read this before turning on the experimental features or exposing it beyond your
own machine.

## The sandbox is soft, not a container

`create_tool` (agent-authored tools) and `exec_python` (the code interpreter) run model-written
Python through a **language-level sandbox**: an AST scan that blocks dunder access,
`open`/`exec`/`eval`/`getattr`/etc., a restricted import allowlist, a curated builtins set, an
SSRF-guarded network stand-in, and a wall-clock timeout. This is **not** a container, VM, gVisor,
or seccomp jail — it relies entirely on the Python-level checks holding. Treat any code these
tools produce or run as **semi-trusted**, not fully isolated.

Practical guidance:

- Both features are **off by default** (`ENABLE_TOOL_CREATION=false`, `ENABLE_CODE_INTERPRETER=false`).
  Only turn them on if you understand and accept this model.
- `ENABLE_TRUSTED_TOOLS` goes further: it lets a tool that needs a restricted capability (`os`,
  `sqlite3`, `open`, `subprocess`, ...) run **outside** the sandbox entirely — but only after a
  human reads the generated code and approves it in the dashboard. Never approve code you haven't
  actually read.
- `ENABLE_DEP_APPROVAL` gates pip installs triggered by a created tool's imports behind an
  explicit human approve/deny step (dashboard or Telegram `/approve`/`/deny`). Don't approve a
  package you don't recognize.
- `TOOL_SECRET_NAMES` is the only way created-tool code can see environment secrets (the sandbox
  blocks `import os`). Only list names you're comfortable a semi-trusted tool can read.

## Admin token

Mutating and admin endpoints (config view/edit/save, restart, system-prompt edit, dependency and
trusted-tool approvals) are **unprotected by default** to match a simple, open local dashboard.
Set `ADMIN_TOKEN` in `.env` to require an `X-Admin-Token` header on those endpoints. Do this before
putting Argus anywhere reachable by anyone other than you.

## Don't expose the dashboard to the public internet

The dashboard (and its API) is built for localhost or a trusted LAN — there is no built-in user
system, rate limiting, or CSRF protection beyond the admin token. If you need remote access:

- Put it behind a reverse proxy (nginx/Caddy) with authentication (e.g. basic auth, an SSO proxy)
  and HTTPS, or tunnel over a private network (Tailscale/WireGuard) instead of opening a port.
- Set `ADMIN_TOKEN`.
- Set `SSL_CERTFILE`/`SSL_KEYFILE` if you do terminate TLS at Argus itself (see `.env.example` for
  how to generate a self-signed cert), though a reverse proxy is the more common setup.

## Secrets

All credentials (model API keys, Telegram bot token, SMTP password, ntfy topic, tool secrets) live
in `.env`, which is gitignored and never committed. `model_presets.json`, `trusted_tools.json`, and
`dep_approvals.json` can also carry sensitive data (stored connections, approved code hashes) and
are gitignored too. Don't paste real secrets into `.env.example`, issues, or pull requests.

## Reporting a vulnerability

This is a small personal/hobby project without a dedicated security team. If you find a
vulnerability, please open a private report (GitHub's "Report a vulnerability" under the Security
tab) rather than a public issue, and give a reasonable amount of time to respond before public
disclosure.
