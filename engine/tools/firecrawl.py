"""map_site / crawl_site / extract_data — the rest of the Firecrawl v2 surface.

`fetch_page` already exposes `/v2/scrape` (one page → markdown). These add:
  - map_site   → POST /v2/map      : discover the URLs on a site (fast sitemap). Sync.
  - crawl_site → POST /v2/crawl    : crawl many pages → markdown (async: returns an id, poll it).
  - extract_data → POST /v2/extract: AI-structured extraction from pages (async: id, poll).

The TARGET url(s) the agent gives are SSRF-checked (public http(s) only) before we ask Firecrawl to
fetch them, so the scraper can't be steered at internal/LAN hosts.
"""
from __future__ import annotations

import asyncio
import json

import httpx
from pydantic import BaseModel, Field

from engine.tools.base import Tool
from engine.tools.net_guard import url_fetch_ok

_MAP_CAP = 150            # URLs returned by map_site
_CRAWL_CHARS = 40000      # total markdown returned by crawl_site
_CRAWL_MAX_PAGES = 40


async def _public(url: str) -> bool:
    return await asyncio.to_thread(url_fetch_ok, url)


class _Firecrawl:
    def __init__(self, base_url: str, timeout: float):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    async def post(self, path: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            r = await c.post(f"{self.base}{path}", json=payload)
        r.raise_for_status()
        return r.json()

    async def poll(self, path: str, max_wait: float, interval: float = 3.0) -> dict:
        """Poll an async job (crawl/extract) until it's done or times out."""
        waited = 0.0
        async with httpx.AsyncClient(timeout=self.timeout) as c:
            while True:
                r = await c.get(f"{self.base}{path}")
                r.raise_for_status()
                body = r.json()
                status = body.get("status")
                if status in ("completed", "failed", "cancelled", "error"):
                    return body
                if waited >= max_wait:
                    body["_timed_out"] = True
                    return body
                await asyncio.sleep(interval)
                waited += interval


class MapSiteTool(Tool):
    name = "map_site"
    description = ("Discover the URLs on a website — a fast sitemap. Use to see what pages exist "
                   "before fetching or crawling. Args: url; optional search (only URLs matching this "
                   "keyword).")

    class Params(BaseModel):
        url: str = Field(..., description="the site URL to map")
        search: str = Field("", description="optional keyword filter for the URLs")

    def __init__(self, base_url: str, timeout: float = 45.0):
        self.fc = _Firecrawl(base_url, timeout)

    async def run(self, args: "MapSiteTool.Params") -> str:
        if not await _public(args.url):
            return "map_site error: give a public http(s) site URL (internal hosts are blocked)."
        payload = {"url": args.url}
        if args.search.strip():
            payload["search"] = args.search.strip()
        try:
            body = await self.fc.post("/v2/map", payload)
        except Exception as e:
            return f"map_site error: {type(e).__name__}: {e}"
        links = body.get("links") or body.get("data") or []
        urls = [(l.get("url") if isinstance(l, dict) else str(l)) for l in links]
        urls = [u for u in urls if u]
        if not urls:
            return f"map_site: no URLs found for {args.url!r}."
        shown = urls[:_MAP_CAP]
        extra = f"\n… ({len(urls)} URLs total)" if len(urls) > _MAP_CAP else ""
        return f"Found {len(urls)} URL(s) on {args.url}:\n" + "\n".join(shown) + extra


class CrawlSiteTool(Tool):
    name = "crawl_site"
    description = ("Crawl multiple pages of a website starting from a URL and return their content as "
                  "markdown — good for reading a whole docs section (then add_to_knowledge). Args: "
                  "url; optional limit (max pages, default 15, cap 40). Can take a while.")

    class Params(BaseModel):
        url: str = Field(..., description="the site URL to crawl from")
        limit: int = Field(15, description="max pages to crawl (cap 40)")

    def __init__(self, base_url: str, timeout: float = 45.0):
        self.fc = _Firecrawl(base_url, timeout)

    async def run(self, args: "CrawlSiteTool.Params") -> str:
        if not await _public(args.url):
            return "crawl_site error: give a public http(s) site URL (internal hosts are blocked)."
        limit = max(1, min(int(args.limit), _CRAWL_MAX_PAGES))
        try:
            start = await self.fc.post("/v2/crawl", {"url": args.url, "limit": limit})
            data = start.get("data")
            if data is None:                                   # async job → poll it
                job_id = start.get("id") or start.get("jobId")
                if not job_id:
                    return f"crawl_site error: unexpected start response {start}"
                body = await self.fc.poll(f"/v2/crawl/{job_id}", max_wait=120.0)
                data = body.get("data") or []
                if body.get("_timed_out"):
                    data = data  # return whatever completed so far
        except Exception as e:
            return f"crawl_site error: {type(e).__name__}: {e}"
        pages = [d for d in (data or []) if isinstance(d, dict)]
        if not pages:
            return f"crawl_site: no pages returned for {args.url!r}."
        out, total = [], 0
        for p in pages:
            md = (p.get("markdown") or "").strip()
            src = (p.get("metadata") or {}).get("sourceURL") or (p.get("metadata") or {}).get("url") or ""
            chunk = f"\n\n===== {src} =====\n{md}"
            if total + len(chunk) > _CRAWL_CHARS:
                out.append(f"\n\n…[truncated; {len(pages)} pages crawled, showing the first {len(out)}]")
                break
            out.append(chunk)
            total += len(chunk)
        return f"Crawled {len(pages)} page(s) from {args.url}:" + "".join(out)


class ExtractDataTool(Tool):
    name = "extract_data"
    description = ("Extract specific STRUCTURED information from one or more web pages using AI — "
                  "describe what you want in `prompt` (e.g. 'the product name, price, and rating'). "
                  "Args: urls (list of page URLs); prompt (what to pull out). Returns the extracted data.")

    class Params(BaseModel):
        urls: list[str] = Field(..., description="page URLs to extract from")
        prompt: str = Field(..., description="what information to extract")

    def __init__(self, base_url: str, timeout: float = 45.0):
        self.fc = _Firecrawl(base_url, timeout)

    async def run(self, args: "ExtractDataTool.Params") -> str:
        # NOTE: uses /v2/extract, which this Firecrawl build marks deprecated in favor of /v2/scrape
        # with a `json` format. It still works (async id + poll); revisit if the endpoint is removed.
        urls = [u for u in (args.urls or []) if u]
        if not urls:
            return "extract_data error: provide at least one URL."
        for u in urls:
            if not await _public(u):
                return f"extract_data error: '{u}' isn't a public http(s) URL (internal hosts blocked)."
        try:
            start = await self.fc.post("/v2/extract", {"urls": urls, "prompt": args.prompt})
            data = start.get("data")
            if data is None:                                   # async job → poll it
                job_id = start.get("id") or start.get("jobId")
                if not job_id:
                    return f"extract_data error: unexpected start response {start}"
                body = await self.fc.poll(f"/v2/extract/{job_id}", max_wait=90.0)
                if body.get("_timed_out"):
                    return "extract_data: the extraction took too long and timed out."
                data = body.get("data")
        except Exception as e:
            return f"extract_data error: {type(e).__name__}: {e}"
        if data in (None, {}, []):
            return "extract_data: nothing was extracted (try a clearer prompt or different pages)."
        return "Extracted data:\n" + json.dumps(data, indent=2, default=str)[:_CRAWL_CHARS]
