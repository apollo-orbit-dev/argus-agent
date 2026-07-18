"""wikipedia — summarize a topic from English Wikipedia (keyless REST API).

Try the REST summary endpoint first; on 404 fall back to the search API and
summarize the top hit.
"""
from __future__ import annotations

from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field

from engine.tools.base import Tool

_SUMMARY_URL = "https://en.wikipedia.org/api/rest_v1/page/summary/"
_SEARCH_URL = "https://en.wikipedia.org/w/api.php"
_EXTRACT_CAP = 600
# Wikipedia's API policy requires a descriptive User-Agent or it returns 403.
_HEADERS = {"User-Agent": "Argus-Agent-Testbed/1.0 (https://github.com/; keyless tool)"}


class WikipediaTool(Tool):
    name = "wikipedia"
    description = ("Look up a topic on Wikipedia and return a short summary with a link. "
                   "Use for factual/encyclopedic questions about people, places, or things.")

    class Params(BaseModel):
        query: str = Field(..., description="The topic or article title to look up")

    def __init__(self, timeout: float = 20.0):
        self.timeout = timeout

    @staticmethod
    def _format(title: str, extract: str, url: str) -> str:
        extract = (extract or "").strip()
        if len(extract) > _EXTRACT_CAP:
            extract = extract[:_EXTRACT_CAP].rstrip() + "…"
        parts = [f"{title}"]
        if extract:
            parts.append(extract)
        if url:
            parts.append(url)
        return "\n".join(parts)

    async def run(self, args: "WikipediaTool.Params") -> str:
        title = args.query.strip().replace(" ", "_")
        try:
            async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True,
                                         headers=_HEADERS) as c:
                r = await c.get(_SUMMARY_URL + quote(title, safe=""))
                if r.status_code == 200:
                    try:
                        data = r.json()
                    except Exception as e:
                        return f"wikipedia error: could not parse summary response ({e})"
                    url = (data.get("content_urls", {}).get("desktop", {}) or {}).get("page", "")
                    return self._format(data.get("title", args.query),
                                        data.get("extract", ""), url)
                if r.status_code != 404:
                    return f"wikipedia error: summary returned HTTP {r.status_code}"
                # 404 -> fall back to search
                s = await c.get(_SEARCH_URL, params={
                    "action": "query",
                    "list": "search",
                    "srsearch": args.query,
                    "format": "json",
                })
        except httpx.HTTPError as e:
            return f"wikipedia error: could not reach Wikipedia ({e})"
        if s.status_code != 200:
            return f"wikipedia error: search returned HTTP {s.status_code}"
        try:
            hits = s.json().get("query", {}).get("search", [])
        except Exception as e:
            return f"wikipedia error: could not parse search response ({e})"
        if not hits:
            return f"wikipedia: no article found for {args.query!r}."
        top = hits[0]
        top_title = top.get("title", args.query)
        import re
        snippet = re.sub(r"<[^>]+>", "", top.get("snippet", ""))
        url = "https://en.wikipedia.org/wiki/" + quote(top_title.replace(" ", "_"), safe="")
        return self._format(top_title, snippet, url)
