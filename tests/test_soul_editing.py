"""update_soul / read_soul + the engine's soul backup + revert."""
import asyncio

from config import Config
from engine.engine import Engine
from engine.tools.soul import ReadSoulTool, UpdateSoulTool


def _engine(tmp_path):
    e = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""))
    e._soul_file = tmp_path / "SOUL.md"          # isolate from the repo's real SOUL.md
    e.soul = "You are a wise wizard."
    return e


async def test_read_soul_tool(tmp_path):
    e = _engine(tmp_path)
    t = ReadSoulTool(e.get_soul)
    assert "wise wizard" in await t.run(t.Params())


async def test_update_soul_takes_effect_and_persists(tmp_path):
    e = _engine(tmp_path)
    t = UpdateSoulTool(e.get_soul, e.set_soul)
    out = await t.run(t.Params(soul="You are concise and direct."))
    assert "updated" in out.lower()
    assert e.soul == "You are concise and direct."               # live effect
    assert (tmp_path / "SOUL.md").read_text() == "You are concise and direct."   # persisted


async def test_update_soul_backs_up_and_reverts(tmp_path):
    e = _engine(tmp_path)
    t = UpdateSoulTool(e.get_soul, e.set_soul)
    await t.run(t.Params(soul="New terse voice."))
    assert (tmp_path / "SOUL.md.bak").read_text() == "You are a wise wizard."   # previous backed up
    res = e.revert_soul()
    assert res["ok"] and e.soul == "You are a wise wizard."       # reverted live


async def test_update_soul_rejects_empty_and_too_long(tmp_path):
    e = _engine(tmp_path)
    t = UpdateSoulTool(e.get_soul, e.set_soul, max_len=50)
    assert "empty" in (await t.run(t.Params(soul="   "))).lower()
    assert "too long" in (await t.run(t.Params(soul="x" * 60))).lower()
    assert e.soul == "You are a wise wizard."                     # unchanged after rejections


async def test_revert_with_no_backup(tmp_path):
    e = _engine(tmp_path)
    assert e.revert_soul()["ok"] is False                         # nothing to revert to yet


def test_soul_tools_registered_by_default():
    """read_soul is a vetted global; update_soul is now built PER-RUN (approval-aware, bound to
    this turn's session/run/origin — Task 7), so it no longer lives in the engine's global
    registry. See test_update_soul_registered_per_run for the per-run registration itself."""
    e = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""))
    assert "read_soul" in e.registry.names()
    assert "update_soul" not in e.registry.names()   # no longer global


async def test_update_soul_registered_per_run(monkeypatch):
    """Drive a real turn (stubbing only the model loop) and inspect the per-run registry it
    builds, confirming update_soul is registered there when enable_soul_editing is on."""
    e = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""))
    import engine.engine as engine_mod
    captured = {}

    async def _fake_run_loop(deps, session_id, run_id, text, user_content=None):
        captured["names"] = deps.registry.names()
        return "ok"

    monkeypatch.setattr(engine_mod, "run_loop", _fake_run_loop)
    await e.run_task("s1", "hi")
    assert "update_soul" in captured["names"]
