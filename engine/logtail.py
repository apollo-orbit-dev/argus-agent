"""Tail the server log file for the dashboard "Logs" page — a bounded initial read plus an async
stream of newly-appended lines (a `tail -f` the browser can consume over SSE)."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator


def tail_lines(path: str, lines: int = 200, max_bytes: int = 262_144) -> list[str]:
    """Return the last `lines` lines of the file, reading at most `max_bytes` from the end so a huge
    log never loads wholesale. Returns [] if the file is missing/unreadable."""
    try:
        p = Path(path)
        size = p.stat().st_size
        with open(p, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()            # drop the partial first line
            data = f.read()
        text = data.decode("utf-8", "replace")
        return text.splitlines()[-lines:]
    except OSError:
        return []


async def stream_lines(path: str, initial: int = 200, poll: float = 1.0) -> AsyncIterator[list[str]]:
    """Yield the recent tail once, then yield lists of newly-appended lines as the file grows.
    Handles truncation/rotation (if the file shrinks, re-read from the start)."""
    yield tail_lines(path, initial)
    pos = 0
    try:
        pos = os.path.getsize(path)
    except OSError:
        pos = 0
    buf = ""
    while True:
        await asyncio.sleep(poll)
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size < pos:                  # rotated/truncated → start over
            pos, buf = 0, ""
        if size == pos:
            continue
        try:
            with open(path, "rb") as f:
                f.seek(pos)
                chunk = f.read()
                pos = f.tell()
        except OSError:
            continue
        buf += chunk.decode("utf-8", "replace")
        parts = buf.split("\n")
        buf = parts.pop()               # keep the trailing partial line for next round
        new = [ln for ln in parts if ln]
        if new:
            yield new
