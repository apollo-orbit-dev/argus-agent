"""Single config object for Argus. All values overridable via .env / env vars.

Immutable-ish: patch() returns a validated copy so PATCH /config can live-update
runtime knobs (esp. tool_calling_mode) without a restart and without mutating a
shared object mid-run.
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

ToolCallingMode = Literal["native", "manual"]
SkillSelectionMode = Literal["model_driven", "explicit", "hybrid"]
SemanticRecall = Literal["auto", "on", "off"]
# "global": one shared memory bank keyed on memory_user_id, so facts follow the
# person across every interface (dashboard, Telegram). "session": legacy per-session
# isolation — each conversation (each Telegram chat, the dashboard) has its own memory.
MemoryScope = Literal["global", "session"]


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # allow field named model_* without namespace clash warnings
        protected_namespaces=(),
    )

    # Model (OpenAI-compatible vLLM endpoint).
    # Sampling params are UNSET by default (None) — Argus then omits them from the request and the
    # model server (vLLM) applies its OWN configured defaults. This keeps the model's tuning in one
    # place (the server) instead of hard-coding a copy here that could drift. Set any of these in
    # .env ONLY to deliberately override the server (e.g. force greedy temperature=0 for a test).
    model_base_url: str
    model_name: str
    model_api_key: str = "dummy"
    model_max_tokens: int = 2048
    model_temperature: Optional[float] = None
    model_top_p: Optional[float] = None
    model_top_k: Optional[int] = None
    model_presence_penalty: Optional[float] = None
    # Backend selector. "auto" infers from model_base_url (openrouter.ai -> openrouter,
    # api.openai.com -> openai, else vllm) and gates vLLM-only params. Point model_base_url at
    # https://openrouter.ai/api/v1, set model_api_key to an OpenRouter key, and model_name to an
    # OpenRouter model id (e.g. "anthropic/claude-sonnet-4.5") to test any model you have access to.
    model_provider: str = "auto"
    model_context_window: Optional[int] = None   # for /usage %; falls back to a built-in map
    # Reasoning/thinking control for the MAIN loop, translated per backend: "auto" (model default),
    # "off", "low", "medium", "high". vLLM -> enable_thinking on/off; OpenRouter -> reasoning.effort
    # (or reasoning.enabled=false); OpenAI -> reasoning_effort. Auxiliary calls always force "off".
    model_reasoning: str = "auto"

    # A/B switches
    tool_calling_mode: ToolCallingMode = "native"
    skill_selection_mode: SkillSelectionMode = "hybrid"

    # Tool infrastructure. These are EXTERNAL DEPENDENCIES — empty by default, and the tools that
    # need them are only registered when the URL is set (in .env). So "configured" = the operator
    # actually pointed it at a service; an unset dependency means its tools simply aren't offered
    # (rather than being offered and failing). web_search needs searxng; fetch_page/map_site/
    # crawl_site/extract_data need firecrawl.
    searxng_base_url: str = ""
    firecrawl_base_url: str = ""
    # web_search guards (protect the metered Brave engine behind SearXNG)
    search_cache_ttl: float = 900.0
    search_max_per_min: int = 10

    # Loop / limits
    max_steps: int = 6
    # Auto-compact the conversation once the last turn's context passes this many tokens (0 = off).
    # Caps per-turn cost on a paid model, since every turn re-sends the whole growing conversation.
    auto_compact_tokens: int = 0
    request_timeout: float = 60.0

    # Tool creation (experimental; native mode only). Off by default.
    enable_tool_creation: bool = False
    tool_creation_allow_network: bool = True
    # Wall-clock timeout (seconds) for a single created-tool execution. The old 15s was too
    # tight for tools that make many API calls (e.g. pulling a year of daily data = ~365
    # sequential requests). Raise it for data-heavy tools; lower it to fail runaway tools faster.
    created_tool_timeout: float = 60.0
    # Code interpreter (exec_python): a sandboxed Python REPL for one-off computation, using the
    # SAME soft language-level sandbox as create_tool. Off by default; works in native OR manual
    # tool-calling. Network is off by default (exploratory compute rarely needs it); when on, httpx
    # is available under the SSRF egress guard, same as created tools.
    enable_code_interpreter: bool = False
    code_interpreter_timeout: float = 10.0
    code_interpreter_allow_network: bool = False
    # Approval-gated dependency installs: when a created tool needs a non-stdlib import,
    # file a human approve/deny request instead of hard-failing. On approval the package
    # is pip-installed and allowlisted. Off = disallowed imports hard-fail (legacy). The
    # approval endpoints are admin-gated; the Telegram allowlist is the gate there.
    enable_dep_approval: bool = True
    # Env-var names a created tool may read (comma-separated), exposed to its code as a
    # dict `SECRETS` (the sandbox forbids `import os`, so this is the ONLY way in). Empty =
    # no secrets exposed. e.g. TOOL_SECRET_NAMES=SERVICE_EMAIL,SERVICE_PASSWORD
    tool_secret_names: str = ""
    # Trusted-tool tier: allow model-authored tools that need a RESTRICTED capability (os, sqlite3,
    # open, subprocess, …) to run OUTSIDE the sandbox — but ONLY after a human reads the code and
    # approves it in the dashboard. OFF by default: a stock deploy cannot run unsandboxed code at all.
    enable_trusted_tools: bool = False
    # Adaptive thinking: route each turn to think-on (reasoning) or think-off (fast) by prompt
    # complexity — instant heuristics decide the obvious cases, a cheap classifier resolves the
    # uncertain middle. Biased toward think-ON so it rarely hurts quality. Off by default (so its
    # effect on quality/latency is measurable via probe_gap --adaptive).
    adaptive_thinking: bool = False
    # Cheap post-action verifier: after a turn that CREATED/DELETED/SCHEDULED things, a quick
    # think=False check asks "did you actually complete all of it?" and, if not, makes the agent
    # finish — catching the small model's habit of over-claiming ("deleted them all" after one).
    enable_action_verify: bool = True
    # Skill creation (experimental; native mode only). Safe (data, no code exec).
    enable_skill_creation: bool = False
    # Self-persona editing: the agent can rewrite its OWN SOUL (voice only, not the operational
    # system prompt) via update_soul; previous persona is backed up and revertable. Low-risk.
    enable_soul_editing: bool = True
    # Datastore: vetted SQLite-backed key/value store the agent can save to & query
    # (e.g. daily metrics). Safe — it's a first-class tool, not model-authored code.
    enable_datastore: bool = True
    # Table store: structured tables + a SAFE read-only query/aggregate surface (filter, SUM/AVG,
    # GROUP BY, date ranges) — what KV can't do. Reads run on a read-only SQLite connection. Safe.
    enable_tables: bool = True
    # Artifacts: vetted build_web_page tool — the agent writes self-contained HTML (dashboards,
    # charts, reports) saved to a fixed artifacts/ dir and viewable from the dashboard. Safe.
    enable_artifacts: bool = True
    # PDF: make_pdf renders the agent's HTML (incl. embedded charts) to a PDF in the workspace
    # (WeasyPrint, network-blocked render). Safe.
    enable_pdf: bool = True
    # File workspace: vetted, path-safe files area (write/read/list/delete_file). Safe.
    enable_files: bool = True
    # Document reader: read PDF/docx/xlsx (incl. scanned PDFs via OCR) from the workspace.
    enable_documents: bool = True
    # Knowledge base (RAG): add_to_knowledge / search_knowledge over an embedded chunk store.
    enable_knowledge: bool = True
    # Watcher: poll a URL/feed and alert on change (watch/list_watches/unwatch). Safe.
    enable_watch: bool = True
    # Reliability harness: passive instrument of tool/routine/loop outcomes (dashboard only).
    enable_reliability: bool = True
    reliability_raw_retention_days: int = 30
    # Charts: make_chart renders bar/line/pie/scatter to PNG (view/Telegram) + SVG (embed). Safe.
    enable_charts: bool = True
    # ASCII charts: ascii_chart draws text charts (hbar/vbar/composition/sparkline/line/scatter) that
    # render inline in chat/Telegram/ntfy — no image file. Dependency-free, stateless. Safe.
    enable_ascii_charts: bool = True
    # Routines: named, ordered multi-step sequences (tool + model steps) run on command (run_routine /
    # list_routines) or on schedule; pin the plan for recurring tasks. See docs/routines-spec.md.
    enable_routines: bool = True
    # Outbound notifications (OWNER-ONLY): reach the user off-Telegram via email (SMTP) and/or push
    # (ntfy). Email/push always go to the CONFIGURED owner address/topic — never an arbitrary
    # recipient (the agent notifies YOU, not third parties). Creds are sensitive; kept in .env.
    enable_notify: bool = True
    notify_email_to: str = ""          # owner's email (where notifications are sent)
    notify_email_from: str = ""        # From address (defaults to smtp_user)
    smtp_host: str = ""
    smtp_port: int = 587               # 587 = STARTTLS, 465 = implicit SSL
    smtp_user: str = ""
    smtp_password: str = ""            # app password / SMTP password (plaintext in .env)
    ntfy_topic: str = ""               # ntfy topic to publish to (owner subscribes on their phone)
    ntfy_server: str = "https://ntfy.sh"
    # Background deliveries (scheduled results, watch alerts) ALSO fan out to these channels
    # (comma-separated: "email,ntfy"). Telegram always gets them. Empty = Telegram only.
    notify_fanout: str = ""
    # Task scheduler (agent can schedule/list/cancel timed tasks). On by default.
    enable_scheduler: bool = True
    # Clarify: agent can ask the user a question instead of guessing. On by default.
    enable_clarify: bool = True
    # Observer: detect repeated/no-progress tool calls, nudge then stop. On by default.
    enable_observer: bool = True
    observer_repeat_threshold: int = 2
    # Memory (persistent facts about the user). Semantic recall needs an embedding
    # endpoint; SEMANTIC_RECALL=auto uses it when configured, else keyword recall.
    enable_memory: bool = True
    semantic_recall: SemanticRecall = "auto"
    embedding_base_url: str = ""
    embedding_model: str = ""
    embedding_api_key: str = "dummy"   # so the embedding role can point at a keyed provider
    enable_memory_autoextract: bool = True
    # Behavioral rules: standing rules injected into every turn (context window / cost permitting).
    enable_rules: bool = True             # standing behavioral rules injected into every turn
    enable_rules_autodetect: bool = True  # auto-draft rules from owner corrections (aux model call)
    # Interactive approvals: sensitive actions block the turn for a human OK
    enable_interactive_approvals: bool = True  # sensitive actions block the turn for a human OK
    approval_window_seconds: int = 60          # how long a gate waits before falling back to pending
    # Memory scoping. Default "global": memory is about the *user*, not the interface,
    # so what you tell the dashboard is recalled in Telegram and vice-versa. Set
    # "session" to isolate memory per conversation (e.g. a multi-user Telegram bot).
    memory_scope: MemoryScope = "global"
    memory_user_id: str = "default"

    # Server
    host: str = "0.0.0.0"
    port: int = 8700
    # Where the server's stdout/stderr is captured (the CLI and deploy redirect to this file). The
    # dashboard "Logs" page tails it. Relative paths resolve from the working directory.
    log_file: str = "argus.log"

    # Optional HTTPS: set BOTH to a PEM cert + key and the dashboard serves over TLS. Empty =
    # plain HTTP (the default for a trusted LAN). A self-signed cert lets the browser download
    # .md/PDF files without the "insecure download" nag. Generate one with:
    #   openssl req -x509 -newkey rsa:2048 -nodes -keyout argus-key.pem -out argus-cert.pem \
    #     -days 825 -subj "/CN=argus" -addext "subjectAltName=IP:192.168.1.10"
    ssl_certfile: str = ""
    ssl_keyfile: str = ""

    # Telegram
    telegram_bot_token: str = ""
    allowed_chat_ids: Annotated[list[int], NoDecode] = []

    # Optional protection for sensitive dashboard endpoints (config-file view/edit,
    # save, restart, system-prompt edit). Empty = open (matches the spec's open local
    # dashboard). Set it to require an X-Admin-Token header on those endpoints.
    admin_token: str = ""

    @field_validator("allowed_chat_ids", mode="before")
    @classmethod
    def _parse_chat_ids(cls, v):
        if v is None or v == "":
            return []
        if isinstance(v, str):
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return v

    def patch(self, updates: dict) -> "Config":
        """Return a validated copy with `updates` applied. Raises on invalid values."""
        data = self.model_dump()
        data.update(updates)
        return Config(**data)

    # Fields persisted to .env (scalar config). system_prompt lives in its own file.
    _ENV_FIELDS = (
        "model_base_url", "model_name", "model_api_key", "model_max_tokens",
        "model_temperature", "model_top_p", "model_top_k", "model_presence_penalty",
        "model_provider", "model_context_window", "model_reasoning",
        "tool_calling_mode", "skill_selection_mode",
        "searxng_base_url", "firecrawl_base_url", "search_cache_ttl", "search_max_per_min",
        "max_steps", "auto_compact_tokens", "request_timeout",
        "enable_tool_creation", "tool_creation_allow_network", "created_tool_timeout",
        "enable_code_interpreter", "code_interpreter_timeout", "code_interpreter_allow_network",
        "enable_dep_approval", "tool_secret_names", "enable_trusted_tools", "enable_action_verify",
        "adaptive_thinking",
        "enable_skill_creation", "enable_soul_editing", "enable_datastore", "enable_tables",
        "enable_artifacts", "enable_pdf",
        "enable_files", "enable_documents", "enable_knowledge", "enable_watch", "enable_reliability",
        "reliability_raw_retention_days", "enable_charts",
        "enable_ascii_charts", "enable_routines",
        "enable_notify", "notify_email_to", "notify_email_from", "smtp_host", "smtp_port",
        "smtp_user", "smtp_password", "ntfy_topic", "ntfy_server", "notify_fanout",
        "enable_scheduler", "enable_clarify", "enable_observer", "observer_repeat_threshold",
        "enable_memory", "semantic_recall", "embedding_base_url", "embedding_model", "embedding_api_key",
        "enable_memory_autoextract", "enable_rules", "enable_rules_autodetect", "enable_interactive_approvals", "approval_window_seconds", "memory_scope", "memory_user_id",
        "host", "port", "log_file", "ssl_certfile", "ssl_keyfile",
        "telegram_bot_token", "allowed_chat_ids", "admin_token",
    )

    def env_pairs(self) -> dict[str, str]:
        """Map ENV_KEY -> string value for persistence."""
        out = {}
        for f in self._ENV_FIELDS:
            v = getattr(self, f)
            if v is None:
                continue          # unset (e.g. sampling params) — don't persist; server decides
            if isinstance(v, bool):
                v = "true" if v else "false"
            elif isinstance(v, list):
                v = ",".join(str(x) for x in v)
            out[f.upper()] = str(v)
        return out

    @classmethod
    def load(cls) -> "Config":
        return cls()  # type: ignore[call-arg]


def load_dotenv_into_environ(env_path=None) -> list[str]:
    """Load KEY=VALUE lines from .env into os.environ (without overriding vars already set).

    pydantic-settings reads .env only for its own Config fields — arbitrary vars the operator
    adds (e.g. SERVICE_PASSWORD) never reach os.environ, so tool code and `_tool_secrets` can't see
    them. This makes .env behave like a real environment file. Returns the keys it set.
    """
    import os
    from pathlib import Path

    p = Path(env_path) if env_path else Path(__file__).resolve().parent / ".env"
    if not p.exists():
        return []
    set_keys = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")   # tolerate quotes / stray spaces
        if key and key not in os.environ:
            os.environ[key] = val
            set_keys.append(key)
    return set_keys


def persist_to_env(config: "Config", env_path) -> None:
    """Write config's scalar values into an .env file, updating keys in place and
    preserving comments / unrelated lines. Missing keys are appended.
    """
    import os

    pairs = config.env_pairs()
    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as fh:
            lines = fh.read().splitlines()

    seen = set()
    out_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in pairs:
                out_lines.append(f"{key}={pairs[key]}")
                seen.add(key)
                continue
        out_lines.append(line)
    for key, val in pairs.items():
        if key not in seen:
            out_lines.append(f"{key}={val}")

    with open(env_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out_lines) + "\n")
