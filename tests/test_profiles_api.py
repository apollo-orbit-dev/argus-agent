"""HTTP surface for agent profiles: create / duplicate / rename / delete / activate, the staleness
report, and the per-session permission matrix the Developer page reads."""
import httpx
import pytest

from backend.app import create_app
from config import Config
from engine.engine import Engine


@pytest.fixture
def client(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="")
    eng = Engine(cfg, data_dir=str(tmp_path), env_path=str(tmp_path / ".env"))
    app = create_app(eng)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), eng


async def test_list_create_activate_and_delete(client):
    c, eng = client
    async with c:
        d = (await c.get("/profiles")).json()
        assert d["active_profile"] == "Default"
        assert [p["name"] for p in d["profiles"]] == ["Default"]

        r = await c.post("/profiles", json={"name": "Research", "source": "Default"})
        assert r.status_code == 200 and r.json()["name"] == "Research"
        # duplicate is a snapshot copy, not a reference
        assert r.json()["tools"] == eng.profiles.get("Default").tools

        r = await c.post("/profiles/Research/activate", json={"session_id": "s1"})
        assert r.status_code == 200 and r.json()["ok"] is True
        assert "widened" in r.json()                    # activation is announced, never blocked
        assert (await c.get("/profiles?session_id=s1")).json()["session_profile"] == "Research"
        assert (await c.get("/profiles?session_id=s2")).json()["session_profile"] == "Default"

        # the active/default profile cannot be deleted, and neither can one a session is using
        assert (await c.request("DELETE", "/profiles/Default")).status_code == 409
        assert (await c.request("DELETE", "/profiles/Research")).status_code == 409
        eng.profiles.unbind("s1")
        assert (await c.request("DELETE", "/profiles/Research")).status_code == 200
        assert (await c.request("DELETE", "/profiles/Default")).status_code == 409   # last one


async def test_rename_edit_and_detail(client):
    c, eng = client
    async with c:
        await c.post("/profiles", json={"name": "Coding", "source": "Default"})
        r = await c.put("/profiles/Coding", json={"soul": "Be terse.", "description": "code mode"})
        assert r.status_code == 200 and r.json()["soul"] == "Be terse."
        r = await c.post("/profiles/Coding/rename", json={"name": "Code"})
        assert r.status_code == 200
        d = (await c.get("/profiles/Code")).json()
        assert d["soul"] == "Be terse." and d["description"] == "code mode"
        assert "stale_tools" in d and "all_tools" in d
        assert (await c.get("/profiles/Nope")).status_code == 404
        assert (await c.post("/profiles", json={"name": ""})).status_code == 400


async def test_permissions_endpoint_is_scoped_to_the_sessions_profile(client):
    c, eng = client
    async with c:
        await c.post("/profiles", json={"name": "Locked", "source": "Default"})
        await c.post("/profiles/Locked/activate", json={"session_id": "s1"})
        r = await c.post("/permissions/set",
                         json={"key": "calculator", "state": "deny", "session_id": "s1"})
        assert r.status_code == 200
        s1 = {p["key"]: p["state"] for p in (await c.get("/permissions?session_id=s1")).json()["permissions"]}
        s2 = {p["key"]: p["state"] for p in (await c.get("/permissions?session_id=s2")).json()["permissions"]}
        assert s1["calculator"] == "deny"
        assert s2["calculator"] != "deny"          # the other profile is untouched
        assert "dep-install" in s1                 # not a tool, still listed, still global
        assert eng.permissions.get("calculator") == "allow"   # global store never written
