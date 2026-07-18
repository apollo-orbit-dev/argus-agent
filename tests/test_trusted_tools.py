import httpx
import pytest

from config import Config
from engine.engine import Engine
from engine.experimental.trust_store import TrustStore, code_hash
from engine.experimental.tool_creation import (
    CreateToolTool, RestrictedCapabilityError, scan_ast)
from engine.tools.base import ToolRegistry
from backend.app import create_app


# ---- TrustStore ----

def test_trust_store_request_approve_revoke(tmp_path):
    s = TrustStore(str(tmp_path / "t.json"))
    code = "import os\ndef run(args): return os.getcwd()"
    ch = code_hash(code)
    r = s.request("t", code, "sess")
    assert r["status"] == "pending" and r["code_hash"] == ch
    assert not s.is_trusted("t", ch)
    assert s.approve(r["id"])["status"] == "approved"
    assert s.is_trusted("t", ch)
    assert s.revoke("t") is True
    assert not s.is_trusted("t", ch)


def test_code_hash_change_forces_reapproval(tmp_path):
    s = TrustStore(str(tmp_path / "t.json"))
    c1 = "import os\ndef run(args): return '1'"
    s.approve(s.request("t", c1, "s")["id"])
    assert s.is_trusted("t", code_hash(c1))
    c2 = "import os\ndef run(args): return '2'"          # code changed
    assert not s.is_trusted("t", code_hash(c2))          # must be re-approved


def test_request_dedups_same_code(tmp_path):
    s = TrustStore(str(tmp_path / "t.json"))
    code = "def run(args): return open('/x').read()"
    a = s.request("t", code, "s")
    b = s.request("t", code, "s")
    assert a["id"] == b["id"] and len(s.list("pending")) == 1


# ---- sandbox classification ----

def test_scan_ast_flags_restricted_capability():
    with pytest.raises(RestrictedCapabilityError):
        scan_ast("def run(args): return open('/etc/hostname').read()", False)
    with pytest.raises(RestrictedCapabilityError):
        scan_ast("def run(args): return getattr(args, 'x', 1)", False)


# ---- CreateToolTool trust flow ----

async def test_restricted_capability_files_trust_request_when_enabled(tmp_path):
    ts = TrustStore(str(tmp_path / "t.json"))
    reg = ToolRegistry()
    ct = CreateToolTool(reg, trust_store=ts, allow_trusted=True, session_id="s")
    out = await ct.run(ct.Params(name="reader", description="d", parameters={},
                                 code="def run(args): return open('/etc/hostname').read()", test_args={}))
    assert "trusted" in out.lower() and "review" in out.lower()
    assert len(ts.list("pending")) == 1
    assert reg.get("reader") is None                     # NOT registered while pending


async def test_master_switch_off_hard_fails_no_request(tmp_path):
    ts = TrustStore(str(tmp_path / "t.json"))
    reg = ToolRegistry()
    ct = CreateToolTool(reg, trust_store=ts, allow_trusted=False, session_id="s")   # OFF
    out = await ct.run(ct.Params(name="r", description="d", parameters={},
                                 code="def run(args): return open('/x').read()", test_args={}))
    assert "error" in out.lower()
    assert ts.list("pending") == []                      # no trust request filed when disabled


async def test_approved_trusted_tool_runs_unsandboxed(tmp_path):
    ts = TrustStore(str(tmp_path / "t.json"))
    reg = ToolRegistry()
    ct = CreateToolTool(reg, trust_store=ts, allow_trusted=True, session_id="s",
                        persist_dir=str(tmp_path))
    code = "import os\ndef run(args): return os.name"    # `import os` is sandbox-forbidden
    await ct.run(ct.Params(name="osname", description="d", parameters={}, code=code, test_args={}))
    req = ts.list("pending")[0]
    ts.approve(req["id"])                                # human approves
    out = await ct.run(ct.Params(name="osname", description="d", parameters={}, code=code, test_args={}))
    assert "created" in out.lower() or "verified" in out.lower()
    tool = reg.get("osname")
    assert (await tool.run(tool.Params())) in ("posix", "nt")   # ran real os, unsandboxed


# ---- endpoints admin-gated ----

async def test_trust_endpoints_admin_gated(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="", admin_token="secret")
    e = Engine(cfg)
    e.trust = TrustStore(str(tmp_path / "t.json"))
    e.trust.request("reader", "def run(args): return open('/x').read()", "s")
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(e)), base_url="http://t")
    async with c:
        assert (await c.get("/trust")).status_code == 200          # read is open
        assert (await c.get("/trust")).json()["pending"][0]["tool_name"] == "reader"
        assert (await c.post("/trust/approve", json={"id": "x"})).status_code == 401
        assert (await c.post("/trust/revoke", json={"tool_name": "x"})).status_code == 401
