"""Argus single entrypoint.

Starts the FastAPI app (engine API + dashboard) and — when a Telegram token is
configured — the Telegram bot, concurrently, in ONE asyncio event loop, sharing
ONE Engine instance. Both are clients of the same engine.

    python main.py

If ``TELEGRAM_BOT_TOKEN`` is blank (or no chat ids are allowlisted), Telegram is
skipped and Argus runs dashboard-only.
"""
from __future__ import annotations

import asyncio
import logging
import os

import uvicorn

from backend.app import create_app
from backend.telegram_bot import build_telegram_app, register_bot_commands
from config import Config, load_dotenv_into_environ
from engine.engine import Engine

log = logging.getLogger("argus.main")


def _banner(config: Config, telegram_on: bool) -> str:
    scheme = "https" if (config.ssl_certfile and config.ssl_keyfile) else "http"
    url = f"{scheme}://{config.host}:{config.port}"
    lines = [
        "",
        "=" * 60,
        "  Argus — small-model agent-loop testbed",
        "=" * 60,
        f"  tool_calling_mode  : {config.tool_calling_mode}",
        f"  skill_selection_mode: {config.skill_selection_mode}",
        f"  dashboard          : {url}",
        f"  telegram           : {'enabled' if telegram_on else 'disabled'}",
        "=" * 60,
        "",
    ]
    return "\n".join(lines)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO — which include the Telegram bot token.
    # Keep secrets out of the log file.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # Make .env behave like a real environment file so credential vars (e.g. SERVICE_PASSWORD)
    # the operator adds are visible to os.environ — which is where tool secrets are read from.
    loaded = load_dotenv_into_environ()
    if loaded:
        log.info("loaded %d var(s) from .env into the environment", len(loaded))
    config = Config.load()
    engine = Engine(config)
    app = create_app(engine)

    telegram_on = bool(config.telegram_bot_token) and bool(config.allowed_chat_ids)
    print(_banner(config, telegram_on), flush=True)

    # Optional HTTPS: only when BOTH a cert and key are configured and present on disk.
    # A missing/half-configured cert must not silently fall back in a surprising way, and must
    # never crash the boot — warn and stay on HTTP instead.
    tls = {}
    if config.ssl_certfile and config.ssl_keyfile:
        if os.path.exists(config.ssl_certfile) and os.path.exists(config.ssl_keyfile):
            tls = {"ssl_certfile": config.ssl_certfile, "ssl_keyfile": config.ssl_keyfile}
        else:
            log.warning("ssl_certfile/ssl_keyfile set but not found on disk — serving plain HTTP")

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level="info",
            **tls,
        )
    )

    tg_app = None
    if telegram_on:
        # Telegram startup must NEVER take down the dashboard/engine: a transient network
        # timeout reaching api.telegram.org at boot would otherwise crash the whole process.
        # Retry a few times, then fall back to dashboard-only if it still won't come up.
        for attempt in range(1, 4):
            try:
                tg_app = build_telegram_app(engine, config)
                await tg_app.initialize()
                # post_init doesn't fire with manual initialize(); register commands ourselves.
                await register_bot_commands(tg_app)
                await tg_app.start()
                await tg_app.updater.start_polling()
                log.info("Telegram enabled for chat ids: %s", config.allowed_chat_ids)
                break
            except Exception:
                log.warning("Telegram startup attempt %d/3 failed", attempt, exc_info=True)
                try:
                    if tg_app is not None:
                        await tg_app.shutdown()
                except Exception:
                    pass
                tg_app = None
                if attempt < 3:
                    await asyncio.sleep(3)

    if tg_app is not None:
        # Tell the allowlisted chats the gateway just (re)started — so a deploy/restart is visible.
        for _cid in config.allowed_chat_ids:
            try:
                await tg_app.bot.send_message(_cid, "🔄 Argus restarted — back online.")
            except Exception:
                log.debug("could not send restart notice to %s", _cid, exc_info=True)

        # Telegram parity for interactive approvals: when a Telegram-originated turn hits a gate
        # (dep-install, soul-edit, ...), ApprovalBroker._surface() calls this to push inline
        # Approve/Deny (+ standing, where the gate's policy allows it) buttons to that chat.
        # ApprovalBroker itself only checks `req["origin"] == "telegram"` before calling this, so
        # the allowlist check here is defensive-only (a telegram-origin session_id should already
        # be an allowed chat — see on_message's _guard).
        from backend.telegram_bot import apv_keyboard, apv_request_text
        from engine.approvals import GATES

        async def _telegram_approval(session_id: str, req: dict) -> None:
            try:
                chat_id = int(session_id)
            except (TypeError, ValueError):
                return
            if chat_id not in config.allowed_chat_ids:
                return
            gate = GATES.get(req["kind"])
            await tg_app.bot.send_message(
                chat_id, apv_request_text(req),
                reply_markup=apv_keyboard(req["id"], gate.states if gate else []))

        engine.approvals._telegram = _telegram_approval

        # Deliver scheduled-task results back to Telegram chats (session id == chat id).
        from backend.telegram_bot import to_telegram_html

        async def _deliver(session_id: str, text: str) -> None:
            try:
                chat_id = int(session_id)
            except (TypeError, ValueError):
                return  # non-telegram session (e.g. dashboard) — result is in the event log
            if chat_id not in config.allowed_chat_ids:
                return
            try:
                await tg_app.bot.send_message(chat_id, to_telegram_html(text),
                                              parse_mode="HTML", disable_web_page_preview=True)
            except Exception:
                await tg_app.bot.send_message(chat_id, text)

        engine.notifier.telegram_deliver = _deliver      # the notify tool's telegram channel

        # Background deliveries (scheduled results, watch alerts) go to Telegram AND fan out to any
        # extra channels the owner configured (email/ntfy).
        _fanout = [x.strip() for x in (config.notify_fanout or "").split(",") if x.strip()]

        async def _deliver_all(session_id: str, text: str) -> None:
            await _deliver(session_id, text)
            for ch in _fanout:
                try:
                    await engine.notifier.send(ch, text, subject="Argus", session_id=session_id)
                except Exception:
                    log.debug("fan-out to %s failed", ch, exc_info=True)

        engine.scheduler.deliver = _deliver_all
        engine.watch_manager.deliver = _deliver_all      # watch alerts reach Telegram + fan-out
    elif telegram_on:
        log.error("Telegram could not start after retries; continuing dashboard-only")

    # Watch alerts get a short model-written summary of what changed (best-effort, think=False).
    async def _watch_summary(url: str, description: str, text: str) -> str:
        try:
            resp = await engine._model_client().chat([
                {"role": "system", "content": "A web page the user is watching just changed. In "
                 "1–2 sentences, say what's most notable now, focused on what they're watching for. "
                 "Be concrete; no preamble."},
                {"role": "user", "content": f"Watching for: {description or '(any change)'}\n"
                                            f"URL: {url}\nNew page content:\n{text}"}],
                max_tokens=160, think=False)
            return (resp.content or "").strip()
        except Exception:
            return ""
    engine.watch_manager.summarize = _watch_summary

    # Start the task scheduler loop (fires due jobs; delivers via _deliver if set).
    if config.enable_scheduler:
        await engine.scheduler.start()
    if config.enable_watch:
        await engine.watch_manager.start()
    else:
        if not config.telegram_bot_token:
            log.info(
                "Telegram disabled (no token); dashboard-only at http://%s:%s",
                config.host, config.port,
            )
        else:
            log.info(
                "Telegram disabled (no allowed_chat_ids); dashboard-only at "
                "http://%s:%s", config.host, config.port,
            )

    try:
        await server.serve()
    finally:
        await engine.scheduler.stop()
        await engine.watch_manager.stop()
        if tg_app is not None:
            log.info("Shutting down Telegram bot…")
            for _cid in config.allowed_chat_ids:   # best-effort "going down" notice
                try:
                    await tg_app.bot.send_message(_cid, "🔻 Argus is restarting…")
                except Exception:
                    pass
            try:
                if tg_app.updater is not None:
                    await tg_app.updater.stop()
                await tg_app.stop()
                await tg_app.shutdown()
            except Exception:  # pragma: no cover - best-effort shutdown
                log.exception("error during Telegram shutdown")


if __name__ == "__main__":
    asyncio.run(main())
