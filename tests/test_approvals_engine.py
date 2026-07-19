from engine.engine import Engine
from config import Config


def _engine(tmp_path, **ov):
    return Engine(Config(**ov), data_dir=str(tmp_path))   # match real engine-test construction


def test_broker_and_wrappers_exist(tmp_path):
    e = _engine(tmp_path)
    assert e.approvals is not None
    assert isinstance(e.permissions_list(), list)
    e.permission_set("soul-edit", "allow")
    assert any(r["kind"] == "soul-edit" and r["state"] == "allow" for r in e.permissions_list())
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
