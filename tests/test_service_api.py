import httpx
import pytest

from backend.app import create_app
from config import Config
from engine.engine import Engine


def _client(tmp_path, admin_token=""):
    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="",
                 admin_token=admin_token)
    eng = Engine(cfg, env_path=str(tmp_path / ".env"))
    eng._system_prompt_file = tmp_path / "sp.txt"
    app = create_app(eng)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_service_status_returns_dict(monkeypatch, tmp_path):
    import engine.service as svc
    monkeypatch.setattr(svc, "status", lambda name=None: {"ok": True, "supported": True,
        "name": "argus.service", "installed": False, "enabled": False, "active": False,
        "linger": False, "unit_path": "/x"})
    async with _client(tmp_path) as c:
        r = await c.get("/service/status")
        assert r.status_code == 200
        assert r.json()["name"] == "argus.service"


async def test_service_install_calls_engine_service(monkeypatch, tmp_path):
    import engine.service as svc
    called = {}
    def fake_install(name=None, dry_run=False):
        called["hit"] = True
        return {"ok": True, "name": "argus.service", "note": "installed", "enabled": True,
                "linger_ok": True, "started": False, "unit_path": "/x", "unit_text": "[Service]"}
    monkeypatch.setattr(svc, "install", fake_install)
    async with _client(tmp_path) as c:
        r = await c.post("/service/install")
        assert r.status_code == 200 and r.json()["note"] == "installed"
        assert called["hit"]


async def test_service_uninstall_calls_engine_service(monkeypatch, tmp_path):
    import engine.service as svc
    called = {}
    def fake_uninstall(name=None):
        called["hit"] = True
        return {"ok": True, "name": "argus.service", "note": "uninstalled"}
    monkeypatch.setattr(svc, "uninstall", fake_uninstall)
    async with _client(tmp_path) as c:
        r = await c.post("/service/uninstall")
        assert r.status_code == 200 and r.json()["note"] == "uninstalled"
        assert called["hit"]


async def test_service_endpoints_admin_gated(monkeypatch, tmp_path):
    import engine.service as svc
    monkeypatch.setattr(svc, "status", lambda name=None: {"ok": True})
    async with _client(tmp_path, admin_token="s3cret") as c:
        assert (await c.get("/service/status")).status_code == 401
        assert (await c.get("/service/status", headers={"X-Admin-Token": "s3cret"})).status_code == 200
        assert (await c.post("/service/install")).status_code == 401
