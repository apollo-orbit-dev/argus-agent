from config import Config
from engine.engine import Engine


def _engine(tmp_path, on=True):
    return Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                         enable_trace_persistence=on), data_dir=str(tmp_path))


def test_off_no_store_no_db(tmp_path):
    e = _engine(tmp_path, on=False)
    assert e._trace is None
    import asyncio
    asyncio.run(e.emit("run1", "sess", 0, "tool_call", {"tool": "calculator"}))
    assert not (tmp_path / "events.db").exists()


def test_persist_and_restart_hydration(tmp_path):
    import asyncio
    e = _engine(tmp_path, on=True)
    async def go():
        await e.emit("run1", "sess", 0, "tool_call", {"tool": "calculator"})
        await e.emit("run1", "sess", 1, "final", {"answer": "4"})
    asyncio.run(go())
    assert (tmp_path / "events.db").exists()
    # a fresh engine on the same data_dir (in-memory buffer empty) still replays the run
    e2 = _engine(tmp_path, on=True)
    evs = e2.recent("sess")
    assert [ev.kind for ev in evs] == ["tool_call", "final"]
    assert evs[0].data["tool"] == "calculator"


def test_recent_merges_without_duplicates(tmp_path):
    import asyncio
    e = _engine(tmp_path, on=True)
    asyncio.run(e.emit("run1", "sess", 0, "tool_call", {"tool": "calc"}))
    evs = e.recent("sess")                     # in-memory + persisted, deduped
    assert sum(1 for ev in evs if ev.kind == "tool_call") == 1
