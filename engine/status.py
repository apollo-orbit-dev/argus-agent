"""Reachability checks for the model endpoint, SearXNG, and Firecrawl."""
from __future__ import annotations

import asyncio

import httpx


async def _probe(method: str, url: str, timeout: float = 6.0, **kw) -> dict:
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, url, **kw)
        return {"reachable": True, "status": r.status_code}
    except httpx.HTTPError as e:
        return {"reachable": False, "error": str(e)[:200]}


async def check_all(model_base_url: str, model_name: str,
                    searxng_base_url: str, firecrawl_base_url: str,
                    embedding_base_url: str = "", embedding_model: str = "") -> dict:
    # Only PROBE a dependency that's configured (URL set). An unset dependency reports
    # configured=False rather than being probed and shown as "unreachable" — it just isn't set up.
    urls = {
        "model": (model_base_url, f"{model_base_url.rstrip('/')}/models"),
        "searxng": (searxng_base_url, f"{searxng_base_url.rstrip('/')}/search?q=ping&format=json"),
        "firecrawl": (firecrawl_base_url, f"{firecrawl_base_url.rstrip('/')}/"),  # 404 = up
        "embedding": (embedding_base_url, f"{embedding_base_url.rstrip('/')}/models"),
    }
    configured = {k: v for k, v in urls.items() if (v[0] or "").strip()}
    probed = dict(zip(configured.keys(),
                      await asyncio.gather(*(_probe("GET", u[1]) for u in configured.values()))))
    names = {"model": model_name, "embedding": embedding_model}
    out = {}
    for key, (base, _) in urls.items():
        if key in probed:
            r = {**probed[key], "configured": True, "url": base}
        else:
            r = {"configured": False, "reachable": False, "url": ""}
        if key in names:
            r["name"] = names[key]
        out[key] = r
    return out
