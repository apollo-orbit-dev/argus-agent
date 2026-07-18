"""fetch_page — extract clean page content via the existing Firecrawl instance.

Firecrawl v2: POST {base}/v2/scrape {"url","formats":["markdown"]}
           -> {"success": true, "data": {"markdown": "...", "metadata": {...}}}.
"""
from __future__ import annotations

import httpx
from pydantic import BaseModel, Field

from engine.tools.base import Tool

_CONTENT_CAP = 4000


class FetchPageTool(Tool):
    name = "fetch_page"
    description = ("Fetch a web page by URL and return its main content as clean text/markdown. "
                   "Use after web_search to read a specific result. Long pages are truncated.")

    class Params(BaseModel):
        url: str = Field(..., description="The full URL of the page to fetch (http/https)")

    def __init__(self, base_url: str, timeout: float = 45.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    async def run(self, args: "FetchPageTool.Params") -> str:
        endpoint = f"{self.base_url}/v2/scrape"
        payload = {"url": args.url, "formats": ["markdown"]}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(endpoint, json=payload)
        except httpx.HTTPError as e:
            return f"fetch_page error: could not reach scraper ({e})"
        if r.status_code != 200:
            return f"fetch_page error: scraper returned HTTP {r.status_code}"
        try:
            body = r.json()
        except Exception as e:
            return f"fetch_page error: could not parse scraper response ({e})"
        if not body.get("success", False):
            return f"fetch_page error: scraper reported failure for {args.url!r}"
        data = body.get("data") or {}
        markdown = (data.get("markdown") or "").strip()
        if not markdown:
            return f"fetch_page: no readable content extracted from {args.url!r}."
        title = (data.get("metadata") or {}).get("title", "")
        header = f"# Fetched: {title or args.url}\n(source: {args.url})\n\n"
        if len(markdown) > _CONTENT_CAP:
            markdown = markdown[:_CONTENT_CAP] + f"\n\n…[truncated; {len(markdown)} chars total]"
        return header + markdown
