"""Shared SSRF-safe outbound fetch, used by any tool that fetches an agent/user-supplied URL
(the watcher, download_file). Requires http(s); the host must satisfy engine.sandbox.egress_policy,
which DNS-resolves the host and rejects if ANY resolved address is private/loopback/link-local/
reserved/multicast (defeats hostnames that point at internal IPs — cloud metadata, the Argus
server, LAN devices). This is the same resolving policy tool_creation.url_is_safe now delegates to,
so a created tool and a fetched watch/download are held to one standard, not two. Redirects are NOT
auto-followed; each hop is re-validated. (Residual: a rebind between this resolve and httpx's own
connect is a narrow TOCTOU; no-auto-redirect + per-hop re-check closes the common vectors.)
"""
from __future__ import annotations

import asyncio
from urllib.parse import urljoin

import httpx

_MAX_REDIRECTS = 5


class BlockedURLError(ValueError):
    """Raised when a URL/host is rejected by the SSRF guard."""


def url_fetch_ok(url: str) -> bool:
    """http(s) only, and EVERY DNS-resolved address of the host must be public."""
    from engine.sandbox.egress_policy import url_allowed
    return url_allowed(url)[0]


async def safe_fetch(url: str, *, timeout: float = 20.0, max_bytes: int | None = None,
                     headers: dict | None = None) -> httpx.Response:
    """SSRF-guarded GET: validate the host before the request and re-validate every redirect hop
    (auto-follow off). Returns the final httpx.Response. Raises BlockedURLError on a blocked host,
    ValueError on too-many-redirects or an over-large body."""
    if not await asyncio.to_thread(url_fetch_ok, url):
        raise BlockedURLError(f"blocked non-public URL: {url}")
    hdrs = {"User-Agent": "Argus/1.0", **(headers or {})}
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as c:
        current = url
        for _ in range(_MAX_REDIRECTS):
            r = await c.get(current, headers=hdrs)
            loc = r.headers.get("location")
            if r.is_redirect and loc:
                current = urljoin(current, loc)
                if not await asyncio.to_thread(url_fetch_ok, current):
                    raise BlockedURLError(f"blocked redirect to non-public URL: {current}")
                continue
            if max_bytes is not None:
                clen = r.headers.get("content-length")
                if (clen and clen.isdigit() and int(clen) > max_bytes) or len(r.content) > max_bytes:
                    raise ValueError(f"file too large (limit {max_bytes} bytes)")
            return r
        raise ValueError("too many redirects")
