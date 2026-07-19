from fastapi.testclient import TestClient

from backend.app import create_app
from engine.engine import Engine
from config import Config


def _client(tmp_path, admin_token=""):
    e = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                       admin_token=admin_token), data_dir=str(tmp_path))
    return TestClient(create_app(e)), e


def test_approvals_list_is_open(tmp_path):
    client, e = _client(tmp_path)
    r = client.get("/approvals")
    assert r.status_code == 200
    assert r.json()["approvals"] == e.approvals_list()


def test_permissions_list_is_open(tmp_path):
    client, e = _client(tmp_path)
    r = client.get("/permissions")
    assert r.status_code == 200
    assert r.json()["permissions"] == e.permissions_list()


def test_permissions_list_covers_every_tool_plus_dep_install(tmp_path):
    # Task 5: /permissions must list a row for every tool tools_overview() reports (builtin +
    # conditional_enabled + created), not just a hardcoded pair — plus the always-present
    # "dep-install" sub-gate key.
    client, e = _client(tmp_path)
    r = client.get("/permissions")
    assert r.status_code == 200
    rows = {p["key"]: p for p in r.json()["permissions"]}

    assert "dep-install" in rows
    assert rows["dep-install"]["states"] == ["ask", "deny"]

    ov = e.tools_overview()
    tool_names = ([t["name"] for t in ov["builtin"]]
                  + [t["name"] for t in ov["conditional_enabled"]]
                  + [t["name"] for t in ov["created"]])
    assert tool_names, "expected at least one tool from tools_overview()"
    for name in tool_names:
        assert name in rows, f"expected a permissions row for tool {name!r}"
        assert rows[name]["states"] == ["allow", "ask", "deny"]

    # update_soul is on by default (enable_soul_editing=True) and lands in `builtin`.
    assert "update_soul" in rows
    assert rows["update_soul"]["states"] == ["allow", "ask", "deny"]


def test_permissions_set_requires_admin_and_validates(tmp_path):
    client, e = _client(tmp_path, admin_token="secret")
    hdr = {"X-Admin-Token": "secret"}

    # no admin header -> 401
    r = client.post("/permissions/set", json={"key": "dep-install", "state": "ask"})
    assert r.status_code == 401

    # invalid state -> 400, not 500
    r = client.post("/permissions/set", json={"key": "dep-install", "state": "allow"}, headers=hdr)
    assert r.status_code == 400

    # valid state on a real tool -> 200 and persisted
    ov = e.tools_overview()
    tool_names = ([t["name"] for t in ov["builtin"]]
                  + [t["name"] for t in ov["conditional_enabled"]]
                  + [t["name"] for t in ov["created"]])
    assert tool_names, "expected at least one real tool"
    key = tool_names[0]
    r = client.post("/permissions/set", json={"key": key, "state": "deny"}, headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    updated = {p["key"]: p["state"] for p in e.permissions_list()}
    assert updated[key] == "deny"


def test_approvals_decide_requires_admin_and_handles_unknown(tmp_path):
    client, e = _client(tmp_path, admin_token="secret")
    hdr = {"X-Admin-Token": "secret"}

    r = client.post("/approvals/decide", json={"req_id": "nope", "action": "approve"})
    assert r.status_code == 401

    r = client.post("/approvals/decide", json={"req_id": "nope", "action": "approve"}, headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"result": "unknown"}
