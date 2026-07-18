import httpx
import pytest

from engine.tools.web_search import WebSearchTool
from engine.tools.fetch_page import FetchPageTool


@pytest.fixture(autouse=True)
def patch_asyncclient(monkeypatch):
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *a, **kw):
        kw["transport"] = patch_asyncclient.transport
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    yield


# ---- web_search ----

async def test_web_search_formats_results():
    def handler(req):
        assert req.url.params["format"] == "json"
        assert req.url.params["q"] == "raspberry pi"
        return httpx.Response(200, json={"results": [
            {"title": "Pi 5", "url": "https://rpi.com/5", "content": "The Raspberry Pi 5 board."},
            {"title": "Pi 4", "url": "https://rpi.com/4", "content": "Older model."},
        ]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    out = await WebSearchTool("https://searx.example").run(
        WebSearchTool.Params(query="raspberry pi", n=1))
    assert "Pi 5" in out and "https://rpi.com/5" in out
    assert "Pi 4" not in out  # n=1 truncates


async def test_web_search_zero_results():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"results": []}))
    out = await WebSearchTool("https://searx.example").run(WebSearchTool.Params(query="zxqz"))
    assert "no results" in out.lower()


async def test_web_search_http_error():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(502, text="bad gateway"))
    out = await WebSearchTool("https://searx.example").run(WebSearchTool.Params(query="x"))
    assert "error" in out.lower() and "502" in out


# ---- fetch_page ----

async def test_fetch_page_returns_markdown():
    def handler(req):
        import json
        body = json.loads(req.content)
        assert body["formats"] == ["markdown"]
        return httpx.Response(200, json={"success": True, "data": {
            "markdown": "# Hello\n\nThis is the page body.",
            "metadata": {"title": "Hello Page"}}})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    out = await FetchPageTool("http://fc.example").run(
        FetchPageTool.Params(url="https://example.com"))
    assert "This is the page body" in out and "Hello Page" in out


async def test_fetch_page_truncates_long():
    big = "x" * 9000
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"success": True, "data": {"markdown": big}}))
    out = await FetchPageTool("http://fc.example").run(
        FetchPageTool.Params(url="https://example.com"))
    assert "truncated" in out.lower()


async def test_fetch_page_failure_flag():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"success": False, "data": {}}))
    out = await FetchPageTool("http://fc.example").run(
        FetchPageTool.Params(url="https://bad.example"))
    assert "error" in out.lower()
