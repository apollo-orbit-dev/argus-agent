import pytest
from engine.approvals.store import ApprovalStore
from engine.approvals.policy import PermissionStore


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
    assert ps.get("web_search") == "allow"         # per-tool default: not dangerous
    ps.set("web_search", "deny")
    assert ps.get("web_search") == "deny"
    with pytest.raises(ValueError):
        ps.set("dep-install", "allow")             # deps have no 'allow' state
    listing = {row["key"]: row for row in ps.states(["web_search"])}
    assert listing["web_search"]["state"] == "deny"
    assert listing["dep-install"]["states"] == ["ask", "deny"]   # always included


def test_policy_persistence(tmp_path):
    p = str(tmp_path / "permissions.json")
    PermissionStore(p).set("dep-install", "deny")
    assert PermissionStore(p).get("dep-install") == "deny"


def test_malformed_rows_dropped_at_load(tmp_path):
    import json
    from engine.approvals.store import ApprovalStore
    from engine.approvals.policy import PermissionStore
    ap = str(tmp_path / "approvals.json")
    json.dump([
        {"id": "aa11", "kind": "dep-install", "target": "pandas", "session_id": "s",
         "prompt": "p", "origin": "dashboard", "payload": {}, "status": "pending", "created_at": 1.0,
         "resolved_at": None, "decision": None, "actor": None},
        {"id": "bb22"},                       # missing kind/created_at
        {"kind": "soul-edit", "created_at": 2.0},  # missing id
        "not-a-dict",
    ], open(ap, "w"))
    assert [r["id"] for r in ApprovalStore(ap).pending()] == ["aa11"]

    pp = str(tmp_path / "permissions.json")
    json.dump({"dep-install": "deny", "web_search": "banana", "custom_tool": "ask"}, open(pp, "w"))
    ps = PermissionStore(pp)
    assert ps.get("dep-install") == "deny"      # valid kept
    assert ps.get("web_search") == "allow"      # invalid state 'banana' dropped -> default
    assert ps.get("custom_tool") == "ask"       # any tool name is a valid key now — kept
    assert any(row["key"] == "custom_tool" for row in ps.states(["custom_tool"]))
