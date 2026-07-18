import httpx
import pytest

from engine.tools.web_search import WebSearchTool


@pytest.fixture(autouse=True)
def clear_state():
    WebSearchTool._cache.clear()
    WebSearchTool._calls.clear()
    yield
    WebSearchTool._cache.clear()
    WebSearchTool._calls.clear()


@pytest.fixture
def counting_transport(monkeypatch):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(200, json={"results": [{"title": "T", "url": "u", "content": "c"}]})

    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *a, **k):
        k["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **k)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    return calls


async def test_cache_avoids_second_api_call(counting_transport):
    t = WebSearchTool("http://x", cache_ttl=900, max_per_min=100)
    a = await t.run(t.Params(query="raspberry pi", n=3))
    b = await t.run(t.Params(query="Raspberry Pi", n=3))   # same (case-insensitive)
    assert counting_transport["n"] == 1                    # only one real API call
    assert "cached" in b


async def test_rate_limit_blocks_excess(counting_transport):
    t = WebSearchTool("http://x", cache_ttl=0, max_per_min=2)  # ttl 0 -> no cache hits
    await t.run(t.Params(query="q1"))
    await t.run(t.Params(query="q2"))
    out = await t.run(t.Params(query="q3"))                # 3rd within the minute
    assert "rate-limited" in out.lower()
    assert counting_transport["n"] == 2                    # the 3rd never hit the API
