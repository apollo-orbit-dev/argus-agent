"""Update-check: version parsing/comparison (pure) + graceful failure of the GitHub check."""
import asyncio

import engine.version as V
from engine.version import _parse, check_for_update, compare_versions


def test_parse_strips_v_and_extracts():
    assert _parse("v0.7.1") == (0, 7, 1)
    assert _parse("0.7.1") == (0, 7, 1)
    assert _parse("v1.2.3-rc4") == (1, 2, 3)
    assert _parse("nonsense") is None
    assert _parse("") is None


def test_compare_update_available_only_when_latest_newer():
    assert compare_versions("0.7.1", "v0.7.2")["update_available"] is True
    assert compare_versions("0.7.1", "v0.7.1")["update_available"] is False
    assert compare_versions("0.7.1", "v0.7.0")["update_available"] is False
    assert compare_versions("0.7.1", "v1.0.0")["update_available"] is True


def test_compare_handles_missing_or_garbage_latest():
    assert compare_versions("0.7.1", None)["update_available"] is False
    assert compare_versions("0.7.1", "garbage")["update_available"] is False
    # payload shape is stable
    r = compare_versions("0.7.1", "v0.7.2")
    assert r["current"] == "0.7.1" and r["latest"] == "v0.7.2"


def test_check_for_update_never_raises_on_network_failure(monkeypatch):
    V._update_cache["at"], V._update_cache["data"] = 0.0, None   # bust cache

    class _Boom:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): raise RuntimeError("no network")

    monkeypatch.setattr("httpx.AsyncClient", _Boom)
    r = asyncio.run(check_for_update("0.7.1"))
    assert r["update_available"] is False and r["current"] == "0.7.1" and r["latest"] is None


def test_check_for_update_uses_cache_and_reevaluates_vs_current(monkeypatch):
    # a fresh cache entry (latest=v0.7.5) is reused without a second fetch, and update_available is
    # re-evaluated against whatever `current` is passed in.
    import time
    calls = {"n": 0}

    class _Resp:
        status_code = 200
        def json(self):
            calls["n"] += 1
            return {"tag_name": "v0.7.5", "html_url": "u"}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    V._update_cache["at"], V._update_cache["data"] = 0.0, None
    r1 = asyncio.run(check_for_update("0.7.1"))          # fetches
    assert r1["update_available"] is True and calls["n"] == 1
    r2 = asyncio.run(check_for_update("0.7.9"))          # cache hit, re-eval vs newer current
    assert calls["n"] == 1                                # no second fetch
    assert r2["latest"] == "v0.7.5" and r2["update_available"] is False


def test_check_for_update_reports_available(monkeypatch):
    V._update_cache["at"], V._update_cache["data"] = 0.0, None

    class _Resp:
        status_code = 200
        def json(self):
            return {"tag_name": "v9.9.9", "html_url": "https://github.com/x/y/releases/tag/v9.9.9"}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, *a, **k): return _Resp()

    monkeypatch.setattr("httpx.AsyncClient", _Client)
    r = asyncio.run(check_for_update("0.7.1"))
    assert r["update_available"] is True and r["latest"] == "v9.9.9" and "v9.9.9" in r["url"]
