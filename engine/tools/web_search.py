"""web_search — query the existing SearXNG instance, return top-N results as text.

SearXNG JSON: GET {base}/search?q=<q>&format=json -> {"results":[{title,url,content},...]}.

Guarded to protect the backing search API's quota (SearXNG's Brave engine is metered):
a process-wide result CACHE (identical queries within a TTL don't re-hit the API) and a
rolling-window RATE LIMIT (caps calls/min, so a looping agent or scheduled task can't
burn the quota). Both are configurable.
"""
from __future__ import annotations

import time
from collections import deque

import httpx
from pydantic import BaseModel, Field

from engine.tools.base import Tool

_SNIPPET_CAP = 300


class WebSearchTool(Tool):
    name = "web_search"
    description = ("Search the web and return the top results (title, URL, snippet). "
                   "Use this to find pages relevant to a question; follow up with fetch_page "
                   "to read a specific result in full.")

    # process-wide guards (shared across instances)
    _cache: dict = {}                 # key -> (timestamp, result_text)
    _calls: deque = deque()           # timestamps of real API calls (rolling window)

    class Params(BaseModel):
        query: str = Field(..., description="The search query")
        n: int = Field(default=5, ge=1, le=10, description="How many results to return (1-10)")

    def __init__(self, base_url: str, timeout: float = 30.0,
                 cache_ttl: float = 900.0, max_per_min: int = 10):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl
        self.max_per_min = max_per_min

    def _rate_limited(self) -> bool:
        if self.max_per_min <= 0:          # 0 (or negative) = unlimited (rate limit off)
            return False
        now = time.time()
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        return len(self._calls) >= self.max_per_min

    async def run(self, args: "WebSearchTool.Params") -> str:
        key = f"{args.query.lower().strip()}|{args.n}"
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[0] < self.cache_ttl:
            return hit[1] + "\n(cached)"
        if self._rate_limited():
            return ("web_search: rate-limited to protect the search API quota "
                    f"(max {self.max_per_min}/min). Answer from what you have, or try again shortly.")

        url = f"{self.base_url}/search"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.get(url, params={"q": args.query, "format": "json"})
        except httpx.HTTPError as e:
            return f"web_search error: could not reach search service ({e})"
        self._calls.append(now)  # count the real API call
        if r.status_code != 200:
            return f"web_search error: search service returned HTTP {r.status_code}"
        try:
            results = r.json().get("results", [])
        except Exception as e:
            return f"web_search error: could not parse search response ({e})"
        if not results:
            out = f"web_search: no results found for {args.query!r}."
            self._cache[key] = (now, out)
            return out
        lines = [f"Top {min(args.n, len(results))} results for {args.query!r}:"]
        for i, res in enumerate(results[: args.n], 1):
            title = (res.get("title") or "(no title)").strip()
            link = (res.get("url") or "").strip()
            snippet = (res.get("content") or "").strip().replace("\n", " ")
            if len(snippet) > _SNIPPET_CAP:
                snippet = snippet[:_SNIPPET_CAP] + "…"
            lines.append(f"{i}. {title}\n   {link}\n   {snippet}")
        out = "\n".join(lines)
        self._cache[key] = (now, out)
        return out
