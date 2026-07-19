"""Telegram bot — just another *client* of the Engine (same status as the
dashboard). It holds NO conversation state of its own, never imports from
``backend`` peers' internals or ``engine`` internals: it uses the chat id (as a
string) as the engine ``session_id`` and drives everything through the public
Engine API (``run_task`` / ``reset`` / ``patch_config`` / ``get_config`` /
``skills`` / ``subscribe``).

The decision logic is factored into small, pure, module-level helpers so it can
be unit-tested without a network, a real Telegram server, or a real model.
"""
from __future__ import annotations

import asyncio
import base64
import html as _html
import logging
import re
import time
from typing import Any

from telegram import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

log = logging.getLogger("argus.telegram")

# Slash commands registered with Telegram (they appear in the "/" menu in every
# allowed chat). Each (command, description) must have a matching handler below.
# This is the maintainable source of truth; add a row + a handler to add a command.
BOT_COMMANDS = [
    ("new", "Start a new conversation (clears history)"),
    ("usage", "Show context size and session usage"),
    ("compact", "Summarize & shrink the conversation context"),
    ("mode", "Show or switch tool-calling mode (native | manual)"),
    ("model", "Show/switch model: /model <name> (e.g. /model z-ai/glm-5.2)"),
    ("models", "List saved model presets"),
    ("reasoning", "Set reasoning level: /reasoning auto|off|low|medium|high"),
    ("roles", "Show capability→model roles (chat, embedding, …)"),
    ("role", "Assign a role: /role <capability> <connection>"),
    ("reembed", "Rebuild memory/knowledge vectors after changing the embedding model"),
    ("skills", "List the skills currently loaded"),
    ("tools", "List the tools currently available"),
    ("cron", "List your scheduled tasks"),
    ("retry", "Re-run your last message"),
    ("memories", "Show everything the agent has remembered about you"),
    ("forget", "Delete a saved memory: /forget <id>"),
    ("status", "Show whether the agent is working and which step it's on"),
    ("stop", "Interrupt whatever the agent is currently doing"),
    ("restart", "Restart the Argus server"),
    ("verbose", "Show full tool/skill call history per turn: /verbose on|off"),
    ("pending", "List dependency installs awaiting your approval"),
    ("approve", "Approve a pending install: /approve <id>"),
    ("deny", "Deny a pending install: /deny <id>"),
    ("help", "Show help and available commands"),
]


def dep_request_text(r: dict) -> str:
    """Plain-text (no Markdown — module/tool names contain underscores) description of a
    single pending install request, shown alongside Approve/Deny buttons."""
    t = f"🔧 Approval needed: install '{r['module']}' for tool '{r.get('tool_name','?')}'."
    if r.get("last_error"):
        t += "\n⚠️ A previous install attempt failed — you can retry."
    t += "\n⚠️ Approving installs the package and lets created tools import it."
    return t


def dep_keyboard(req_id: str) -> InlineKeyboardMarkup:
    """Inline Approve/Deny buttons for a pending install (callback_data ≤ 64 bytes)."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve & install", callback_data=f"depok:{req_id}"),
        InlineKeyboardButton("🚫 Deny", callback_data=f"depno:{req_id}"),
    ]])


def _approve_result_text(res: dict) -> str:
    """Plain-text result line for an approve action (button or command)."""
    if res.get("ok"):
        ver = f" v{res['version']}" if res.get("version") else ""
        return (f"✅ Installed '{res['module']}'{ver}. Building the tool now — "
                "I'll send the result shortly.")
    return f"❌ Could not install: {str(res.get('error', 'unknown error'))[:400]}"


def format_usage(u: dict) -> str:
    win = u.get("context_window")
    pct = u.get("percent_used")
    ctx_line = f"{u['context_tokens']:,} tokens"
    if win:
        ctx_line += f" / {win:,} ({pct}% of window)"
    ctx_line += f"  [{u['context_tokens_source']}]"
    b = u.get("breakdown") or {}
    breakdown = ""
    if b:
        breakdown = (f"• breakdown: system {b['system_prompt']:,} · "
                     f"tools {b['tool_schemas']:,} · history {b['conversation']:,} tokens\n")
    return (
        "📊 Context & usage\n"
        f"• context length: {ctx_line}\n"
        f"{breakdown}"
        f"• messages in history: {u['messages']}  (runs: {u['runs_this_session']})\n"
        f"• last reply: {u['last_completion_tokens']} tokens · session output: {u['total_output_tokens']}\n"
        f"• model: {u['model']} · mode: {u['tool_calling_mode']} · skills: {u['skill_selection_mode']}\n"
        f"• available: {u['tools_available']} tools, {u['skills_available']} skills\n"
        "Use /compact to summarize and shrink the context."
    )

def format_run_status(s: dict) -> str:
    if s.get("running"):
        step = s.get("current_step") or 0
        head = f"🟢 Working — step {step}/{s.get('max_steps', '?')}"
        if s.get("last_tool"):
            head += f" (last tool: {s['last_tool']})"
    else:
        head = "⚪ Idle — not working on anything right now"
    return (f"{head}\n"
            f"• turns this session: {s.get('turns', 0)}\n"
            f"• messages in history: {s.get('messages', 0)}")


TELEGRAM_MAX_CHARS = 4096
_TRUNCATE_MARKER = "\n\n… (truncated)"

# tool name -> lightweight progress line shown while the loop runs.
_PROGRESS_LINES = {
    "web_search": "🔍 searching the web…",
    "fetch_page": "📄 reading a page…",
    "calculator": "🧮 calculating…",
    "get_current_time": "🕐 checking the time…",
}

_THINKING = "🤔 thinking…"

def help_text() -> str:
    """Built from BOT_COMMANDS so /help never drifts from the registered commands."""
    lines = ["Argus — small-model agent-loop testbed.", "",
             "Just send me a message and I'll run the agent loop on it.", "", "Commands:"]
    lines += [f"/{c} — {d}" for c, d in BOT_COMMANDS]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Pure, testable helpers
# --------------------------------------------------------------------------
def is_allowed(chat_id: int, config: Any) -> bool:
    """True only if ``chat_id`` is in the configured allowlist.

    An empty allowlist serves no one (returns False for every id).
    """
    return chat_id in getattr(config, "allowed_chat_ids", [])


def _safe_pending(engine: Any) -> list:
    """Pending dependency-install requests, or [] if the engine predates the feature."""
    try:
        return engine.pending_deps()
    except Exception:
        return []


def _safe_pending_trust(engine: Any) -> list:
    """Pending trusted-tool requests, or [] if the engine predates the feature."""
    try:
        return engine.pending_trust()
    except Exception:
        return []


def progress_line_for(tool_name: str) -> str:
    """Lightweight status line for a ``tool_call`` event's tool name.

    Falls back to a generic line for unknown tools.
    """
    return _PROGRESS_LINES.get(tool_name, f"⚙️ using {tool_name}…")


def _md_inline(text: str) -> str:
    """Inline markdown -> Telegram HTML (text must already be HTML-escaped)."""
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", text)
    return text


def to_telegram_html(md: str) -> str:
    """Convert GitHub-flavored markdown (what the model emits) into Telegram's HTML
    subset (<b>, <i>, <code>, <pre>, <a>). Headers -> bold, bullets -> •. Best-effort;
    on any parse failure at send time we fall back to strip_markdown()."""
    if not md:
        return ""
    blocks: list[str] = []
    codes: list[str] = []

    def _stash_block(m):
        blocks.append(m.group(1))
        return f"\x00B{len(blocks) - 1}\x00"

    def _stash_code(m):
        codes.append(m.group(1))
        return f"\x00C{len(codes) - 1}\x00"

    text = re.sub(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", _stash_block, md, flags=re.S)
    text = re.sub(r"`([^`\n]+)`", _stash_code, text)
    text = _html.escape(text, quote=False)

    lines = []
    for line in text.split("\n"):
        h = re.match(r"\s*#{1,6}\s+(.*)", line)
        if h:
            lines.append("<b>" + h.group(1).strip() + "</b>")
            continue
        b = re.match(r"(\s*)[-*]\s+(.*)", line)
        if b:
            lines.append(b.group(1) + "• " + b.group(2))
            continue
        lines.append(line)
    text = _md_inline("\n".join(lines))

    for i, c in enumerate(codes):
        text = text.replace(f"\x00C{i}\x00", "<code>" + _html.escape(c, quote=False) + "</code>")
    for i, blk in enumerate(blocks):
        text = text.replace(f"\x00B{i}\x00", "<pre>" + _html.escape(blk, quote=False) + "</pre>")
    return text


def strip_markdown(md: str) -> str:
    """Fallback: remove markdown syntax so it reads cleanly as plain text."""
    t = re.sub(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", r"\1", md or "", flags=re.S)
    t = re.sub(r"`([^`]+)`", r"\1", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"(?<!\w)__(.+?)__(?!\w)", r"\1", t, flags=re.S)
    t = re.sub(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", r"\1", t)
    t = re.sub(r"^\s*#{1,6}\s+", "", t, flags=re.M)
    t = re.sub(r"^(\s*)[-*]\s+", r"\1• ", t, flags=re.M)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)", r"\1 (\2)", t)
    return t


async def deliver(msg, text: str) -> None:
    """Edit `msg` with the agent's answer, rendered as Telegram HTML; on a parse
    error, fall back to clean plain text so delivery never fails."""
    try:
        await msg.edit_text(to_telegram_html(text), parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True)
    except Exception:
        try:
            await msg.edit_text(strip_markdown(text))
        except Exception:
            log.debug("could not edit message with answer", exc_info=True)


async def deliver_new(reply_to, text: str) -> None:
    """Send the agent's answer as a NEW message (verbose mode: keeps the stacked tool-call
    history message from being overwritten). Same HTML render + plain-text fallback as deliver."""
    try:
        await reply_to.reply_text(to_telegram_html(text), parse_mode=ParseMode.HTML,
                                  disable_web_page_preview=True)
    except Exception:
        try:
            await reply_to.reply_text(strip_markdown(text))
        except Exception:
            log.debug("could not send answer message", exc_info=True)


def split_for_telegram(text: str, limit: int = 3900) -> list[str]:
    """Break a long answer into <=limit-char pieces at paragraph/line/word boundaries, so a long
    reply is delivered as SEVERAL messages instead of being cut off with '… (truncated)'."""
    text = text or "(no answer)"
    if len(text) <= limit:
        return [text]
    out, remaining = [], text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = window.rfind("\n\n")                      # prefer a paragraph break
        if cut < limit * 0.5:
            cut = window.rfind("\n")                     # else a line break
        if cut < limit * 0.5:
            cut = window.rfind(" ")                      # else a word break
        if cut <= 0:
            cut = limit                                  # else a hard cut
        out.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining.strip():
        out.append(remaining)
    return out


def truncate_for_telegram(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    """Return ``text`` unchanged if within Telegram's per-message limit, else
    truncate and append a marker so the whole thing still fits under ``limit``.
    """
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    keep = limit - len(_TRUNCATE_MARKER)
    if keep < 0:
        return text[:limit]
    return text[:keep] + _TRUNCATE_MARKER


def mode_command(engine: Any, arg: str | None) -> str:
    """Pure logic behind ``/mode``.

    - no arg -> report the current ``tool_calling_mode``.
    - ``native``/``manual`` -> patch the engine config, confirm the new mode.
    - anything else -> a helpful error message, engine left unchanged.
    """
    arg = (arg or "").strip()
    if not arg:
        current = engine.get_config().get("tool_calling_mode")
        return f"Tool-calling mode is currently: {current}"
    try:
        updated = engine.patch_config({"tool_calling_mode": arg})
    except Exception:
        current = engine.get_config().get("tool_calling_mode")
        return (
            f"Invalid mode: {arg!r}. Use /mode native or /mode manual "
            f"(currently {current})."
        )
    return f"Tool-calling mode set to: {updated.get('tool_calling_mode')}"


def model_command(engine, args: list) -> str:
    """/model — show current model. /model <name> — switch to a saved preset or, if unknown,
    a new OpenRouter model id (added as a preset). /model rm <name> — remove a preset."""
    args = args or []
    if args and args[0].lower() in ("rm", "remove", "del", "delete"):
        if len(args) < 2:
            return "Usage: /model rm <name>"
        n = engine.model_preset_remove(args[1])
        return f"Removed {n} preset(s) matching {args[1]!r}." if n else f"No preset matched {args[1]!r}."
    if not args:
        a = engine.model_presets()["active"]
        return (f"Current model: {a['model_name']}\n"
                f"backend: {a['provider']}  ·  reasoning: {a['reasoning']}\n"
                f"Switch: /model <name>   ·   List: /models")
    arg = " ".join(args).strip()
    try:
        res = engine.model_switch(arg)
    except Exception as e:
        return f"Couldn't switch to {arg!r}: {e}"
    p = res["switched_to"]
    note = "  (new preset added)" if res["created"] else ""
    return (f"✓ Switched to {p['model_name']}{note}\n"
            f"backend: {p.get('provider', 'auto')} — saved, survives restarts.")


def models_text(engine) -> str:
    d = engine.model_presets()
    active = d["active"]["model_name"]
    presets = d["presets"]
    if not presets:
        return "No model presets yet. Add one with /model <name>  (e.g. /model z-ai/glm-5.2)."
    lines = ["Model presets:"]
    for p in presets:
        mark = "  ← current" if p["model_name"] == active else ""
        lines.append(f"• {p['label']}  ({p['model_name']} · {p.get('provider', 'auto')}){mark}")
    lines.append("\nSwitch: /model <name>   ·   Add: /model <new-id>   ·   Remove: /model rm <name>")
    return "\n".join(lines)


_REASONING_LEVELS = ("auto", "off", "low", "medium", "high")


def reasoning_command(engine, arg) -> str:
    arg = (arg or "").strip().lower()
    if not arg:
        cur = engine.get_config().get("model_reasoning")
        return f"Reasoning level: {cur}.  Set with /reasoning {'|'.join(_REASONING_LEVELS)}."
    if arg not in _REASONING_LEVELS:
        return f"Invalid level {arg!r}. Use one of: {', '.join(_REASONING_LEVELS)}."
    engine.patch_config({"model_reasoning": arg})
    engine.save_config_to_env()
    return f"✓ Reasoning set to {arg} (saved)."


def roles_text(engine) -> str:
    d = engine.model_roles()
    roles = d.get("roles", {})
    lines = ["Capability roles:"]
    for cap in d.get("capabilities", []):
        resv = "" if cap in ("chat", "embedding") else "  (reserved)"
        lines.append(f"• {cap}: {roles.get(cap) or '— unset —'}{resv}")
    conns = ", ".join(c["label"] for c in d.get("connections", [])) or "none"
    lines.append(f"\nConnections: {conns}")
    lines.append("Set with /role <capability> <connection>  (e.g. /role embedding embed)")
    return "\n".join(lines)


def role_command(engine, args: list) -> str:
    from engine.model_presets import ROLES
    args = args or []
    if len(args) < 2:
        return "Usage: /role <capability> <connection>  (use 'none' to unset). See /roles."
    cap = args[0].strip().lower()
    if cap not in ROLES:
        return f"Unknown capability {cap!r}. One of: {', '.join(ROLES)}."
    target = " ".join(args[1:]).strip()
    conn = None if target.lower() in ("none", "unset", "off", "-") else target
    try:
        res = engine.set_role(cap, conn)
    except Exception as e:
        return f"Couldn't set {cap}: {e}"
    if conn is None:
        return f"✓ {cap} unset."
    warn = "\n⚠ changing the embedding model needs a re-embed of stored vectors." if cap == "embedding" else ""
    c = res["connection"]
    return f"✓ {cap} → {c['label']} ({c['model_name']}){warn}"


def skills_text(skills: list[dict]) -> str:
    """Render ``engine.skills()`` output into a reply string."""
    if not skills:
        return "No skills loaded yet."
    lines = ["Loaded skills:"]
    for s in skills:
        name = s.get("name", "?")
        desc = s.get("description", "")
        lines.append(f"• {name} — {desc}" if desc else f"• {name}")
    return "\n".join(lines)


def tools_text(overview: dict) -> str:
    """Render ``engine.tools_overview()`` (builtin + created) into a reply string."""
    builtin = overview.get("builtin", []) or []
    created = overview.get("created", []) or []
    lines = [f"🧰 Tools available ({len(builtin) + len(created)}):", "", "Built-in:"]
    lines += [f"• {t.get('name','?')}" for t in builtin]
    if created:
        lines += ["", "Created:"]
        lines += [f"• {t.get('name','?')}" for t in created]
    return "\n".join(lines)


def cron_text(jobs: list[dict]) -> str:
    """Render ``engine.scheduled_jobs()`` into a reply string."""
    if not jobs:
        return "🗓️ No scheduled tasks."
    lines = [f"🗓️ Scheduled tasks ({len(jobs)}):"]
    for j in jobs:
        lines.append(f"• {j.get('instruction','?')[:60]}\n    {j.get('schedule','')} · next {str(j.get('next_run',''))[:16]}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Progress consumer
# --------------------------------------------------------------------------
def _verbose_history(events: list) -> str:
    """Render the full tool/skill history for a turn from the authoritative event log — same format
    the live consumer streams, but rebuilt from `engine.recent()` so it can't be missing calls the
    racy live consumer hadn't drained yet. Returns '' if the turn used no tools/skills."""
    lines = []
    for ev in events:
        if ev.kind == "tool_call":
            lines.append(f"⚙️ {ev.data.get('tool','')} {_short_args(ev.data.get('args'))}".rstrip())
        elif ev.kind == "skill" and ev.data.get("active_skill"):
            lines.append(f"🎯 skill: {ev.data['active_skill']}")
    if not lines:
        return ""
    return truncate_for_telegram("🔧 Working…\n" + "\n".join(lines), limit=3800)


def _short_args(args: Any, limit: int = 60) -> str:
    """A compact one-line rendering of a tool call's args for the verbose history."""
    if not args:
        return ""
    try:
        s = ", ".join(f"{k}={v}" for k, v in args.items())
    except Exception:
        s = str(args)
    s = " ".join(s.split())
    return f"({s[:limit]}…)" if len(s) > limit else f"({s})"


async def _consume_progress(engine: Any, session_id: str, status_msg, verbose: bool = False,
                            state: dict = None) -> None:
    """Consume the engine event stream for one session and edit ``status_msg`` with progress.

    Default: overwrite with a single line for the CURRENT tool call (clean for normal use).
    verbose=True: STACK every tool/skill call into a growing history (debug view). Until cancelled.
    ``state["had_tools"]`` is set True once any tool runs (lets the caller decide whether the
    verbose history is worth preserving as its own message).
    """
    last = None
    lines: list[str] = []
    try:
        async for ev in engine.subscribe(session_id):
            if ev.kind == "tool_call" and state is not None:
                state["had_tools"] = True
            if verbose:
                if ev.kind == "tool_call":
                    lines.append(f"⚙️ {ev.data.get('tool','')} {_short_args(ev.data.get('args'))}".rstrip())
                elif ev.kind == "skill" and ev.data.get("active_skill"):
                    lines.append(f"🎯 skill: {ev.data['active_skill']}")
                else:
                    continue
                text = truncate_for_telegram("🔧 Working…\n" + "\n".join(lines), limit=3800)
            else:
                if ev.kind != "tool_call":
                    continue
                text = progress_line_for(ev.data.get("tool", ""))
                if text == last:
                    continue
                last = text
            try:
                await status_msg.edit_text(text)
            except Exception:
                # Editing can fail (message unchanged, deleted, rate-limited); best-effort only.
                pass
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - defensive
        log.debug("progress consumer stopped", exc_info=True)


async def _keep_typing(bot: Any, chat_id: int) -> None:
    """Keep the bot showing 'typing…' in the chat while the agent works (the indicator
    Telegram shows lasts only ~5s, so refresh it). Best-effort; runs until cancelled."""
    try:
        while True:
            try:
                await bot.send_chat_action(chat_id, "typing")
            except Exception:
                pass
            await asyncio.sleep(4)
    except asyncio.CancelledError:
        raise


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------
async def register_bot_commands(app: Application) -> None:
    """Register Argus's slash commands with Telegram and clear any commands left by a
    previously-used bot at more specific scopes (Telegram resolves the "/" menu by the
    most specific scope, so an old per-scope list would otherwise still show).

    Must be called explicitly (we start the bot via initialize()/start() rather than
    run_polling(), so python-telegram-bot's post_init hook does NOT fire).
    """
    cmds = [BotCommand(c, d) for c, d in BOT_COMMANDS]
    await app.bot.set_my_commands(cmds)  # default scope
    # Also set them EXPLICITLY for private chats — an empty all_private_chats scope
    # (left by a previous bot) would otherwise shadow the default in DMs on some clients.
    try:
        await app.bot.set_my_commands(cmds, scope=BotCommandScopeAllPrivateChats())
    except Exception:
        log.debug("could not set private-chat scope commands", exc_info=True)
    try:
        await app.bot.delete_my_commands(scope=BotCommandScopeAllGroupChats())
    except Exception:
        log.debug("could not clear group-chat scope commands", exc_info=True)
    log.info("registered %d Telegram slash commands (default + private scopes)", len(BOT_COMMANDS))


def build_telegram_app(engine: Any, config: Any) -> Application:
    """Build (but do not start) the python-telegram-bot Application."""

    verbose_chats: set = set()          # chat ids that opted into the stacked tool-call history
    last_text: dict = {}                # chat id -> last user message (for /retry)
    command_dispatch: dict = {}         # built-in command name -> its handler (filled after registration,
                                        # so a custom alias whose expansion is /model, /reasoning, … runs it)

    async def _guard(update: Update) -> int | None:
        """Return the chat id if allowed, else None (silent ignore)."""
        chat = update.effective_chat
        if chat is None:
            return None
        if not is_allowed(chat.id, config):
            return None
        return chat.id

    async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        msg = update.effective_message
        text = (msg.text or msg.caption or "").strip()
        images = []
        if msg.photo:                                    # vision: forward the photo to the model
            try:
                f = await msg.photo[-1].get_file()       # largest rendition
                data = bytes(await f.download_as_bytearray())
                images.append("data:image/jpeg;base64," + base64.b64encode(data).decode())
            except Exception:
                log.debug("could not download photo", exc_info=True)
        if not text and not images:
            return
        last_text[chat_id] = text or "(image)"
        await _run_turn(update, chat_id, text, images)

    async def _run_turn(update: Update, chat_id: int, text: str, images: list) -> None:
        """Run one agent turn for `text`/`images` and deliver the reply. Shared by on_message
        and the custom-command catch-all so both go through the exact same run/split/deliver path."""
        session_id = str(chat_id)
        # Back-to-back: if the agent is already working on an earlier message from this chat,
        # preempt it (like Hermes) so the newest message takes over. Slash commands go through
        # their own handlers and never hit this path, so /verbose, /status, etc. don't interrupt.
        if await engine.interrupt(session_id):
            await update.effective_message.reply_text(
                "⏭️ Got your new message — interrupting the previous one.")
        pending_before = {r["id"] for r in _safe_pending(engine)}
        trust_before = {r["id"] for r in _safe_pending_trust(engine)}
        verbose = chat_id in verbose_chats
        progress_state = {"had_tools": False}
        start_ts = time.time()   # scope this turn's events for the authoritative had-tools check below
        status_msg = await update.effective_message.reply_text(_THINKING)
        progress = asyncio.create_task(
            _consume_progress(engine, session_id, status_msg, verbose=verbose, state=progress_state)
        )
        typing = asyncio.create_task(_keep_typing(update.get_bot(), chat_id))
        try:
            answer = await engine.run_task(session_id, text, images=images or None, origin="telegram")
        except asyncio.CancelledError:      # /stop cancelled the in-flight run
            answer = "⏹️ Stopped."
        except Exception as e:              # surface loop failures instead of hanging
            log.exception("run_task failed for session %s", session_id)
            answer = f"⚠️ Something went wrong: {e}"
        finally:
            for t in (progress, typing):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

        chunks = split_for_telegram(answer or "(no answer)")   # long answers → several messages
        # Authoritative: did any tool run THIS turn? Read it from the event log (filled synchronously
        # as the loop runs), NOT from the live consumer's had_tools flag — the finally-cancel above can
        # race the consumer and leave that flag False even when tools ran, which then OVERWRITES the
        # tool/skill history with the answer. This is the bug that reproduced "every once in a while".
        turn_events = [ev for ev in engine.recent(session_id) if ev.ts >= start_ts]
        if verbose and any(ev.kind == "tool_call" for ev in turn_events):
            # Rebuild the COMPLETE tool/skill history from the log (the live consumer may have been
            # cancelled mid-drain, so it can be short) and pin it in the status message; then send the
            # answer as its OWN message so the history is never overwritten.
            hist = _verbose_history(turn_events)
            if hist:
                try:
                    await status_msg.edit_text(hist)
                except Exception:
                    pass
            for ch in chunks:
                await deliver_new(update.effective_message, ch)
        else:
            # normal mode (or verbose with no tools) — replace the spinner with the first part,
            # then send any remaining parts as their own messages
            await deliver(status_msg, chunks[0])
            for ch in chunks[1:]:
                await deliver_new(update.effective_message, ch)

        # If the agent made any chart images this turn, send them as photos (Telegram can't
        # render the SVG, so make_chart's PNG is what goes here).
        for img in engine.take_pending_images(session_id):
            try:
                with open(img, "rb") as fh:
                    await update.get_bot().send_photo(chat_id, fh)
            except Exception:
                log.debug("could not send chart image %s", img, exc_info=True)

        # If the agent filed a new install request during this turn, surface it here with
        # tap-to-approve buttons so there's no id to copy.
        new_reqs = [r for r in _safe_pending(engine) if r["id"] not in pending_before]
        for r in new_reqs:
            await update.effective_message.reply_text(
                dep_request_text(r), reply_markup=dep_keyboard(r["id"]))

        # Trusted-tool requests are notice-only — approving arbitrary code is a code-REVIEW
        # decision, so it happens in the dashboard (where you can actually read the code), not here.
        new_trust = [r for r in _safe_pending_trust(engine) if r["id"] not in trust_before]
        for r in new_trust:
            await update.effective_message.reply_text(
                f"🔐 The tool '{r['tool_name']}' wants TRUSTED (unsandboxed) execution. Review its "
                "code and Approve/Deny it in the dashboard's 'Trusted-tool requests' panel, then ask "
                "me to build it again.")

    async def on_custom_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Catch-all for `/foo` that no built-in handler claimed. If `foo` is a user-defined alias,
        act on its expansion: when the expansion is itself a slash command (e.g. `/model ds4f`) run
        that built-in; otherwise send the expansion to the agent as a normal turn. Unknown /commands
        stay silent — a bare typo shouldn't reach the model."""
        chat_id = await _guard(update)
        if chat_id is None:
            return
        raw = (update.effective_message.text or "").strip()
        if not raw.startswith("/"):
            return
        name = raw.partition(" ")[0][1:].split("@", 1)[0]   # strip leading '/' and any @botname suffix
        expansion = engine.custom_command_expand(name)
        if not expansion:
            return                              # unknown /command — likely a typo, so stay silent
        extra = raw.partition(" ")[2].strip()   # anything the user typed after the alias
        if expansion.startswith("/"):
            chead, _, cargs = expansion[1:].partition(" ")
            handler = command_dispatch.get(chead.split("@", 1)[0].lower())
            if handler:                         # expansion is a built-in command -> run it directly
                argstr = " ".join(p for p in (cargs.strip(), extra) if p)
                ctx.args = argstr.split() if argstr else []
                await handler(update, ctx)
                return
        # plain-text alias -> run as a turn; run_task expands the alias (+ any trailing args)
        last_text[chat_id] = raw
        await _run_turn(update, chat_id, raw, [])

    async def on_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        engine.reset(str(chat_id))
        await update.effective_message.reply_text("🆕 New conversation — history cleared.")

    async def on_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text("🔄 Restarting Argus… back in a moment.")

        async def _do():
            await asyncio.sleep(0.5)          # let the reply flush
            import os
            import sys
            os.execv(sys.executable, [sys.executable] + sys.argv)   # re-exec in place
        asyncio.create_task(_do())

    async def on_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        arg = ctx.args[0] if ctx.args else None
        await update.effective_message.reply_text(mode_command(engine, arg))

    async def on_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(model_command(engine, ctx.args))

    async def on_models(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(models_text(engine))

    async def on_reasoning(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        arg = ctx.args[0] if ctx.args else None
        await update.effective_message.reply_text(reasoning_command(engine, arg))

    async def on_roles(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(roles_text(engine))

    async def on_role(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(role_command(engine, ctx.args))

    async def on_reembed(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        msg = await update.effective_message.reply_text("Re-embedding stored vectors…")
        d = await engine.reembed()
        if d.get("ok"):
            await msg.edit_text(f"✓ Re-embedded {d['memory']} facts + {d['knowledge']} chunks "
                                f"with {d.get('model')}.")
        else:
            await msg.edit_text("Re-embed problem: " + (d.get("error") or f"{d.get('failed', 0)} failed"))

    async def on_skills(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(skills_text(engine.skills()))

    async def on_tools(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(tools_text(engine.tools_overview()))

    async def on_cron(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(cron_text(engine.scheduled_jobs()))

    async def on_retry(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        text = last_text.get(chat_id)
        if not text:
            await update.effective_message.reply_text("Nothing to retry yet.")
            return
        session_id = str(chat_id)
        status_msg = await update.effective_message.reply_text(f"🔁 Retrying: {text[:60]}…")
        progress = asyncio.create_task(
            _consume_progress(engine, session_id, status_msg, verbose=chat_id in verbose_chats))
        typing = asyncio.create_task(_keep_typing(update.get_bot(), chat_id))
        try:
            answer = await engine.run_task(session_id, text, origin="telegram")
        except asyncio.CancelledError:
            answer = "⏹️ Stopped."
        except Exception as e:
            answer = f"⚠️ Something went wrong: {e}"
        finally:
            for t in (progress, typing):
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
        _rchunks = split_for_telegram(answer or "(no answer)")
        await deliver(status_msg, _rchunks[0])
        for _ch in _rchunks[1:]:
            await deliver_new(update.effective_message, _ch)

    async def on_usage(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(format_usage(await engine.usage(str(chat_id))))

    async def on_compact(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        msg = await update.effective_message.reply_text("🗜️ Compacting…")
        res = await engine.compact(str(chat_id))
        if not res.get("compacted"):
            await msg.edit_text(f"Nothing to compact: {res.get('reason', 'not enough history')}.")
            return
        await msg.edit_text(
            f"✅ Compacted: {res['messages_before']}→{res['messages_after']} messages, "
            f"~{res['tokens_before']:,}→~{res['estimated_tokens_after']:,} tokens. "
            "Earlier context is preserved as a summary.")

    async def on_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(help_text())

    async def on_pending(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        pending = _safe_pending(engine)
        if not pending:
            await update.effective_message.reply_text("✅ No dependency installs are waiting.")
            return
        for r in pending:                          # one message + buttons per request
            await update.effective_message.reply_text(
                dep_request_text(r), reply_markup=dep_keyboard(r["id"]))

    async def on_approve(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)             # kept as a fallback to the buttons
        if chat_id is None:
            return
        if not ctx.args:
            await update.effective_message.reply_text("Tip: use the Approve button. Or /approve <id> (see /pending).")
            return
        msg = await update.effective_message.reply_text("⏳ Installing (this can take a minute)…")
        res = await engine.approve_dep(ctx.args[0].strip())
        await msg.edit_text(_approve_result_text(res))

    async def on_deny(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        if not ctx.args:
            await update.effective_message.reply_text("Tip: use the Deny button. Or /deny <id> (see /pending).")
            return
        res = engine.deny_dep(ctx.args[0].strip())
        await update.effective_message.reply_text(
            f"🚫 Denied install of '{res['module']}'." if res.get("ok")
            else res.get("error", "no such pending request"))

    async def on_memories(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        facts = engine.memory_list(str(chat_id))
        if not facts:
            await update.effective_message.reply_text(
                "🧠 I haven't saved any memories about you yet.")
            return
        lines = "\n".join(f"{f['id']}. {f['text']}" for f in facts)
        await update.effective_message.reply_text(
            f"🧠 What I remember about you:\n{lines}\n\nUse /forget <id> to remove any.")

    async def on_forget(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        if not ctx.args:
            await update.effective_message.reply_text(
                "Usage: /forget <id>  — see the ids with /memories")
            return
        try:
            fid = int(ctx.args[0])
        except ValueError:
            await update.effective_message.reply_text(
                "Give a numeric memory id, e.g. /forget 12  (see /memories)")
            return
        ok = engine.memory_forget(str(chat_id), fid)
        await update.effective_message.reply_text(
            "🗑️ Forgotten." if ok else f"I don't have a memory with id {fid}. Check /memories.")

    async def on_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        await update.effective_message.reply_text(
            format_run_status(engine.run_status(str(chat_id))))

    async def on_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        if engine.stop(str(chat_id)):
            await update.effective_message.reply_text("⏹️ Stopping…")
        else:
            await update.effective_message.reply_text("Nothing is running right now.")

    async def on_verbose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat_id = await _guard(update)
        if chat_id is None:
            return
        arg = (ctx.args[0].lower() if ctx.args else "")
        if arg == "on":
            verbose_chats.add(chat_id)
            await update.effective_message.reply_text(
                "🔎 Verbose ON — I'll stack the full tool/skill call history each turn.")
        elif arg == "off":
            verbose_chats.discard(chat_id)
            await update.effective_message.reply_text(
                "Verbose OFF — I'll show just the current step (the normal clean view).")
        else:
            state = "on" if chat_id in verbose_chats else "off"
            await update.effective_message.reply_text(
                f"Verbose is {state}. Use /verbose on or /verbose off.")

    async def on_dep_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle taps on the inline Approve/Deny buttons."""
        q = update.callback_query
        chat = update.effective_chat
        if chat is None or not is_allowed(chat.id, config):
            await q.answer("Not authorized.")
            return
        data = q.data or ""
        action, _, req_id = data.partition(":")
        if action == "depno":
            res = engine.deny_dep(req_id)
            await q.answer("Denied." if res.get("ok") else "Not pending.")
            await q.edit_message_text(
                f"🚫 Denied install of '{res['module']}'." if res.get("ok")
                else "This request is no longer pending.")
            return
        if action == "depok":
            await q.answer("Installing…")
            module = next((r["module"] for r in _safe_pending(engine) if r["id"] == req_id), req_id)
            await q.edit_message_text(f"⏳ Installing '{module}' (this can take a minute)…")
            res = await engine.approve_dep(req_id)
            await q.edit_message_text(_approve_result_text(res))
            return
        await q.answer()

    # concurrent_updates=True so each update runs in its own task: a slash command (/stop,
    # /status, /verbose) is handled WHILE the agent is working instead of queuing behind it,
    # and a new message can preempt an in-flight run (see on_message).
    app = Application.builder().token(config.telegram_bot_token).concurrent_updates(True).build()
    app.add_handler(CommandHandler("start", on_help))
    app.add_handler(CommandHandler("help", on_help))
    app.add_handler(CommandHandler("new", on_new))
    app.add_handler(CommandHandler("reset", on_new))          # alias (not in the menu)
    app.add_handler(CommandHandler("usage", on_usage))
    app.add_handler(CommandHandler("compact", on_compact))
    app.add_handler(CommandHandler("mode", on_mode))
    app.add_handler(CommandHandler("model", on_model))
    app.add_handler(CommandHandler("models", on_models))
    app.add_handler(CommandHandler("reasoning", on_reasoning))
    app.add_handler(CommandHandler("roles", on_roles))
    app.add_handler(CommandHandler("role", on_role))
    app.add_handler(CommandHandler("reembed", on_reembed))
    app.add_handler(CommandHandler("skills", on_skills))
    app.add_handler(CommandHandler("tools", on_tools))
    app.add_handler(CommandHandler("cron", on_cron))
    app.add_handler(CommandHandler("retry", on_retry))
    app.add_handler(CommandHandler("memories", on_memories))
    app.add_handler(CommandHandler("forget", on_forget))
    app.add_handler(CommandHandler("status", on_status))
    app.add_handler(CommandHandler("stop", on_stop))
    app.add_handler(CommandHandler("restart", on_restart))
    app.add_handler(CommandHandler("verbose", on_verbose))
    app.add_handler(CommandHandler("pending", on_pending))
    app.add_handler(CommandHandler("approve", on_approve))
    app.add_handler(CommandHandler("deny", on_deny))
    app.add_handler(CallbackQueryHandler(on_dep_callback, pattern=r"^dep(ok|no):"))
    app.add_handler(MessageHandler((filters.TEXT | filters.PHOTO) & ~filters.COMMAND, on_message))
    # Registered LAST: any /command not claimed by a built-in handler above falls through to here,
    # where it's matched against the user's custom aliases (unknown commands stay silent).
    app.add_handler(MessageHandler(filters.COMMAND, on_custom_command))
    # Map each registered built-in command to its handler so a custom alias can invoke one
    # (e.g. an alias expanding to `/model ds4f` actually switches the model).
    for h in app.handlers[0]:
        for cmd in (getattr(h, "commands", None) or []):
            command_dispatch[cmd] = h.callback
    return app
