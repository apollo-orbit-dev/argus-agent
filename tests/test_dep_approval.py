import httpx
import pytest

from config import Config
from engine.engine import Engine
from engine.experimental import dep_installer
from engine.experimental.dep_store import DepStore
from engine.experimental.tool_creation import (
    CreateToolTool, DisallowedImportError, scan_ast)
from engine.tools.base import ToolRegistry
from backend.app import create_app
from backend.telegram_bot import (
    _approve_result_text, _safe_pending, dep_keyboard, dep_request_text)


# ---- DepStore (pure state) ----

def test_request_pending_and_dedup(tmp_path):
    s = DepStore(str(tmp_path / "d.json"))
    r1 = s.request("example_sdk", "fetch_data", "42", "code")
    assert r1["status"] == "pending"
    r2 = s.request("example_sdk", "fetch_data", "42", "code")   # dup -> same request
    assert r2["id"] == r1["id"]
    assert len(s.list("pending")) == 1


def test_approve_adds_module_and_persists(tmp_path):
    p = str(tmp_path / "d.json")
    s = DepStore(p)
    r = s.request("base64", "b64tool", "u")
    assert s.mark_approved(r["id"], "1.0")["status"] == "approved"
    assert s.is_approved("base64")
    s2 = DepStore(p)                                    # reload from disk
    assert "base64" in s2.approved_modules()
    assert s2.list("pending") == []


def test_deny(tmp_path):
    s = DepStore(str(tmp_path / "d.json"))
    r = s.request("evilpkg", "t", "u")
    assert s.deny(r["id"])["status"] == "denied"
    assert not s.is_approved("evilpkg")
    assert s.deny(r["id"]) is None                      # already resolved


def test_mark_failed_stays_pending(tmp_path):
    s = DepStore(str(tmp_path / "d.json"))
    r = s.request("flaky", "t", "u")
    s.mark_failed(r["id"], "network error")
    assert s.get(r["id"])["status"] == "pending"        # retryable
    assert s.get(r["id"])["last_error"] == "network error"


# ---- installer name guard ----

def test_valid_package_name():
    assert dep_installer.valid_package_name("example_sdk")
    assert dep_installer.valid_package_name("beautifulsoup4")
    assert not dep_installer.valid_package_name("evil; rm -rf /")
    assert not dep_installer.valid_package_name("--upgrade")
    assert not dep_installer.valid_package_name("")


async def test_install_refuses_bad_name():
    ok, ver, log = await dep_installer.install("bad; name")
    assert ok is False and "not a valid package name" in log


# ---- sandbox gate ----

def test_scan_ast_raises_disallowed_with_module():
    with pytest.raises(DisallowedImportError) as ei:
        scan_ast("import example_sdk\ndef run(args): return '1'", False)
    assert ei.value.module == "example_sdk"


def test_scan_ast_allows_extra_module():
    scan_ast("import base64\ndef run(args): return '1'", False, {"base64"})   # no raise


# ---- CreateToolTool: files a request instead of hard-failing ----

async def test_create_tool_files_request_for_disallowed_import(tmp_path):
    store = DepStore(str(tmp_path / "d.json"))
    reg = ToolRegistry()
    ct = CreateToolTool(reg, dep_store=store, session_id="7")
    out = await ct.run(ct.Params(
        name="fetch_data", description="pull account data",
        code="import example_sdk\ndef run(args): return '1'",
        parameters={}, test_args={}))
    assert "approval" in out.lower() and "example_sdk" in out
    assert len(store.list("pending")) == 1
    assert store.list("pending")[0]["session_id"] == "7"
    assert reg.get("fetch_data") is None              # NOT registered while pending


async def test_create_tool_without_store_hard_fails(tmp_path):
    reg = ToolRegistry()
    ct = CreateToolTool(reg, dep_store=None)            # legacy behavior
    out = await ct.run(ct.Params(
        name="t", description="d",
        code="import example_sdk\ndef run(args): return '1'",
        parameters={}, test_args={}))
    assert "not allowed" in out.lower()


async def test_stdlib_import_is_not_approvable(tmp_path):
    """`import os` must NOT become an install request — stdlib isn't pip-installable and
    os is restricted for safety. It should hard-fail and steer to SECRETS."""
    store = DepStore(str(tmp_path / "d.json"))
    reg = ToolRegistry()
    ct = CreateToolTool(reg, dep_store=store, session_id="7")
    out = await ct.run(ct.Params(
        name="grab_env", description="d",
        code="import os\ndef run(args): return os.environ.get('X','')",
        parameters={}, test_args={}))
    assert "standard library" in out.lower()
    assert store.list("pending") == []                 # NOT filed as an install
    assert reg.get("grab_env") is None


async def test_stdlib_block_steers_to_secrets(tmp_path):
    store = DepStore(str(tmp_path / "d.json"))
    ct = CreateToolTool(ToolRegistry(), dep_store=store, session_id="7",
                        secrets={"SERVICE_EMAIL": "x", "SERVICE_PASSWORD": "y"})
    out = await ct.run(ct.Params(
        name="t", description="d",
        code="import os\ndef run(args): return os.environ.get('SERVICE_EMAIL','')",
        parameters={}, test_args={}))
    assert "SECRETS" in out and "SERVICE_EMAIL" in out


async def test_create_tool_reads_secrets(tmp_path):
    reg = ToolRegistry()
    ct = CreateToolTool(reg, secrets={"SERVICE_EMAIL": "me@example.com"})
    code = "def run(args):\n    return SECRETS.get('SERVICE_EMAIL', 'none')\n"
    out = await ct.run(ct.Params(name="whoami", description="d", code=code,
                                 parameters={}, test_args={}))
    assert "me@example.com" in out                       # tool read the injected secret
    assert "SERVICE_EMAIL" in ct.description              # model is told the exact key


def test_dotenv_feeds_tool_secrets(tmp_path, monkeypatch):
    """The end-to-end that was broken live: .env creds -> os.environ -> _tool_secrets -> SECRETS."""
    import os
    from config import load_dotenv_into_environ
    envf = tmp_path / ".env"
    envf.write_text('SERVICE_EMAIL=me@x.com\nSERVICE_PASSWORD="pw"\n# a comment\nBLANK=\n')
    monkeypatch.delenv("SERVICE_EMAIL", raising=False)
    monkeypatch.delenv("SERVICE_PASSWORD", raising=False)
    try:
        keys = load_dotenv_into_environ(str(envf))
        assert "SERVICE_EMAIL" in keys
        assert os.environ["SERVICE_PASSWORD"] == "pw"          # quotes stripped
        cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                     tool_secret_names="SERVICE_EMAIL,SERVICE_PASSWORD")
        e = Engine(cfg)
        assert e._tool_secrets() == {"SERVICE_EMAIL": "me@x.com", "SERVICE_PASSWORD": "pw"}
    finally:
        for k in ("SERVICE_EMAIL", "SERVICE_PASSWORD"):
            os.environ.pop(k, None)


def test_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    import os
    from config import load_dotenv_into_environ
    monkeypatch.setenv("PRESET_VAR", "keep")
    (tmp_path / ".env").write_text("PRESET_VAR=changed\n")
    load_dotenv_into_environ(str(tmp_path / ".env"))
    assert os.environ["PRESET_VAR"] == "keep"                 # real env wins over .env


def test_tool_secrets_from_config(monkeypatch, tmp_path):
    monkeypatch.setenv("SERVICE_EMAIL", "a@b.c")
    monkeypatch.setenv("SERVICE_PASSWORD", "pw")
    monkeypatch.setenv("SHOULD_NOT_LEAK", "secret")
    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                 tool_secret_names="SERVICE_EMAIL, SERVICE_PASSWORD")
    e = Engine(cfg)
    s = e._tool_secrets()
    assert s == {"SERVICE_EMAIL": "a@b.c", "SERVICE_PASSWORD": "pw"}   # only allowlisted names
    assert "SHOULD_NOT_LEAK" not in s


async def test_create_tool_uses_approved_module(tmp_path):
    store = DepStore(str(tmp_path / "d.json"))
    store.mark_approved(store.request("base64", "b", "u")["id"], "")   # pre-approved
    reg = ToolRegistry()
    ct = CreateToolTool(reg, dep_store=store, session_id="7")
    code = ("import base64\n"
            "def run(args):\n"
            "    return base64.b64encode(args['s'].encode()).decode()\n")
    out = await ct.run(ct.Params(
        name="b64", description="d", code=code,
        parameters={"s": {"type": "string"}}, test_args={"s": "hi"}))
    assert "created" in out.lower()
    assert reg.get("b64") is not None                   # now registers + test-runs


# ---- Engine approve/deny ----

def _engine(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="")
    e = Engine(cfg)
    e.deps = DepStore(str(tmp_path / "d.json"))
    # stub the auto-continuation's run_task so approve tests don't hit the model
    e._continued = []

    async def _stub_run_task(session_id, text, **kw):
        e._continued.append((session_id, text))
        return "built it"
    e.run_task = _stub_run_task
    return e


async def _drain(e):
    import asyncio
    await asyncio.gather(*list(e._bg_tasks))       # let the spawned continuation finish


async def test_engine_approve_dep(tmp_path, monkeypatch):
    e = _engine(tmp_path)
    req = e.deps.request("base64", "t", "u")

    async def fake_install(module, timeout=300.0):
        return True, "9.9", "Successfully installed"
    monkeypatch.setattr(dep_installer, "install", fake_install)

    res = await e.approve_dep(req["id"])
    assert res["ok"] and res["module"] == "base64" and res["version"] == "9.9"
    assert e.deps.is_approved("base64")
    await _drain(e)
    assert (await e.approve_dep("nope"))["ok"] is False   # unknown id


async def test_approve_dep_auto_continues(tmp_path, monkeypatch):
    """Approving must automatically resume the build in the original session — no manual re-prompt."""
    e = _engine(tmp_path)
    req = e.deps.request("example_sdk", "fetch_data", "chat-123")

    async def fake_install(module, timeout=300.0):
        return True, "1.0", "ok"
    monkeypatch.setattr(dep_installer, "install", fake_install)

    res = await e.approve_dep(req["id"])
    assert res["ok"] and res.get("continuing") is True
    await _drain(e)
    assert len(e._continued) == 1
    session, text = e._continued[0]
    assert session == "chat-123"                    # continues in the original chat
    assert "example_sdk" in text and "fetch_data" in text


async def test_engine_approve_dep_install_failure_keeps_pending(tmp_path, monkeypatch):
    e = _engine(tmp_path)
    req = e.deps.request("flaky", "t", "u")

    async def fake_install(module, timeout=300.0):
        return False, "", "pip exploded"
    monkeypatch.setattr(dep_installer, "install", fake_install)

    res = await e.approve_dep(req["id"])
    assert res["ok"] is False and "pip exploded" in res["error"]
    assert e.deps.get(req["id"])["status"] == "pending"   # retryable, not lost


def test_engine_deny_dep(tmp_path):
    e = _engine(tmp_path)
    req = e.deps.request("evilpkg", "t", "u")
    assert e.deny_dep(req["id"])["ok"] is True
    assert e.deny_dep(req["id"])["ok"] is False


# ---- backend endpoints: admin-gated writes ----

async def test_deps_endpoints_admin_gated(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="m",
                 telegram_bot_token="", admin_token="secret")
    e = Engine(cfg)
    e.deps = DepStore(str(tmp_path / "d.json"))
    e.deps.request("base64", "t", "u")
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(e)), base_url="http://t")
    async with c:
        assert (await c.get("/deps")).status_code == 200          # read is open
        assert (await c.get("/deps")).json()["pending"][0]["module"] == "base64"
        assert (await c.post("/deps/approve", json={"id": "x"})).status_code == 401
        assert (await c.post("/deps/deny", json={"id": "x"})).status_code == 401


# ---- telegram helpers ----

def test_dep_request_text_is_plain():
    txt = dep_request_text({"id": "dep_d23e5563", "module": "example_sdk",
                            "tool_name": "get_fetch_data_data"})
    assert "example_sdk" in txt and "get_fetch_data_data" in txt
    assert "`" not in txt and "*" not in txt        # plain text — underscores render literally


def test_dep_keyboard_callback_data():
    kb = dep_keyboard("dep_d23e5563")
    btns = kb.inline_keyboard[0]
    assert btns[0].callback_data == "depok:dep_d23e5563"
    assert btns[1].callback_data == "depno:dep_d23e5563"
    assert len(btns[0].callback_data.encode()) <= 64


def test_approve_result_text():
    assert "Installed 'example_sdk' v1.2" in _approve_result_text(
        {"ok": True, "module": "example_sdk", "version": "1.2"})
    assert "Could not install" in _approve_result_text({"ok": False, "error": "boom"})


def test_safe_pending_on_engine_without_feature():
    class Bare:
        pass
    assert _safe_pending(Bare()) == []
