import json

import httpx
import pytest

from engine.tools.weather import WeatherTool
from engine.tools.wikipedia import WikipediaTool
from engine.tools.dictionary import DictionaryTool
from engine.tools.currency_convert import CurrencyConvertTool
from engine.tools.crypto_price import CryptoPriceTool
from engine.tools.geocode import GeocodeTool


@pytest.fixture(autouse=True)
def patch_asyncclient(monkeypatch):
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *a, **kw):
        kw["transport"] = patch_asyncclient.transport
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    yield


# ---- weather ----

async def test_weather_formats_summary():
    def handler(req):
        if "geocoding-api" in req.url.host:
            assert req.url.params["name"] == "Nashville"
            return httpx.Response(200, json={"results": [
                {"latitude": 36.16, "longitude": -86.78, "name": "Nashville",
                 "country": "United States"}]})
        assert req.url.params["latitude"] == "36.16"
        return httpx.Response(200, json={"current": {
            "temperature_2m": 25.3, "relative_humidity_2m": 60,
            "wind_speed_10m": 12.5, "weather_code": 1}})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    out = await WeatherTool().run(WeatherTool.Params(location="Nashville"))
    assert "Nashville" in out and "25.3°C" in out
    assert "60%" in out and "12.5" in out


async def test_weather_location_not_found():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={}))
    out = await WeatherTool().run(WeatherTool.Params(location="Zzxqville"))
    assert "not found" in out.lower()


async def test_weather_http_error():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(503, text="down"))
    out = await WeatherTool().run(WeatherTool.Params(location="Nashville"))
    assert "error" in out.lower() and "503" in out


# ---- wikipedia ----

async def test_wikipedia_summary():
    def handler(req):
        assert "Python" in req.url.path
        return httpx.Response(200, json={
            "title": "Python (programming language)",
            "extract": "Python is a high-level programming language.",
            "content_urls": {"desktop": {"page": "https://en.wikipedia.org/wiki/Python"}}})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    out = await WikipediaTool().run(
        WikipediaTool.Params(query="Python (programming language)"))
    assert "high-level programming language" in out
    assert "https://en.wikipedia.org/wiki/Python" in out


async def test_wikipedia_falls_back_to_search():
    def handler(req):
        if "rest_v1" in req.url.path:
            return httpx.Response(404, json={"title": "Not found"})
        return httpx.Response(200, json={"query": {"search": [
            {"title": "Foobar", "snippet": "A <span>metasyntactic</span> variable."}]}})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    out = await WikipediaTool().run(WikipediaTool.Params(query="foobar xyz"))
    assert "Foobar" in out and "metasyntactic variable" in out
    assert "<span>" not in out  # HTML stripped


async def test_wikipedia_http_error():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(500, text="boom"))
    out = await WikipediaTool().run(WikipediaTool.Params(query="anything"))
    assert "error" in out.lower() and "500" in out


# ---- dictionary ----

async def test_dictionary_definitions():
    def handler(req):
        assert req.url.path.endswith("/serendipity")
        return httpx.Response(200, json=[{
            "word": "serendipity",
            "meanings": [{"partOfSpeech": "noun", "definitions": [
                {"definition": "The occurrence of events by chance in a happy way."},
                {"definition": "Good luck in making unexpected discoveries."},
                {"definition": "third def should be trimmed"}]}]}])
    patch_asyncclient.transport = httpx.MockTransport(handler)
    out = await DictionaryTool().run(DictionaryTool.Params(word="serendipity"))
    assert "serendipity" in out and "noun" in out
    assert "happy way" in out
    assert "third def" not in out  # only first 2 defs per meaning


async def test_dictionary_word_not_found():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(404, json={
            "title": "No Definitions Found",
            "message": "Sorry pal, we couldn't find definitions."}))
    out = await DictionaryTool().run(DictionaryTool.Params(word="asdfqwer"))
    assert "no definition found" in out.lower()


async def test_dictionary_http_error():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(502, text="bad"))
    out = await DictionaryTool().run(DictionaryTool.Params(word="serendipity"))
    assert "error" in out.lower() and "502" in out


# ---- currency_convert ----

async def test_currency_convert_formats():
    def handler(req):
        assert req.url.params["from"] == "USD"
        assert req.url.params["to"] == "EUR"
        return httpx.Response(200, json={
            "amount": 100.0, "base": "USD", "rates": {"EUR": 92.15}})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    out = await CurrencyConvertTool().run(CurrencyConvertTool.Params(
        amount=100, from_currency="usd", to_currency="eur"))
    assert "100 USD = 92.15 EUR" in out
    assert "rate 0.9215" in out


async def test_currency_convert_unknown_code():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(404, json={"message": "not found"}))
    out = await CurrencyConvertTool().run(CurrencyConvertTool.Params(
        amount=1, from_currency="usd", to_currency="zzz"))
    assert "error" in out.lower() and "unknown" in out.lower()


async def test_currency_convert_http_error():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(500, text="oops"))
    out = await CurrencyConvertTool().run(CurrencyConvertTool.Params(
        amount=1, from_currency="usd", to_currency="eur"))
    assert "error" in out.lower() and "500" in out


# ---- crypto_price ----

async def test_crypto_price_formats():
    def handler(req):
        assert req.url.params["ids"] == "bitcoin"
        assert req.url.params["vs_currencies"] == "usd"
        return httpx.Response(200, json={"bitcoin": {"usd": 64000.0}})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    out = await CryptoPriceTool().run(CryptoPriceTool.Params(
        coin="bitcoin", vs_currency="usd"))
    assert "Bitcoin" in out and "64,000.00" in out and "USD" in out


async def test_crypto_price_unknown_coin():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={}))
    out = await CryptoPriceTool().run(CryptoPriceTool.Params(coin="notacoin"))
    assert "no price found" in out.lower()


async def test_crypto_price_http_error():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(429, text="rate limited"))
    out = await CryptoPriceTool().run(CryptoPriceTool.Params(coin="bitcoin"))
    assert "error" in out.lower() and "429" in out


# ---- geocode ----

async def test_geocode_returns_parseable_json():
    """JSON, not prose. The output has two readers -- the model reading a tool result, and
    created-tool code calling geocode() through tool composition -- and only structured output
    serves both. A prose version shipped briefly and broke composition: `json.loads(geocode(...))`
    raised, the created tool fell into its except branch, and the model went back to hardcoding."""
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"results": [
            {"name": "Milton", "admin1": "Florida", "country": "United States",
             "latitude": 30.63241, "longitude": -87.03969, "timezone": "America/Chicago"}]}))
    out = await GeocodeTool().run(GeocodeTool.Params(location="Milton, FL"))
    place = json.loads(out)                      # must not raise
    assert place["latitude"] == 30.63241 and place["longitude"] == -87.03969
    assert place["name"] == "Milton" and place["admin1"] == "Florida"
    assert place["timezone"] == "America/Chicago"


async def test_geocode_uses_the_state_hint_to_disambiguate():
    """The reason this is a built-in rather than something each created tool re-implements:
    'Springfield, IL' must pick Illinois, not the first Springfield the API returns."""
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"results": [
            {"name": "Springfield", "admin1": "Massachusetts", "country": "United States",
             "latitude": 42.10, "longitude": -72.58, "timezone": "America/New_York"},
            {"name": "Springfield", "admin1": "Illinois", "country": "United States",
             "latitude": 39.80, "longitude": -89.64, "timezone": "America/Chicago"}]}))
    place = json.loads(await GeocodeTool().run(GeocodeTool.Params(location="Springfield, IL")))
    assert place["admin1"] == "Illinois" and place["latitude"] == 39.80


async def test_geocode_location_not_found_is_json_too():
    """Errors stay parseable: composed code does json.loads() unconditionally, so a bare prose
    error string would raise inside the caller instead of being handled."""
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, json={}))
    err = json.loads(await GeocodeTool().run(GeocodeTool.Params(location="Zzxqville")))
    assert "not found" in err["error"].lower()


async def test_geocode_http_error_is_reported_not_swallowed():
    """A 503 must not read as 'no such place' -- that would send the model off correcting a
    spelling that was never wrong."""
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(503, text="down"))
    err = json.loads(await GeocodeTool().run(GeocodeTool.Params(location="Nashville")))
    assert "not found" not in err["error"].lower()
