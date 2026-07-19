import pytest
from engine.approvals.types import DEFAULT_ASK, states_for, default_for
from engine.approvals.policy import PermissionStore


def test_states_and_defaults():
    assert states_for("dep-install") == ["ask", "deny"]
    assert states_for("update_soul") == ["allow", "ask", "deny"]
    assert states_for("web_search") == ["allow", "ask", "deny"]
    assert default_for("dep-install") == "ask"
    assert default_for("update_soul") == "ask"          # in DEFAULT_ASK
    assert default_for("exec_python") == "ask"
    assert default_for("web_search") == "allow"         # not dangerous
    assert "dep-install" in DEFAULT_ASK and "update_soul" in DEFAULT_ASK


def test_get_returns_default_then_stored(tmp_path):
    ps = PermissionStore(str(tmp_path / "permissions.json"))
    assert ps.get("web_search") == "allow"              # default
    assert ps.get("update_soul") == "ask"               # default-ask
    ps.set("web_search", "deny")
    assert ps.get("web_search") == "deny"
    ps.set("update_soul", "allow")
    assert ps.get("update_soul") == "allow"


def test_set_validates(tmp_path):
    ps = PermissionStore(str(tmp_path / "permissions.json"))
    with pytest.raises(ValueError):
        ps.set("dep-install", "allow")                  # deps never allow
    with pytest.raises(ValueError):
        ps.set("web_search", "bogus")


def test_states_listing(tmp_path):
    ps = PermissionStore(str(tmp_path / "permissions.json"))
    ps.set("forget", "deny")
    rows = {r["key"]: r for r in ps.states(["web_search", "forget", "update_soul"])}
    assert rows["web_search"]["state"] == "allow" and rows["web_search"]["is_default"] is True
    assert rows["forget"]["state"] == "deny" and rows["forget"]["is_default"] is False
    assert rows["update_soul"]["states"] == ["allow", "ask", "deny"]
    assert "dep-install" in {r["key"] for r in ps.states([])}   # always included


def test_notify_defaults_allow():
    # notify is the owner's own delivery channel + how scheduled tasks deliver results;
    # it must NOT default to Ask (that would hard-pause every non-interactive scheduled turn).
    assert default_for("notify") == "allow"
    assert "notify" not in DEFAULT_ASK
