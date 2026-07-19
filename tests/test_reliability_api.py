# tests/test_reliability_api.py — follow the existing TestClient pattern in tests/test_config_admin.py
import asyncio
import time
import httpx
from httpx import ASGITransport
from config import Config
from engine.engine import Engine
from backend.app import create_app


def _client(tmp_path, enabled=True, admin_token=""):
    eng = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                        admin_token=admin_token, enable_reliability=enabled),
                 data_dir=str(tmp_path))
    app = create_app(eng)
    return eng, httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def test_summary_endpoint(tmp_path):
    async def go():
        eng, c = _client(tmp_path)
        from engine.events import StepEvent
        # Real (current) timestamps, not the epoch — ReliabilityStore.summary() buckets by calendar
        # day off time.time(), so an event stamped near 1970 would fall outside ANY "last N days" window.
        now = time.time()
        await eng.events.publish(StepEvent("r", "s", 1, "tool_call", {"tool": "weather"}, now))
        await eng.events.publish(StepEvent("r", "s", 1, "tool_result", {"tool": "weather", "ok": True}, now + 0.1))
        r = await c.get("/reliability/summary")
        assert r.status_code == 200
        assert r.json()["enabled"] and r.json()["tool_calls"] == 1
        await c.aclose()
    asyncio.run(go())


def test_disabled_returns_enabled_false(tmp_path):
    async def go():
        eng, c = _client(tmp_path, enabled=False)
        r = await c.get("/reliability/summary")
        assert r.status_code == 200 and r.json()["enabled"] is False
        await c.aclose()
    asyncio.run(go())


def test_tools_routines_loop_failures_endpoints(tmp_path):
    async def go():
        eng, c = _client(tmp_path)
        from engine.events import StepEvent
        now = time.time()
        await eng.events.publish(StepEvent("r", "s", 1, "tool_call", {"tool": "weather"}, now))
        await eng.events.publish(StepEvent("r", "s", 1, "tool_result", {"tool": "weather", "ok": False}, now + 0.1))
        r = await c.get("/reliability/tools")
        assert r.status_code == 200 and isinstance(r.json(), list)
        r = await c.get("/reliability/routines")
        assert r.status_code == 200 and isinstance(r.json(), list)
        r = await c.get("/reliability/loop")
        assert r.status_code == 200 and isinstance(r.json(), dict)
        r = await c.get("/reliability/failures")
        assert r.status_code == 200 and isinstance(r.json(), list)
        r = await c.get("/reliability/failures", params={"entity": "weather", "limit": 5})
        assert r.status_code == 200
        await c.aclose()
    asyncio.run(go())


def test_admin_token_gates_reliability_endpoints(tmp_path):
    async def go():
        eng, c = _client(tmp_path, admin_token="s3cret")
        for path in ("/reliability/summary", "/reliability/tools", "/reliability/routines",
                     "/reliability/loop", "/reliability/failures"):
            r = await c.get(path)
            assert r.status_code == 401
            r = await c.get(path, headers={"X-Admin-Token": "s3cret"})
            assert r.status_code == 200
        await c.aclose()
    asyncio.run(go())
