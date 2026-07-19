from fastapi.testclient import TestClient

from backend.app import create_app
from engine.engine import Engine
from config import Config


def _client(tmp_path, admin_token=""):
    e = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                       admin_token=admin_token), data_dir=str(tmp_path))
    return TestClient(create_app(e)), e


def test_list_is_open_and_reflects_store(tmp_path):
    client, e = _client(tmp_path)
    e.rules_add("Never use emoji")
    r = client.get("/rules/list")
    assert r.status_code == 200
    assert any(x["text"] == "Never use emoji" for x in r.json()["rules"])


def test_save_remove_toggle_require_admin(tmp_path):
    client, e = _client(tmp_path, admin_token="secret")
    assert client.post("/rules/save", json={"text": "Always cite sources"}).status_code == 401
    hdr = {"X-Admin-Token": "secret"}
    assert client.post("/rules/save", json={"text": "Always cite sources"}, headers=hdr).status_code == 200
    rid = e.rules_list()[0]["id"]
    assert client.post("/rules/toggle", json={"id": rid, "enabled": False}).status_code == 401
    assert client.post("/rules/toggle", json={"id": rid, "enabled": False}, headers=hdr).status_code == 200
    assert e.rules_list()[0]["enabled"] is False
    assert client.post("/rules/remove", json={"id": rid}).status_code == 401
    assert client.post("/rules/remove", json={"id": rid}, headers=hdr).status_code == 200
    assert e.rules_list() == []


def test_save_rejects_empty(tmp_path):
    client, e = _client(tmp_path, admin_token="secret")
    hdr = {"X-Admin-Token": "secret"}
    r = client.post("/rules/save", json={"text": "   "}, headers=hdr)
    assert r.status_code == 400
    assert e.rules_list() == []
