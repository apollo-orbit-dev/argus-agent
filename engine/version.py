"""Single source of truth for the Argus version: read it from pyproject.toml so there's one place
to bump it (the packaging manifest), and expose it to the API/dashboard. Also a lightweight
"is there a newer release on GitHub?" check for the dashboard update indicator."""
from __future__ import annotations

import logging
import re
import time
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Optional

log = logging.getLogger("argus.version")

GITHUB_REPO = "apollo-orbit-dev/argus-agent"
_UPDATE_CACHE_TTL = 1800.0          # re-check GitHub at most every 30 min
_update_cache: dict = {"at": 0.0, "data": None}


@lru_cache(maxsize=1)
def get_version() -> str:
    try:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        return (data.get("project", {}).get("version")
                or data.get("tool", {}).get("poetry", {}).get("version")
                or "0.0.0")
    except Exception:
        return "0.0.0"


def _parse(v: str) -> Optional[tuple]:
    """'v0.7.1' / '0.7.1' -> (0, 7, 1). None if it doesn't look like a version."""
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", (v or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


def compare_versions(current: str, latest: Optional[str]) -> dict:
    """Pure comparison (no network). update_available only when both parse and latest > current."""
    cur, lat = _parse(current), _parse(latest or "")
    available = bool(cur and lat and lat > cur)
    return {"current": current, "latest": latest, "update_available": available}


async def check_for_update(current: Optional[str] = None, timeout: float = 6.0) -> dict:
    """Ask GitHub for the latest published release and compare. Cached ~30 min; degrades gracefully
    (never raises — a network/parse failure just reports no update available)."""
    current = current or get_version()
    now = time.time()
    if _update_cache["data"] and (now - _update_cache["at"]) < _UPDATE_CACHE_TTL:
        cached = dict(_update_cache["data"])
        cached.update(compare_versions(current, cached.get("latest")))  # re-eval vs current (cheap)
        return cached
    result = {"current": current, "latest": None, "update_available": False, "url": None}
    try:
        import httpx
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code == 200:
            data = resp.json()
            latest = data.get("tag_name")
            result.update(compare_versions(current, latest))
            result["url"] = data.get("html_url") or f"https://github.com/{GITHUB_REPO}/releases"
        else:
            log.info("update check: GitHub returned %s", resp.status_code)
    except Exception as e:                       # noqa: BLE001 - update check must never break /status
        log.info("update check failed: %s", e)
    _update_cache["at"], _update_cache["data"] = now, dict(result)
    return result
