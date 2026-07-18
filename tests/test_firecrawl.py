"""map_site / crawl_site / extract_data — response parsing, SSRF guard, async polling."""
import asyncio
import json

from engine.tools.firecrawl import CrawlSiteTool, ExtractDataTool, MapSiteTool


async def _noop(*a, **k):
    return None


class _Resp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class _FakeClient:
    """Routes POST/GET to a script keyed by path; each key is a list consumed in order."""
    script = {}

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        return _Resp(self._take(url))

    async def get(self, url):
        return _Resp(self._take(url))

    def _take(self, url):
        for key, responses in _FakeClient.script.items():
            if url.endswith(key):
                return responses.pop(0) if len(responses) > 1 else responses[0]
        raise AssertionError(f"no scripted response for {url}")


def _patch(monkeypatch, script):
    _FakeClient.script = script
    monkeypatch.setattr("engine.tools.firecrawl.httpx.AsyncClient", _FakeClient)
    # public URL check → always allow in tests (no live DNS)
    monkeypatch.setattr("engine.tools.firecrawl.url_fetch_ok", lambda u: True)


async def test_map_site(monkeypatch):
    _patch(monkeypatch, {"/v2/map": [{"success": True, "links": [
        {"url": "https://x.com/a"}, {"url": "https://x.com/b"}, "https://x.com/c"]}]})
    out = await MapSiteTool("http://fc").run(MapSiteTool.Params(url="https://x.com"))
    assert "3 URL" in out and "https://x.com/a" in out and "https://x.com/c" in out


async def test_crawl_site_async_poll(monkeypatch):
    _patch(monkeypatch, {
        "/v2/crawl": [{"success": True, "id": "job1"}],          # start → async id
        "/v2/crawl/job1": [                                       # poll: scraping, then completed
            {"status": "scraping"},
            {"status": "completed", "data": [
                {"markdown": "# Page A\nhello", "metadata": {"sourceURL": "https://x.com/a"}},
                {"markdown": "# Page B\nworld", "metadata": {"sourceURL": "https://x.com/b"}}]}],
    })
    monkeypatch.setattr("engine.tools.firecrawl.asyncio.sleep", _noop)
    out = await CrawlSiteTool("http://fc").run(CrawlSiteTool.Params(url="https://x.com", limit=5))
    assert "Crawled 2 page" in out and "hello" in out and "https://x.com/b" in out


async def test_extract_data_async_poll(monkeypatch):
    _patch(monkeypatch, {
        "/v2/extract": [{"success": True, "id": "ex1"}],
        "/v2/extract/ex1": [{"status": "completed", "data": {"price": "$19.99", "rating": 4.5}}],
    })
    monkeypatch.setattr("engine.tools.firecrawl.asyncio.sleep", _noop)
    out = await ExtractDataTool("http://fc").run(
        ExtractDataTool.Params(urls=["https://x.com/p"], prompt="price and rating"))
    assert "19.99" in out and "rating" in out


async def test_ssrf_guard_blocks_internal(monkeypatch):
    # url_fetch_ok returns False → internal URL rejected before any request
    monkeypatch.setattr("engine.tools.firecrawl.url_fetch_ok", lambda u: False)
    m = await MapSiteTool("http://fc").run(MapSiteTool.Params(url="http://192.168.0.5"))
    assert "blocked" in m.lower()
    c = await CrawlSiteTool("http://fc").run(CrawlSiteTool.Params(url="http://127.0.0.1"))
    assert "blocked" in c.lower()
    e = await ExtractDataTool("http://fc").run(
        ExtractDataTool.Params(urls=["http://169.254.169.254"], prompt="x"))
    assert "blocked" in e.lower()


async def test_crawl_limit_capped(monkeypatch):
    sent = {}

    class _C(_FakeClient):
        async def post(self, url, json=None):
            sent["limit"] = json.get("limit")
            return _Resp({"success": True, "data": [{"markdown": "x", "metadata": {}}]})
    _FakeClient.script = {}
    monkeypatch.setattr("engine.tools.firecrawl.httpx.AsyncClient", _C)
    monkeypatch.setattr("engine.tools.firecrawl.url_fetch_ok", lambda u: True)
    await CrawlSiteTool("http://fc").run(CrawlSiteTool.Params(url="https://x.com", limit=999))
    assert sent["limit"] == 40                                   # capped at 40
