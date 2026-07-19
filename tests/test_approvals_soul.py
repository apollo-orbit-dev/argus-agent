"""update_soul is now a plain tool (validate + set_soul) — it no longer self-gates. The loop gates
it BY NAME ('update_soul' is in DEFAULT_ASK) before ever calling run(); that gating behavior is
covered by tests/test_loop_toolgate.py. This file just covers the tool's own validate+apply logic.
"""
from engine.tools.soul import UpdateSoulTool


async def test_soul_applies_directly_no_broker_involved():
    saved = {}
    t = UpdateSoulTool(lambda: "old", lambda s: saved.__setitem__("v", s))
    out = await t.run(t.Params(soul="New persona"))
    assert saved["v"] == "New persona" and "updated" in out.lower()


async def test_soul_rejects_empty():
    t = UpdateSoulTool(lambda: "old", lambda s: None)
    out = await t.run(t.Params(soul="   "))
    assert "empty" in out.lower()


async def test_soul_rejects_too_long():
    t = UpdateSoulTool(lambda: "old", lambda s: None, max_len=10)
    out = await t.run(t.Params(soul="x" * 20))
    assert "too long" in out.lower()
