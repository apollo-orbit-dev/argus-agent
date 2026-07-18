import httpx
import pytest

from config import Config
from engine.engine import Engine
from engine.protocol import ModelResponse
from backend.app import create_app


class _EchoModel:
    """Deterministic fake model: replies with the last user message, no network."""
    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None, reasoning=None):
        last_user = next((m["content"] for m in reversed(messages)
                          if m.get("role") == "user"), "")
        return ModelResponse(content=f"echo: {last_user}", finish_reason="stop")


def _engine():
    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="")
    eng = Engine(cfg)
    eng._model_client = lambda: _EchoModel()  # inject fake, avoid real network in unit tests
    return eng


@pytest.fixture
def client():
    app = create_app(_engine())
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_run_echoes(client):
    async with client:
        r = await client.post("/run", json={"session_id": "s1", "text": "hello"})
        assert r.status_code == 200
        assert "hello" in r.json()["answer"]


async def test_config_get_and_patch(client):
    async with client:
        r = await client.get("/config")
        assert r.status_code == 200
        assert r.json()["tool_calling_mode"] == "native"
        r = await client.patch("/config", json={"tool_calling_mode": "manual"})
        assert r.status_code == 200
        assert r.json()["tool_calling_mode"] == "manual"


async def test_status_shape(client, monkeypatch):
    async def fake_check_all(*a, **k):
        return {"model": {"reachable": True}, "searxng": {"reachable": True},
                "firecrawl": {"reachable": False}}
    monkeypatch.setattr("engine.status.check_all", fake_check_all)
    async with client:
        r = await client.get("/status")
        assert r.status_code == 200
        body = r.json()
        for k in ("model", "searxng", "firecrawl", "tool_calling_mode", "skill_selection_mode"):
            assert k in body


# Live SSE streaming is verified separately against a real uvicorn server
# (curl -N); httpx ASGITransport buffers streamed bodies, so we unit-test the
# SSE frame format here rather than assert over the in-process stream.
def test_sse_frame_format():
    from backend.app import _sse

    class E:
        def to_json(self):
            return {"kind": "final", "step": 1}

    frame = _sse(E())
    assert frame == 'data: {"kind": "final", "step": 1}\n\n'
