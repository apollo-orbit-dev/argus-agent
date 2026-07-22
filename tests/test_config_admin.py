import httpx
import pytest

from config import Config, persist_to_env
from engine.engine import Engine
from engine.protocol import ModelResponse
from backend.app import create_app


# ---- config persistence (pure) ----

def test_env_pairs_formatting():
    c = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="",
               enable_tool_creation=True, allowed_chat_ids="1,2")
    pairs = c.env_pairs()
    assert pairs["MODEL_BASE_URL"] == "http://x/v1"
    assert pairs["ENABLE_TOOL_CREATION"] == "true"
    assert pairs["ALLOWED_CHAT_IDS"] == "1,2"


def test_persist_to_env_updates_in_place_and_appends(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# a comment\nMODEL_NAME=old\nUNRELATED=keep\n")
    c = Config(model_base_url="http://x/v1", model_name="new", telegram_bot_token="")
    persist_to_env(c, str(env))
    text = env.read_text()
    assert "# a comment" in text          # comment preserved
    assert "UNRELATED=keep" in text       # unrelated line preserved
    assert "MODEL_NAME=new" in text       # updated in place
    assert "MODEL_BASE_URL=http://x/v1" in text  # appended


# ---- admin endpoints ----

class _EchoModel:
    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None, reasoning=None):
        return ModelResponse(content="ok", finish_reason="stop")


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="")
    eng = Engine(cfg)
    eng._model_client = lambda: _EchoModel()
    eng._system_prompt_file = tmp_path / "sp.txt"   # isolate from repo
    from engine.custom_commands import CustomCommandStore
    eng.commands = CustomCommandStore(str(tmp_path / "custom_commands.yaml"))   # don't touch the repo file
    import backend.app as app_mod
    monkeypatch.setattr(app_mod, "ENV_PATH", tmp_path / ".env")
    app = create_app(eng)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t"), eng, tmp_path


async def test_system_prompt_get_put(client):
    c, eng, _ = client
    async with c:
        r = await c.get("/system-prompt")
        assert "prompt" in r.json()
        r = await c.put("/system-prompt", json={"prompt": "You are a test bot."})
        assert r.json()["saved"] is True
        r = await c.get("/system-prompt")
        assert r.json()["prompt"] == "You are a test bot."
    assert eng.get_system_prompt() == "You are a test bot."


async def test_env_view_edit_and_save(client):
    c, eng, tmp = client
    async with c:
        await c.put("/config/env", json={"text": "MODEL_NAME=main\nFOO=bar\n"})
        r = await c.get("/config/env")
        assert "FOO=bar" in r.json()["text"]
        r = await c.post("/config/save")
        assert r.json()["saved"] is True
    assert (tmp / ".env").exists()


async def test_custom_commands_crud_endpoints(client):
    c, eng, _ = client
    async with c:
        assert (await c.get("/commands")).json() == {}
        r = await c.post("/commands", json={"name": "/Standup", "expansion": "Summarize my standup"})
        assert r.status_code == 200 and r.json()["name"] == "standup"
        assert (await c.get("/commands")).json() == {"standup": "Summarize my standup"}
        # reserved names are rejected with 400
        r = await c.post("/commands", json={"name": "status", "expansion": "x"})
        assert r.status_code == 400
        r = await c.post("/commands/remove", json={"name": "standup"})
        assert r.json()["removed"] is True
        assert (await c.get("/commands")).json() == {}
    # and the engine actually expands a saved alias inside run_task
    eng.custom_command_set("hi", "say hello")
    assert eng._expand_command("/hi there") == "say hello there"


async def test_files_inline_vs_attachment_disposition(client):
    c, eng, _ = client
    eng.files_save("report.md", b"# Title\n\nhello")
    async with c:
        r = await c.get("/files/report.md")                 # default: force a download
        assert r.status_code == 200
        assert "attachment" in r.headers["content-disposition"]
        r = await c.get("/files/report.md", params={"inline": 1})   # preview: render in place
        assert r.status_code == 200
        assert r.headers["content-disposition"].startswith("inline")
        assert r.text == "# Title\n\nhello"


async def test_files_inline_is_xss_hardened(client):
    """Inline previews must never let a workspace .html/.svg execute as a document same-origin."""
    c, eng, _ = client
    eng.files_save("evil.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
    eng.files_save("evil.html", b"<script>alert(1)</script>")
    eng.files_save("logo.png", b"\x89PNG\r\n\x1a\n fake png bytes")
    async with c:
        # every inline response is sandboxed and nosniff'd
        for nm in ("evil.svg", "evil.html", "logo.png"):
            r = await c.get(f"/files/{nm}", params={"inline": 1})
            assert r.headers["content-security-policy"] == "sandbox"
            assert r.headers["x-content-type-options"] == "nosniff"
        # html/xml/unknown are forced to text/plain — they can't render as a document
        r = await c.get("/files/evil.html", params={"inline": 1})
        assert r.headers["content-type"].startswith("text/plain")
        # an image keeps its real type (safe to render; sandbox+nosniff still applied)
        r = await c.get("/files/logo.png", params={"inline": 1})
        assert r.headers["content-type"] == "image/png"


async def test_files_upload_rejects_traversal_name_with_400(client):
    """A filename that safe_path rejects (e.g. path traversal) must come back as a clean 400,
    not a 500 — the endpoint has to catch the ValueError safe_path now raises."""
    c, eng, tmp_path = client
    async with c:
        r = await c.post("/files/upload", files={"file": ("../evil.txt", b"pwned", "text/plain")})
        assert r.status_code == 400
        assert "traversal" in r.json()["detail"].lower()
    assert not eng.files_path("evil.txt")            # nothing was written under the workspace
    assert not (tmp_path.parent / "evil.txt").exists()


async def test_files_upload_saves_a_well_formed_name(client):
    c, eng, _ = client
    name = "test_files_upload_saves_a_well_formed_name.txt"
    try:
        async with c:
            r = await c.post("/files/upload", files={"file": (name, b"hello", "text/plain")})
            assert r.status_code == 200
            assert r.json()["ok"] is True
        assert eng.files_path(name)
    finally:
        eng.files_delete(name)                       # this fixture doesn't isolate the workspace dir


async def test_enable_tool_creation_via_patch(client):
    c, eng, _ = client
    async with c:
        r = await c.patch("/config", json={"enable_tool_creation": True})
        assert r.json()["enable_tool_creation"] is True


async def test_admin_token_gates_sensitive_endpoints(tmp_path, monkeypatch):
    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="",
                 admin_token="s3cret")
    eng = Engine(cfg)
    eng._system_prompt_file = tmp_path / "sp.txt"
    import backend.app as app_mod
    monkeypatch.setattr(app_mod, "ENV_PATH", tmp_path / ".env")
    (tmp_path / ".env").write_text("MODEL_NAME=main\n")
    app = create_app(eng)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        # no header -> 401
        r = await c.get("/config/env")
        assert r.status_code == 401
        r = await c.post("/admin/restart")
        assert r.status_code == 401
        # correct header -> allowed
        r = await c.get("/config/env", headers={"X-Admin-Token": "s3cret"})
        assert r.status_code == 200
        # open endpoints remain open (no token needed)
        assert (await c.get("/config")).status_code == 200


async def test_admin_token_absent_means_open(client):
    # default fixture has no admin_token -> sensitive endpoints work without a header
    c, eng, _ = client
    async with c:
        assert (await c.get("/config/env")).status_code == 200
