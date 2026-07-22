"""Watcher: store CRUD + change detection (baseline → alert only on change)."""
import asyncio

from engine.tools.watch import (ListWatchesTool, UnwatchTool, WatchManager,
                                WatchStore, WatchTool, _normalize)


def test_normalize_strips_html():
    assert _normalize("<p>Hello   <b>world</b></p>") == "Hello world"
    assert "alert" not in _normalize("<script>var x='alert'</script>ok")


def test_store_crud(tmp_path):
    s = WatchStore(str(tmp_path / "w.json"))
    w = s.add("https://example.com", "price", "sess1", 30, now=100.0)
    assert w["interval_minutes"] == 30 and w["active"]
    assert len(s.list("sess1")) == 1 and s.list("other") == []
    assert s.remove(w["id"], "other") is False        # wrong session can't remove
    assert s.remove(w["id"], "sess1") is True
    assert s.list("sess1") == []


def test_min_interval_enforced(tmp_path):
    s = WatchStore(str(tmp_path / "w.json"))
    w = s.add("https://x.com", "", "s", 1, now=0.0)   # asked for 1 min
    assert w["interval_minutes"] == 5                 # clamped to 5


async def test_change_detection_baseline_then_alert(tmp_path):
    s = WatchStore(str(tmp_path / "w.json"))
    w = s.add("https://example.com", "watch it", "sess1", 5, now=0.0)
    alerts = []

    async def _deliver(session_id, text):
        alerts.append((session_id, text))
    mgr = WatchManager(s, deliver=_deliver)
    mgr._fetch = lambda url: _fake("<html>version one</html>")

    await mgr._check(s.get(w["id"]), now=1000.0)       # first check = baseline, no alert
    assert alerts == []
    assert s.get(w["id"])["last_hash"] is not None

    await mgr._check(s.get(w["id"]), now=2000.0)       # same content → no alert
    assert alerts == []

    mgr._fetch = lambda url: _fake("<html>version TWO changed</html>")
    await mgr._check(s.get(w["id"]), now=3000.0)       # changed → alert
    assert len(alerts) == 1
    assert alerts[0][0] == "sess1" and "watch it" in alerts[0][1]


def _fake(text):
    async def _c():
        return text
    return _c()


def test_url_fetch_ok_blocks_internal():
    """SSRF guard: internal/loopback/private/metadata hosts are rejected (no DNS needed for
    literals + blocked hostnames), and non-http schemes are rejected."""
    from engine.tools.watch import url_fetch_ok
    for bad in ("http://127.0.0.1/", "http://localhost:8700/", "http://169.254.169.254/",
                "http://192.168.1.100/", "http://10.0.0.5/", "http://[::1]/",
                "ftp://example.com/", "file:///etc/passwd", "http://0.0.0.0/"):
        assert url_fetch_ok(bad) is False, bad


def test_url_fetch_ok_rejects_hostname_resolving_to_private_address(monkeypatch):
    """Mutation guard for the headline fix: url_fetch_ok delegates to the resolving egress policy,
    so a hostname that DNS-resolves to a LAN address must be refused even though the literal name
    looks public. Reverting the resolving delegation to a literal-only check leaves this red."""
    import socket

    from engine.tools.watch import url_fetch_ok
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", ("192.168.0.93", 443))])
    assert url_fetch_ok("https://totally-innocent.example.com/") is False


async def test_fetch_rejects_internal_ip(tmp_path):
    """_fetch raises before any request when the URL host is internal (literal, no network)."""
    mgr = WatchManager(WatchStore(str(tmp_path / "w.json")))
    with_raises = False
    try:
        await mgr._fetch("http://169.254.169.254/latest/meta-data/")
    except ValueError as e:
        with_raises = "blocked" in str(e).lower()
    assert with_raises


def test_watch_tool_rejects_internal_url(tmp_path):
    s = WatchStore(str(tmp_path / "w.json"))
    t = WatchTool(s, "sess1")
    assert "error" in asyncio.run(t.run(t.Params(url="not-a-url", description="x"))).lower()
    out = asyncio.run(t.run(t.Params(url="http://127.0.0.1:8700/admin", description="x")))
    assert "isn't allowed" in out or "not be" in out.lower() or "error" in out.lower()
    assert s.list("sess1") == []                     # nothing registered


def test_watch_tool_accepts_public_url(tmp_path, monkeypatch):
    monkeypatch.setattr("engine.tools.watch.url_fetch_ok", lambda u: True)   # no live DNS in tests
    s = WatchStore(str(tmp_path / "w.json"))
    t = WatchTool(s, "sess1")
    ok = asyncio.run(t.run(t.Params(url="https://example.com", description="prices")))
    assert "watching" in ok.lower()
    assert "example.com" in asyncio.run(ListWatchesTool(s, "sess1").run(ListWatchesTool.Params()))
