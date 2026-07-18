import httpx
import pytest

from config import Config
from engine.engine import DEFAULT_SOUL, Engine
from engine.protocol import ModelResponse
from backend.app import create_app


class _CaptureModel:
    """Records the system prompt it was sent; replies with a fixed final answer."""
    last_system = None
    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None, reasoning=None):
        _CaptureModel.last_system = next((m["content"] for m in messages
                                          if m.get("role") == "system"), "")
        return ModelResponse(content="ok", finish_reason="stop")


def _engine(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="")
    e = Engine(cfg)
    e._model_client = lambda: _CaptureModel()
    e._soul_file = tmp_path / "SOUL.md"
    e._system_prompt_file = tmp_path / "system_prompt.md"
    e.soul = DEFAULT_SOUL
    return e


def test_default_soul_when_no_file(tmp_path):
    e = _engine(tmp_path)
    assert "Personality" in e.get_soul()


def test_set_soul_persists_and_reloads(tmp_path):
    e = _engine(tmp_path)
    e.set_soul("You are a laconic pirate.")
    assert e.get_soul() == "You are a laconic pirate."
    assert (tmp_path / "SOUL.md").read_text().strip() == "You are a laconic pirate."


async def test_soul_is_injected_into_system_prompt(tmp_path):
    e = _engine(tmp_path)
    e.set_soul("SOUL-MARKER-XYZ persona line.")
    await e.run_task("s", "hello")
    assert "SOUL-MARKER-XYZ" in _CaptureModel.last_system
    assert "Argus" in _CaptureModel.last_system  # operational base prompt still present


def test_system_prompt_migrates_from_legacy_txt(tmp_path):
    (tmp_path / "system_prompt.txt").write_text("legacy prompt text")
    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="")
    e = Engine(cfg)
    e._system_prompt_file = tmp_path / "system_prompt.md"     # .md absent
    e._system_prompt_legacy = tmp_path / "system_prompt.txt"  # legacy present
    assert e._load_system_prompt() == "legacy prompt text"


@pytest.fixture
def client(tmp_path, monkeypatch):
    e = _engine(tmp_path)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(e)),
                             base_url="http://t"), e


async def test_soul_endpoints(client):
    c, e = client
    async with c:
        assert (await c.get("/soul")).json()["soul"]
        r = await c.put("/soul", json={"soul": "friendly and brief"})
        assert r.json()["saved"] is True
        assert (await c.get("/soul")).json()["soul"] == "friendly and brief"
