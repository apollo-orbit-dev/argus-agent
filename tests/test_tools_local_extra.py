"""Tests for the four network-free local tools: unit_convert, time_in_zone,
random_tool, text_tools. Each covers happy paths plus at least one error path."""
import asyncio
import random

from engine.tools.unit_convert import UnitConvertTool
from engine.tools.time_tools import TimeInZoneTool
from engine.tools.random_tools import RandomTool
from engine.tools.text_tools import TextTool


def _run(tool, **kwargs):
    return asyncio.run(tool.run(tool.Params(**kwargs)))


# --- unit_convert ---------------------------------------------------------

def test_unit_convert_temperature():
    t = UnitConvertTool()
    assert _run(t, value=100, from_unit="F", to_unit="C") == "100 F = 37.78 C"
    assert _run(t, value=0, from_unit="C", to_unit="K") == "0 C = 273.15 K"


def test_unit_convert_length_and_data():
    t = UnitConvertTool()
    assert _run(t, value=1, from_unit="km", to_unit="m") == "1 km = 1000 m"
    assert _run(t, value=1, from_unit="GB", to_unit="MB") == "1 GB = 1000 MB"


def test_unit_convert_cross_category_error():
    t = UnitConvertTool()
    out = _run(t, value=5, from_unit="kg", to_unit="m")
    assert "error" in out.lower()
    assert "cannot convert" in out.lower()


def test_unit_convert_unknown_unit_error():
    t = UnitConvertTool()
    out = _run(t, value=5, from_unit="furlong", to_unit="m")
    assert "error" in out.lower()
    assert "unknown" in out.lower()


# --- time_in_zone ---------------------------------------------------------

def test_time_in_zone_city():
    t = TimeInZoneTool()
    out = _run(t, location="Tokyo")
    assert "Asia/Tokyo" in out
    assert "error" not in out.lower()


def test_time_in_zone_iana():
    t = TimeInZoneTool()
    out = _run(t, location="Europe/Paris")
    assert "Europe/Paris" in out


def _mock_geocode(monkeypatch, results):
    import httpx
    real_init = httpx.AsyncClient.__init__

    def handler(req):
        return httpx.Response(200, json={"results": results})

    def fake_init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)


def test_time_in_zone_geocodes_place_with_state(monkeypatch):
    # 'Athens, GA' must resolve to Georgia (Eastern), not Athens, Greece
    _mock_geocode(monkeypatch, [
        {"name": "Athens", "admin1": "Attica", "country": "Greece", "timezone": "Europe/Athens"},
        {"name": "Athens", "admin1": "Georgia", "country": "United States",
         "country_code": "US", "timezone": "America/New_York"},
    ])
    out = _run(TimeInZoneTool(), location="Athens, GA")
    assert "America/New_York" in out and "error" not in out.lower()
    assert "Georgia" in out


def test_time_in_zone_geocode_no_results_error(monkeypatch):
    _mock_geocode(monkeypatch, [])
    out = _run(TimeInZoneTool(), location="Narnia")
    assert "error" in out.lower() and "IANA" in out


# --- random_tool ----------------------------------------------------------

def test_random_dice_in_range():
    random.seed(0)
    t = RandomTool()
    for _ in range(20):
        out = _run(t, action="dice", sides=6)
        assert out.startswith("Rolled a d6:")
        val = int(out.rsplit(":", 1)[1])
        assert 1 <= val <= 6


def test_random_coin_and_number():
    random.seed(0)
    t = RandomTool()
    coin = _run(t, action="coin")
    assert coin.split(":", 1)[1].strip() in {"heads", "tails"}
    num = _run(t, action="number", min=5, max=5)
    assert num.endswith("5")


def test_random_choice():
    random.seed(0)
    t = RandomTool()
    out = _run(t, action="choice", options=["a", "b", "c"])
    assert out.startswith("Chose:")
    assert out.rsplit(":", 1)[1].strip() in {"a", "b", "c"}


def test_random_unknown_action_error():
    t = RandomTool()
    out = _run(t, action="teleport")
    assert "error" in out.lower()
    assert "unknown action" in out.lower()


def test_random_empty_choice_error():
    t = RandomTool()
    out = _run(t, action="choice", options=[])
    assert "error" in out.lower()


# --- text_tools -----------------------------------------------------------

def test_text_transforms():
    t = TextTool()
    assert _run(t, action="upper", text="hi") == "HI"
    assert _run(t, action="lower", text="HI") == "hi"
    assert _run(t, action="reverse", text="abc") == "cba"
    assert _run(t, action="title", text="hello world") == "Hello World"


def test_text_count():
    t = TextTool()
    out = _run(t, action="count", text="one two three")
    assert "3 words" in out
    assert "13 characters" in out


def test_text_base64_roundtrip():
    t = TextTool()
    enc = _run(t, action="base64_encode", text="hello")
    assert enc == "aGVsbG8="
    assert _run(t, action="base64_decode", text=enc) == "hello"


def test_text_bad_base64_error():
    t = TextTool()
    out = _run(t, action="base64_decode", text="not!valid!base64!")
    assert "error" in out.lower()


def test_text_unknown_action_error():
    t = TextTool()
    out = _run(t, action="explode", text="x")
    assert "error" in out.lower()
    assert "unknown action" in out.lower()
