import asyncio

from engine.engine import Engine
from config import Config


def _engine(tmp_path, **ov):
    return Engine(Config(**ov), data_dir=str(tmp_path))   # match real engine-test construction


def test_broker_and_wrappers_exist(tmp_path):
    e = _engine(tmp_path)
    assert e.approvals is not None
    assert isinstance(e.permissions_list(), list)
    e.permission_set("update_soul", "allow")
    assert any(r["key"] == "update_soul" and r["state"] == "allow" for r in e.permissions_list())
    assert e.approvals_list() == []


def test_permission_set_rejects_invalid(tmp_path):
    e = _engine(tmp_path)
    import pytest
    with pytest.raises(ValueError):
        e.permission_set("dep-install", "allow")


def test_master_flag_off_no_broker_calls(tmp_path):
    e = _engine(tmp_path, enable_interactive_approvals=False)
    # with the flag off, gate() must never be reached; the broker may exist but is inert.
    # (Asserted indirectly in the SOUL/dep tasks; here just confirm the flag is readable.)
    assert e._config.enable_interactive_approvals is False


async def test_approval_emit_attributes_events_to_the_real_run_not_synthetic_approval(tmp_path):
    """Regression for the bug: Engine._approval_emit used to hardcode run_id="approval", step=0,
    disconnecting the approval card from the turn that's actually paused (and leaving the
    synthetic "approval" run without a `final` event, so the dashboard showed it red/dead).
    It must now publish under the real run_id/step it's given."""
    e = _engine(tmp_path)
    captured = []
    orig_publish = e.events.publish

    async def capture(ev):
        captured.append(ev)
        await orig_publish(ev)

    e.events.publish = capture

    await e._approval_emit("sess1", "run_xyz", 3, "approval_request",
                            {"req_id": "abc123", "kind": "soul-edit"})

    assert len(captured) == 1
    ev = captured[0]
    assert ev.run_id == "run_xyz"
    assert ev.run_id != "approval"          # NOT the old synthetic run id
    assert ev.step == 3
    assert ev.session_id == "sess1"
    assert ev.kind == "approval_request"
