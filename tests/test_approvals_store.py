import pytest
from engine.approvals.store import ApprovalStore
from engine.approvals.policy import PermissionStore
from engine.approvals.types import GATES


def test_approval_create_resolve_pending(tmp_path):
    s = ApprovalStore(str(tmp_path / "approvals.json"))
    r = s.create("dep-install", "pandas", "sess", "Install pandas", "dashboard", {"tool": "x"}, now=1.0)
    assert r["status"] == "pending" and r["kind"] == "dep-install" and r["target"] == "pandas"
    assert r["payload"] == {"tool": "x"}
    assert [x["id"] for x in s.pending()] == [r["id"]]
    assert s.resolve(r["id"], "approved", "approve_once", "owner", now=2.0) is True
    assert s.pending() == []
    assert s.get(r["id"])["status"] == "approved" and s.get(r["id"])["actor"] == "owner"
    assert s.resolve("missing", "approved", "x", "y") is False


def test_approval_persistence_and_corrupt(tmp_path):
    p = str(tmp_path / "approvals.json")
    ApprovalStore(p).create("soul-edit", "d1", "s", "Edit persona", "telegram", {}, now=5.0)
    assert len(ApprovalStore(p).pending()) == 1
    open(p, "w").write("{ not json")
    assert ApprovalStore(p).pending() == []


def test_policy_defaults_and_set(tmp_path):
    ps = PermissionStore(str(tmp_path / "permissions.json"))
    assert ps.get("dep-install") == "ask"          # default
    assert ps.get("soul-edit") == "ask"
    ps.set("soul-edit", "allow")
    assert ps.get("soul-edit") == "allow"
    with pytest.raises(ValueError):
        ps.set("dep-install", "allow")             # deps have no 'allow' state
    listing = {row["kind"]: row for row in ps.list()}
    assert listing["soul-edit"]["state"] == "allow"
    assert listing["dep-install"]["states"] == ["ask", "deny"]


def test_policy_persistence(tmp_path):
    p = str(tmp_path / "permissions.json")
    PermissionStore(p).set("dep-install", "deny")
    assert PermissionStore(p).get("dep-install") == "deny"
