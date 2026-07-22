"""Reachability probes must be CHEAP.

The SearXNG probe once ran a real search (`/search?q=ping`), which SearXNG forwards to every
configured engine — including metered ones like Brave. The dashboard polls /status every 5 seconds,
so an open browser tab spent ~720 real search-API calls an hour of the owner's paid quota just to
render a green dot. These tests pin that a probe never issues billable work.
"""
import httpx
import pytest

from engine import status as status_mod


@pytest.fixture
def captured(monkeypatch):
    """Record every URL the probes request, and answer them all with a 200."""
    seen: list[str] = []
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *a, **kw):
        def handler(req):
            seen.append(str(req.url))
            return httpx.Response(200, json={})
        kw["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    return seen


async def test_searxng_probe_does_not_run_a_search(captured):
    await status_mod.check_all("http://model/v1", "m", "http://searx:8080", "")
    searx = [u for u in captured if "searx" in u]
    assert searx == ["http://searx:8080/healthz"]
    assert not any("/search" in u for u in searx), \
        "a reachability probe must never issue a billable search"
    assert not any("q=" in u for u in searx), "no query may be sent to the engines"


async def test_every_probe_is_a_plain_get_with_no_query_string(captured):
    """Blanket guard: whatever endpoints these move to later, none of them may carry a query —
    that's what turns a health check into metered work."""
    await status_mod.check_all("http://model/v1", "m", "http://searx:8080",
                               "http://firecrawl:3002", "http://embed/v1", "e")
    assert captured, "expected the configured dependencies to be probed"
    for url in captured:
        assert "?" not in url, f"probe URL carries a query string: {url}"


async def test_unconfigured_dependencies_are_not_probed_at_all(captured):
    out = await status_mod.check_all("http://model/v1", "m", "", "")
    assert not any("searx" in u for u in captured)
    assert out["searxng"] == {"configured": False, "reachable": False, "url": ""}


async def test_a_404_still_counts_as_reachable(monkeypatch):
    """An older SearXNG without /healthz answers 404. The server responded, so it is up — the probe
    must not report a false outage over a missing health endpoint."""
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(lambda req: httpx.Response(404, text="not found"))
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    out = await status_mod.check_all("http://model/v1", "m", "http://searx:8080", "")
    assert out["searxng"]["reachable"] is True
    assert out["searxng"]["status"] == 404
