import asyncio

from engine.tools.time_tool import TimeTool


def test_time_returns_iso_with_year():
    t = TimeTool()
    out = asyncio.run(t.run(t.Params()))
    assert "20" in out  # contains a year like 2026
    assert "T" in out   # ISO-8601 separator


def test_time_bad_timezone_falls_back():
    t = TimeTool()
    out = asyncio.run(t.run(t.Params(timezone="Not/AZone")))
    assert "20" in out  # does not crash; returns a time anyway
