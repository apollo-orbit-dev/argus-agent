from engine.tools.soul import UpdateSoulTool
from engine.approvals.types import TurnPaused, Decision


class _FakeBroker:
    def __init__(self, decision=None, raise_paused=False):
        self.decision = decision; self.raise_paused = raise_paused; self.calls = []
    async def gate(self, kind, target, session_id, run_id, prompt, origin, payload=None):
        self.calls.append((kind, target, payload))
        if self.raise_paused:
            raise TurnPaused("r1", kind, "waiting")
        return self.decision


async def test_soul_no_broker_applies_directly():
    saved = {}
    t = UpdateSoulTool(lambda: "old", lambda s: saved.__setitem__("v", s))   # approvals=None
    out = await t.run(t.Params(soul="New persona"))
    assert saved["v"] == "New persona" and "updated" in out.lower()


async def test_soul_approved_applies():
    saved = {}
    b = _FakeBroker(Decision(approved=True))
    t = UpdateSoulTool(lambda: "old", lambda s: saved.__setitem__("v", s),
                       approvals=b, session_id="s", run_id="r", origin="dashboard")
    await t.run(t.Params(soul="New persona"))
    assert saved["v"] == "New persona"
    assert b.calls[0][0] == "soul-edit" and b.calls[0][2]["soul"] == "New persona"   # payload carries text


async def test_soul_denied_leaves_unchanged():
    saved = {}
    b = _FakeBroker(Decision(denied=True))
    t = UpdateSoulTool(lambda: "old", lambda s: saved.__setitem__("v", s), approvals=b)
    out = await t.run(t.Params(soul="New persona"))
    assert "v" not in saved and "declin" in out.lower()


async def test_soul_timeout_propagates_turnpaused():
    import pytest
    b = _FakeBroker(raise_paused=True)
    t = UpdateSoulTool(lambda: "old", lambda s: None, approvals=b)
    with pytest.raises(TurnPaused):
        await t.run(t.Params(soul="New persona"))
