"""Watcher — poll a URL/feed on a schedule, diff against last state, alert only on change.

The reliable "tell me when X changes" primitive (price drops, a page updates, a feed posts).
Persistent last-state + diffing is the fiddly part you don't want re-implemented per tool, so it's
a vetted built-in. A background loop (mirrors the scheduler) fetches each due watch, normalizes +
hashes the text, and on a change delivers a notification to the watch's session (optionally with a
model-written summary of what changed). The first fetch just establishes the baseline — no alert.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from engine.tools.base import Tool
from engine.tools.net_guard import safe_fetch, url_fetch_ok   # re-exported for existing imports

log = logging.getLogger("argus.watch")


def _normalize(text: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text or "", flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)          # strip tags
    return re.sub(r"\s+", " ", text).strip()


class WatchStore:
    def __init__(self, path: str):
        self.path = path
        self.watches: list[dict] = []
        self._load()

    def _load(self) -> None:
        if os.path.exists(self.path):
            try:
                self.watches = json.loads(open(self.path, encoding="utf-8").read())
            except Exception:
                self.watches = []

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.watches, fh, indent=1)
        os.replace(tmp, self.path)

    def add(self, url: str, description: str, session_id: str, interval_minutes: int, now: float) -> dict:
        w = {"id": "watch_" + uuid.uuid4().hex[:8], "url": url, "description": description,
             "session_id": session_id, "interval_minutes": max(5, int(interval_minutes)),
             "active": True, "last_hash": None, "last_checked": None, "created_at": now}
        self.watches.append(w)
        self._save()
        return w

    def list(self, session_id: Optional[str] = None) -> list[dict]:
        return [w for w in self.watches if session_id is None or w["session_id"] == session_id]

    def get(self, watch_id: str) -> Optional[dict]:
        return next((w for w in self.watches if w["id"] == watch_id), None)

    def update(self, watch_id: str, **fields) -> None:
        w = self.get(watch_id)
        if w:
            w.update(fields)
            self._save()

    def remove(self, watch_id: str, session_id: Optional[str] = None) -> bool:
        w = self.get(watch_id)
        if not w or (session_id is not None and w["session_id"] != session_id):
            return False
        self.watches = [x for x in self.watches if x["id"] != watch_id]
        self._save()
        return True


class WatchManager:
    """Background poller. deliver(session_id, text) pushes an alert (Telegram); summarize(url,
    description, text) -> str optionally writes a change summary. Both are set by main.py."""

    def __init__(self, store: WatchStore, tick: float = 60.0, timeout: float = 20.0,
                 deliver: Optional[Callable[[str, str], Awaitable[None]]] = None,
                 summarize: Optional[Callable[[str, str, str], Awaitable[str]]] = None):
        self.store = store
        self.tick = tick
        self.timeout = timeout
        self.deliver = deliver
        self.summarize = summarize
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.tick)
                await self.check_all()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("watch loop tick failed", exc_info=True)

    async def check_all(self) -> None:
        now = time.time()
        for w in list(self.store.list()):
            if not w.get("active"):
                continue
            last = w.get("last_checked") or 0
            if now - last >= w["interval_minutes"] * 60:
                try:
                    await self._check(w, now)
                except Exception:
                    log.debug("watch check failed for %s", w.get("id"), exc_info=True)

    async def _check(self, w: dict, now: float) -> None:
        text = await self._fetch(w["url"])
        digest = hashlib.sha256(_normalize(text).encode("utf-8")).hexdigest()
        first = w.get("last_hash") is None
        changed = digest != w.get("last_hash")
        self.store.update(w["id"], last_hash=digest, last_checked=now)
        if changed and not first:
            await self._alert(w, _normalize(text))

    async def _fetch(self, url: str) -> str:
        # SSRF-guarded fetch (validates host + every redirect hop; see net_guard).
        r = await safe_fetch(url, timeout=self.timeout,
                             headers={"User-Agent": "Argus-Watcher/1.0"})
        return r.text

    async def _alert(self, w: dict, text: str) -> None:
        if not self.deliver:
            return
        note = ""
        if self.summarize:
            try:
                note = await self.summarize(w["url"], w.get("description", ""), text[:4000])
            except Exception:
                note = ""
        msg = f"🔔 Watch update — {w.get('description') or w['url']}\n{w['url']}"
        if note:
            msg += f"\n\n{note}"
        try:
            await self.deliver(w["session_id"], msg)
        except Exception:
            log.debug("watch alert delivery failed", exc_info=True)


# ---- session-bound tools ----

class WatchTool(Tool):
    name = "watch"
    description = (
        "Watch a web page or feed and alert the user when it CHANGES. Give the url and a short "
        "description of what you're watching for; optionally interval_minutes (default 60, min 5). "
        "The user is notified only when the content changes — good for price drops, updates, new "
        "posts. The first check just records a baseline."
    )

    class Params(BaseModel):
        url: str = Field(..., description="the page/feed URL to watch")
        description: str = Field("", description="what you're watching for (shown in the alert)")
        interval_minutes: int = Field(60, description="how often to check, minutes (min 5)")

    def __init__(self, store: WatchStore, session_id: str):
        self.store = store
        self.session_id = session_id

    async def run(self, args: "WatchTool.Params") -> str:
        url = args.url.strip()
        if not re.match(r"^https?://", url, re.I):
            return "watch error: give a full http(s) URL to watch."
        if not await asyncio.to_thread(url_fetch_ok, url):
            return ("watch error: that URL isn't allowed — it must be a PUBLIC http(s) address, "
                    "not an internal, loopback, or private-network host.")
        w = self.store.add(url, args.description.strip(), self.session_id,
                           args.interval_minutes, time.time())
        return (f"watch: now watching {w['url']} every {w['interval_minutes']} min "
                f"(id {w['id']}). I'll message you when it changes.")


class ListWatchesTool(Tool):
    name = "list_watches"
    description = "List the things you're currently watching for changes. No arguments."

    class Params(BaseModel):
        pass

    def __init__(self, store: WatchStore, session_id: str):
        self.store = store
        self.session_id = session_id

    async def run(self, args: "ListWatchesTool.Params") -> str:
        ws = [w for w in self.store.list(self.session_id) if w.get("active")]
        if not ws:
            return "You aren't watching anything right now."
        return "Currently watching:\n" + "\n".join(
            f"  {w['id']}: {w['url']} (every {w['interval_minutes']}m)"
            f"{' — ' + w['description'] if w.get('description') else ''}" for w in ws)


class UnwatchTool(Tool):
    name = "unwatch"
    description = "Stop watching something, by its watch id (see list_watches). Arg: id."

    class Params(BaseModel):
        id: str = Field(..., description="the watch id to stop")

    def __init__(self, store: WatchStore, session_id: str):
        self.store = store
        self.session_id = session_id

    async def run(self, args: "UnwatchTool.Params") -> str:
        return (f"unwatch: stopped watch {args.id}."
                if self.store.remove(args.id, self.session_id)
                else f"unwatch: no watch '{args.id}' belonging to you.")
