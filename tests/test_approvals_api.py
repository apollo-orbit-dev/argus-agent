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


def test_permissions_set_requires_admin_and_validates(tmp_path):
    client, e = _client(tmp_path, admin_token="secret")
    hdr = {"X-Admin-Token": "secret"}

    # no admin header -> 401
    r = client.post("/permissions/set", json={"kind": "dep-install", "state": "ask"})
    assert r.status_code == 401

    # invalid state -> 400, not 500
    r = client.post("/permissions/set", json={"kind": "dep-install", "state": "allow"}, headers=hdr)
    assert r.status_code == 400

    # valid state -> 200 and persisted
    kinds = e.permissions_list()
    assert kinds, "expected at least one managed permission kind"
    kind = kinds[0]["key"]
    valid_states = kinds[0].get("states") or ["ask", "always", "never"]
    state = valid_states[0]
    r = client.post("/permissions/set", json={"kind": kind, "state": state}, headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    updated = {p["key"]: p["state"] for p in e.permissions_list()}
    assert updated[kind] == state


def test_approvals_decide_requires_admin_and_handles_unknown(tmp_path):
    client, e = _client(tmp_path, admin_token="secret")
    hdr = {"X-Admin-Token": "secret"}

    r = client.post("/approvals/decide", json={"req_id": "nope", "action": "approve"})
    assert r.status_code == 401

    r = client.post("/approvals/decide", json={"req_id": "nope", "action": "approve"}, headers=hdr)
    assert r.status_code == 200
    assert r.json() == {"result": "unknown"}
