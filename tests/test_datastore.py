import pytest

from engine.tools.datastore import DataStore, DataStoreTool


def _store(tmp_path):
    return DataStore(str(tmp_path / "ds.db"))


# ---- DataStore (sqlite) ----

def test_put_get_roundtrip(tmp_path):
    s = _store(tmp_path)
    r = s.put("readings", "2026-07-11", '{"score": 82}')
    assert r["updated"] is False
    assert s.get("readings", "2026-07-11")["value"] == '{"score": 82}'
    assert s.get("readings", "nope") is None


def test_put_updates_existing(tmp_path):
    s = _store(tmp_path)
    s.put("readings", "2026-07-11", "old")
    r = s.put("readings", "2026-07-11", "new")
    assert r["updated"] is True
    assert s.get("readings", "2026-07-11")["value"] == "new"
    assert len(s.list("readings")) == 1                # replace, not duplicate


def test_list_and_collections_and_delete(tmp_path):
    s = _store(tmp_path)
    s.put("readings", "2026-07-10", "a")
    s.put("readings", "2026-07-11", "b")
    s.put("weight", "2026-07-11", "180")
    assert [x["key"] for x in s.list("readings")] == ["2026-07-11", "2026-07-10"]   # key DESC
    assert set(s.collections()) == {"readings", "weight"}
    assert s.delete("readings", "2026-07-10") is True
    assert s.delete("readings", "2026-07-10") is False
    assert len(s.list("readings")) == 1


def test_persists_across_reopen(tmp_path):
    p = str(tmp_path / "ds.db")
    DataStore(p).put("readings", "2026-07-11", "x")
    assert DataStore(p).get("readings", "2026-07-11")["value"] == "x"


# ---- DataStoreTool ----

async def test_tool_save_get_list_delete(tmp_path):
    t = DataStoreTool(_store(tmp_path))
    out = await t.run(t.Params(action="save", collection="readings", key="2026-07-11",
                               value='{"score": 82}'))
    assert "saved" in out and "new record" in out
    assert '"score": 82' in await t.run(t.Params(action="get", collection="readings", key="2026-07-11"))
    assert "2026-07-11" in await t.run(t.Params(action="list", collection="readings"))
    assert "deleted" in await t.run(t.Params(action="delete", collection="readings", key="2026-07-11"))
    assert "empty" in await t.run(t.Params(action="list", collection="readings"))


async def test_tool_validation_messages(tmp_path):
    t = DataStoreTool(_store(tmp_path))
    assert "needs both a key and a value" in await t.run(
        t.Params(action="save", collection="readings", key="k", value=""))
    assert "action must be one of" in await t.run(
        t.Params(action="frobnicate", collection="readings"))
    assert "no record found" in await t.run(
        t.Params(action="get", collection="readings", key="missing"))


def test_datastore_registered_when_enabled(tmp_path, monkeypatch):
    from config import Config
    from engine.engine import Engine
    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="")
    assert cfg.enable_datastore is True
    e = Engine(cfg)
    assert e.registry.get("datastore") is not None
