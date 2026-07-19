import asyncio, pytest
from engine.approvals.store import ApprovalStore
from engine.approvals.policy import PermissionStore
from engine.approvals.broker import ApprovalBroker
from engine.approvals.types import TurnPaused


def _broker(tmp_path, window=60):
    return ApprovalBroker(ApprovalStore(str(tmp_path / "a.json")),
                          PermissionStore(str(tmp_path / "p.json")), window=window)


async def test_policy_allow_and_deny_autoresolve(tmp_path):
    b = _broker(tmp_path)
    b.policy.set("soul-edit", "allow")
    d = await b.gate("soul-edit", "t", "s", "r", "edit", "dashboard")
    assert d.approved and d.auto and not b.store.pending()          # no prompt filed
    b.policy.set("soul-edit", "deny")
    d = await b.gate("soul-edit", "t", "s", "r", "edit", "dashboard")
    assert d.denied and d.auto


async def test_non_interactive_origin_pauses_immediately(tmp_path):
    b = _broker(tmp_path)
    with pytest.raises(TurnPaused):
        await b.gate("dep-install", "pandas", "s", "r", "install", "scheduled")
    assert len(b.store.pending()) == 1                              # left pending


async def test_ask_resolves_live(tmp_path):
    b = _broker(tmp_path)
    async def decide():
        for _ in range(50):
            pend = b.store.pending()
            if pend:
                b.resolve(pend[0]["id"], "approve_once", "owner"); return
            await asyncio.sleep(0.01)
    asyncio.get_event_loop().create_task(decide())
    d = await b.gate("dep-install", "pandas", "s", "r", "install", "dashboard")
    assert d.approved and not d.auto and d.actor == "owner"
    assert True


async def test_ask_times_out_to_paused(tmp_path):
    b = _broker(tmp_path, window=0.05)
    with pytest.raises(TurnPaused):
        await b.gate("dep-install", "pandas", "s", "r", "install", "dashboard")
    assert len(b.store.pending()) == 1


async def test_always_allow_sets_policy(tmp_path):
    b = _broker(tmp_path)
    async def decide():
        for _ in range(50):
            pend = b.store.pending()
            if pend:
                b.resolve(pend[0]["id"], "always_allow", "owner"); return
            await asyncio.sleep(0.01)
    asyncio.get_event_loop().create_task(decide())
    d = await b.gate("soul-edit", "t", "s", "r", "edit", "dashboard")
    assert d.approved and b.policy.get("soul-edit") == "allow"


async def test_deferred_approve_registers_oneshot_and_resumes(tmp_path):
    b = _broker(tmp_path, window=0.02)
    resumed = []
    b.register_resume("dep-install", lambda req: resumed.append(req["id"]) or _noop())
    # first gate times out -> pending
    with pytest.raises(TurnPaused):
        await b.gate("dep-install", "pandas", "s", "r", "install", "dashboard")
    req_id = b.store.pending()[0]["id"]
    out = b.resolve(req_id, "approve_once", "owner")   # decision arrives after timeout
    assert out == "deferred" and resumed == [req_id]
    # the one-shot now auto-approves the SAME (kind,target,session) on re-run
    d = await b.gate("dep-install", "pandas", "s", "r2", "install", "dashboard")
    assert d.approved and d.one_shot


async def _noop():
    return None
