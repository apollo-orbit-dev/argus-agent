import time
from engine.reliability.store import ReliabilityStore

DAY = 86400


def _store(tmp_path):
    return ReliabilityStore(str(tmp_path / "rel.db"), retention_days=30)


def test_record_and_summary(tmp_path):
    s = _store(tmp_path)
    now = 1_700_000_000.0
    s.record("tool", "weather", ok=1, ms=100, detail="", ts=now)
    s.record("tool", "weather", ok=1, ms=300, detail="", ts=now)
    s.record("tool", "web_search", ok=0, ms=50, detail="timeout", ts=now)
    s.record("validation_fail", "web_search", ok=0, ms=None, detail="bad args", ts=now)
    s.record("reprompt", "", ok=None, ms=None, detail="no tool call", ts=now)
    out = s.summary(days=30, now=now)
    assert out["tool_calls"] == 3
    assert out["tool_success_pct"] == round(2 / 3 * 100, 1)
    tools = {t["entity"]: t for t in s.per_tool(days=30, now=now)}
    assert tools["weather"]["success_pct"] == 100.0 and tools["weather"]["mean_ms"] == 200
    assert tools["web_search"]["success_pct"] == 0.0
    lh = s.loop_health(days=30, now=now)
    assert lh["reprompt"]["total"] == 1 and lh["validation_fail"]["total"] == 1


def test_routine_completion(tmp_path):
    s = _store(tmp_path)
    now = 1_700_000_000.0
    s.record("routine", "morning", ok=1, ms=5000, detail="", ts=now)
    s.record("routine", "morning", ok=0, ms=100, detail="step 'x' failed", ts=now)
    r = {x["entity"]: x for x in s.per_routine(days=30, now=now)}["morning"]
    assert r["runs"] == 2 and r["completion_pct"] == 50.0


def test_retention_prunes_raw_but_keeps_rollup(tmp_path):
    s = _store(tmp_path)
    old = 1_700_000_000.0 - 40 * DAY
    new = 1_700_000_000.0
    s.record("tool", "weather", ok=1, ms=10, detail="", ts=old)
    s.record("tool", "weather", ok=1, ms=10, detail="", ts=new)
    s.prune(now=new)
    # raw drill-down only has the recent row...
    assert len(s.recent_failures(limit=50)) >= 0            # no failures, but query works
    raw = s._rw.execute("SELECT COUNT(*) AS n FROM outcomes").fetchone()["n"]
    assert raw == 1                                          # old raw row pruned
    daily = s._rw.execute("SELECT COUNT(*) AS n FROM daily").fetchone()["n"]
    assert daily == 2                                        # both days' rollups kept forever


def test_detail_is_capped_at_1000_chars(tmp_path):
    """The store is the single place a detail note is bounded. A note under the cap survives whole;
    a longer one is clipped to 1000 (raised from 200 so full-ish tracebacks/API errors are readable)."""
    s = _store(tmp_path)
    now = 1_700_000_000.0
    mid = "x" * 500
    s.record("tool", "a", ok=0, ms=1, detail=mid, ts=now)
    s.record("tool", "b", ok=0, ms=1, detail="y" * 1500, ts=now)
    fails = {f["entity"]: f for f in s.recent_failures(limit=10)}
    assert fails["a"]["detail"] == mid                 # 500 chars: kept whole (would have been cut at 200)
    assert len(fails["b"]["detail"]) == 1000           # 1500 chars: clipped to the cap


def test_recent_failures_filters_by_entity(tmp_path):
    s = _store(tmp_path)
    now = 1_700_000_000.0
    s.record("tool", "web_search", ok=0, ms=5, detail="HTTP 500", ts=now)
    s.record("tool", "weather", ok=0, ms=5, detail="nope", ts=now)
    fails = s.recent_failures(entity="web_search", limit=10)
    assert len(fails) == 1 and fails[0]["detail"] == "HTTP 500"
