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


async def test_cancelled_gate_cleans_pending(tmp_path):
    b = _broker(tmp_path, window=5)
    task = asyncio.ensure_future(b.gate("dep-install", "pandas", "s", "r", "install", "dashboard"))
    for _ in range(50):
        if b.store.pending(): break
        await asyncio.sleep(0.01)
    task.cancel()
    try: await task
    except asyncio.CancelledError: pass
    assert b._pending == {}                      # no leaked future
    # a later decision now takes the DEFERRED path correctly
    req_id = b.store.pending()[0]["id"]
    assert b.resolve(req_id, "approve_once", "owner") == "deferred"


async def test_double_resolve_is_idempotent(tmp_path):
    b = _broker(tmp_path, window=0.05)
    with pytest.raises(TurnPaused):
        await b.gate("dep-install", "pandas", "s", "r", "install", "dashboard")
    req_id = b.store.pending()[0]["id"]
    resumes = []
    b.register_resume("dep-install", lambda req: resumes.append(req["id"]) or _noop())
    assert b.resolve(req_id, "approve_once", "owner") == "deferred"
    assert b.resolve(req_id, "approve_once", "owner") == "unknown"   # 2nd is a no-op
    assert resumes == [req_id]                    # resume fired exactly once
    assert (("dep-install", "pandas", "s")) in b._oneshots
    assert len([k for k in b._oneshots if k == ("dep-install", "pandas", "s")]) == 1


async def test_always_allow_on_deny_only_gate_is_unknown(tmp_path):
    b = _broker(tmp_path, window=0.05)
    with pytest.raises(TurnPaused):
        await b.gate("dep-install", "pandas", "s", "r", "install", "dashboard")
    req_id = b.store.pending()[0]["id"]
    assert b.resolve(req_id, "always_allow", "owner") == "unknown"   # deps have no 'allow' state
    assert b.policy.get("dep-install") == "ask"                       # policy unchanged


async def test_resolve_emits_approval_resolved(tmp_path):
    events = []
    async def cap(session_id, kind, data):
        events.append((session_id, kind, data))
    b = ApprovalBroker(ApprovalStore(str(tmp_path / "a.json")),
                        PermissionStore(str(tmp_path / "p.json")), emit=cap, window=5)
    task = asyncio.ensure_future(b.gate("soul-edit", "t", "sess", "r", "edit", "dashboard", payload={"soul": "x"}))
    for _ in range(50):
        if b.store.pending(): break
        await asyncio.sleep(0.01)
    req_id = b.store.pending()[0]["id"]
    b.resolve(req_id, "approve_once", "owner")
    await task
    await asyncio.sleep(0.01)   # let the scheduled emit task run
    resolved = [e for e in events if e[1] == "approval_resolved"]
    assert resolved and resolved[0][2]["req_id"] == req_id and resolved[0][2]["outcome"] == "approved"


async def test_deferred_resolve_emits_approval_resolved(tmp_path):
    events = []
    async def cap(session_id, kind, data):
        events.append((session_id, kind, data))
    b = ApprovalBroker(ApprovalStore(str(tmp_path / "a.json")),
                        PermissionStore(str(tmp_path / "p.json")), emit=cap, window=0.02)
    with pytest.raises(TurnPaused):
        await b.gate("dep-install", "pandas", "sess", "r", "install", "dashboard")
    req_id = b.store.pending()[0]["id"]
    out = b.resolve(req_id, "approve_once", "owner")
    assert out == "deferred"
    await asyncio.sleep(0.01)   # let the scheduled emit task run
    resolved = [e for e in events if e[1] == "approval_resolved"]
    assert resolved and resolved[0][2]["req_id"] == req_id and resolved[0][2]["outcome"] == "approved"
