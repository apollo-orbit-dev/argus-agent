import httpx
from httpx import ASGITransport
from config import Config
from engine.engine import Engine
from backend.app import create_app


def _client(tmp_path, admin_token="secret"):
    eng = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                        admin_token=admin_token), data_dir=str(tmp_path))
    app = create_app(eng)
    return eng, httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def test_session_endpoints(tmp_path):
    import asyncio
    async def go():
        eng, c = _client(tmp_path)
        H = {"X-Admin-Token": "secret"}
        # create (admin-gated: no token -> 401)
        assert (await c.post("/sessions", json={"name": "work"})).status_code == 401
        r = await c.post("/sessions", json={"name": "work"}, headers=H)
        sid = r.json()["id"]
        eng.store.append_message(sid, {"role": "user", "content": "hi"})
        # list
        lst = (await c.get("/sessions")).json()
        assert any(s["id"] == sid and s["name"] == "work" for s in lst)
        # messages
        msgs = (await c.get(f"/sessions/{sid}/messages")).json()
        assert msgs["total"] == 1 and msgs["messages"][0]["content"] == "hi"
        # rename (admin)
        assert (await c.patch(f"/sessions/{sid}", json={"name": "w2"}, headers=H)).status_code == 200
        # delete (admin)
        assert (await c.delete(f"/sessions/{sid}", headers=H)).status_code == 200
        assert not any(s["id"] == sid for s in (await c.get("/sessions")).json())
        await c.aclose()
    asyncio.run(go())
