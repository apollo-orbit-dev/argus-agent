"""Shared SSRF-safe outbound fetch, used by any tool that fetches an agent/user-supplied URL
(the watcher, download_file). Requires http(s), DNS-resolves the host and rejects if ANY resolved
address is private/loopback/link-local/reserved/multicast (defeats hostnames that point at
internal IPs — cloud metadata, the Argus server, LAN devices — which the literal-only `url_is_safe`
misses). Redirects are NOT auto-followed; each hop is re-validated. (Residual: a rebind between this
resolve and httpx's own connect is a narrow TOCTOU; no-auto-redirect + per-hop re-check closes the
common vectors.)
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx

from engine.experimental.tool_creation import url_is_safe

_MAX_REDIRECTS = 5


class BlockedURLError(ValueError):
    """Raised when a URL/host is rejected by the SSRF guard."""


def _ip_blocked(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return True                          # unresolvable/garbage → block
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified)


def url_fetch_ok(url: str) -> bool:
    """http(s) only, and EVERY DNS-resolved address of the host must be public."""
    try:
        p = urlparse(str(url))
    except Exception:
        return False
    if p.scheme.lower() not in ("http", "https"):
        return False
    host = (p.hostname or "").lower().rstrip(".")
    if not host or not url_is_safe(url):     # scheme, blocked hostnames, IP-literal private ranges
        return False
    port = p.port or (443 if p.scheme.lower() == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except Exception:
        return False
    return bool(infos) and not any(_ip_blocked(info[4][0]) for info in infos)


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
