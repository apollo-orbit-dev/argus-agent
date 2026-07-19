"""Task 8: gate dependency installs + deferred install-and-resume.

CreateToolTool used to file a DepStore request and return a "filed a request" string on a
disallowed third-party import, letting the turn continue past a request that wasn't approved yet
(the bug). With `approvals` set (interactive-approvals ON), a disallowed import now GATES through
the broker: approved -> install + recompile in the SAME turn; denied -> not created, nothing
installed; timeout -> TurnPaused propagates so the loop can pause/resume the turn later.
"""
import asyncio

import pytest

from config import Config
from engine.approvals.types import Decision, TurnPaused
from engine.engine import Engine
from engine.experimental import dep_installer
from engine.experimental.tool_creation import CreateToolTool
from engine.tools.base import ToolRegistry

NONSTD_CODE = "import example_sdk\ndef run(args): return str(example_sdk.VERSION)\n"


class _FakeBroker:
    def __init__(self, decision=None, raise_paused=False):
        self.decision = decision
        self.raise_paused = raise_paused
        self.calls = []

    async def gate(self, kind, target, session_id, run_id, prompt, origin, payload=None):
        self.calls.append((kind, target, payload))
        if self.raise_paused:
            raise TurnPaused("r1", kind, "waiting")
        return self.decision


def _fake_install_factory(monkeypatch, installed_module="example_sdk", version="1.2.3"):
    """Stub dep_installer.install: makes the module importable (so recompile succeeds) without
    touching pip/network, and records how many times it was called."""
    calls = []

    async def fake_install(module, timeout=300.0):
        calls.append(module)
        import sys
        import types
        mod = types.ModuleType(installed_module)
        mod.VERSION = version
        sys.modules[installed_module] = mod
        return True, version, "Successfully installed"

    monkeypatch.setattr(dep_installer, "install", fake_install)
    return calls


@pytest.fixture(autouse=True)
def _cleanup_fake_module():
    yield
    import sys
    sys.modules.pop("example_sdk", None)


async def test_dep_gate_approved_installs_and_builds(monkeypatch):
    calls = _fake_install_factory(monkeypatch)
    reg = ToolRegistry()
    broker = _FakeBroker(Decision(approved=True))
    ct = CreateToolTool(reg, approvals=broker, session_id="s1", run_id="r1", origin="dashboard")

    out = await ct.run(ct.Params(
        name="sdk_tool", description="uses example_sdk", code=NONSTD_CODE,
        parameters={}, test_args={}))

    assert "filed" not in out.lower() and "approval" not in out.lower()   # NOT the old string
    assert "created" in out.lower()
    assert reg.get("sdk_tool") is not None
    assert calls == ["example_sdk"]                     # installed exactly once


async def test_dep_gate_approved_install_records_in_dep_store(monkeypatch, tmp_path):
    """Durability regression: a dep approved via the NEW gate path (CreateToolTool.run's live-approve
    install) must be recorded in DepStore too, not just pip-installed — otherwise approved_modules()
    (which feeds the startup allowlist for load_persisted_tools) won't know about it, and the
    persisted tool silently fails to recompile after a restart."""
    from engine.experimental.dep_store import DepStore

    calls = _fake_install_factory(monkeypatch)
    reg = ToolRegistry()
    broker = _FakeBroker(Decision(approved=True))
    store = DepStore(str(tmp_path / "dep_approvals.json"))
    ct = CreateToolTool(reg, approvals=broker, dep_store=store,
                        session_id="s1", run_id="r1", origin="dashboard")

    out = await ct.run(ct.Params(
        name="sdk_tool", description="uses example_sdk", code=NONSTD_CODE,
        parameters={}, test_args={}))

    assert "created" in out.lower()
    assert calls == ["example_sdk"]
    assert "example_sdk" in store.approved_modules()          # survives to the startup allowlist

    # And it persisted to disk (a fresh DepStore reading the same path also sees it) —
    # this is what makes it survive an actual process restart.
    reloaded = DepStore(str(tmp_path / "dep_approvals.json"))
    assert "example_sdk" in reloaded.approved_modules()


async def test_dep_gate_denied_not_created_nothing_installed(monkeypatch):
    calls = _fake_install_factory(monkeypatch)
    reg = ToolRegistry()
    broker = _FakeBroker(Decision(denied=True))
    ct = CreateToolTool(reg, approvals=broker, session_id="s1", run_id="r1", origin="dashboard")

    out = await ct.run(ct.Params(
        name="sdk_tool", description="uses example_sdk", code=NONSTD_CODE,
        parameters={}, test_args={}))

    assert "not approved" in out.lower() and "not created" in out.lower()
    assert reg.get("sdk_tool") is None
    assert calls == []                                  # nothing installed


async def test_dep_gate_timeout_propagates_turnpaused(monkeypatch):
    calls = _fake_install_factory(monkeypatch)
    reg = ToolRegistry()
    broker = _FakeBroker(raise_paused=True)
    ct = CreateToolTool(reg, approvals=broker, session_id="s1", run_id="r1", origin="api")

    with pytest.raises(TurnPaused):
        await ct.run(ct.Params(
            name="sdk_tool", description="uses example_sdk", code=NONSTD_CODE,
            parameters={}, test_args={}))
    assert reg.get("sdk_tool") is None
    assert calls == []


async def test_dep_gate_called_with_kind_and_target(monkeypatch):
    _fake_install_factory(monkeypatch)
    reg = ToolRegistry()
    broker = _FakeBroker(Decision(approved=True))
    ct = CreateToolTool(reg, approvals=broker, session_id="s1", run_id="r1", origin="dashboard")

    await ct.run(ct.Params(
        name="sdk_tool", description="uses example_sdk", code=NONSTD_CODE,
        parameters={}, test_args={}))

    assert broker.calls[0][0] == "dep-install"
    assert broker.calls[0][1] == "example_sdk"
    payload = broker.calls[0][2]
    assert payload["module"] == "example_sdk"
    assert payload["tool_name"] == "sdk_tool"
    assert payload["code"] == NONSTD_CODE


async def test_dep_gate_denied_message_names_module_and_tool(monkeypatch):
    _fake_install_factory(monkeypatch)
    reg = ToolRegistry()
    broker = _FakeBroker(Decision(denied=True))
    ct = CreateToolTool(reg, approvals=broker, session_id="s1", run_id="r1", origin="dashboard")

    out = await ct.run(ct.Params(
        name="sdk_tool", description="uses example_sdk", code=NONSTD_CODE,
        parameters={}, test_args={}))
    assert "example_sdk" in out and "sdk_tool" in out


async def test_stdlib_import_still_hard_fails_with_approvals_on(monkeypatch):
    """A stdlib import (os) is not installable — it must still hard-fail even with approvals set,
    never reach the gate."""
    reg = ToolRegistry()
    broker = _FakeBroker(Decision(approved=True))
    ct = CreateToolTool(reg, approvals=broker, session_id="s1", run_id="r1", origin="dashboard")
    out = await ct.run(ct.Params(
        name="grab_env", description="d",
        code="import os\ndef run(args): return os.environ.get('X', '')",
        parameters={}, test_args={}))
    assert "standard library" in out.lower()
    assert broker.calls == []                            # never gated
    assert reg.get("grab_env") is None


async def test_dep_gate_one_shot_skips_reinstall(monkeypatch):
    """A one_shot decision (set by ApprovalBroker on a DEFERRED approve, consumed by the re-run's
    gate() call) means Engine._resume_dep already installed the module before spawning this turn —
    the re-run must NOT install it again."""
    calls = _fake_install_factory(monkeypatch)
    reg = ToolRegistry()
    broker = _FakeBroker(Decision(approved=True, one_shot=True))
    ct = CreateToolTool(reg, approvals=broker, session_id="s1", run_id="r1", origin="api")

    # Module already importable (as if _resume_dep installed it already) — simulate directly.
    import sys
    import types
    mod = types.ModuleType("example_sdk")
    mod.VERSION = "9.9"
    sys.modules["example_sdk"] = mod

    out = await ct.run(ct.Params(
        name="sdk_tool", description="uses example_sdk", code=NONSTD_CODE,
        parameters={}, test_args={}))

    assert "created" in out.lower()
    assert calls == []                                  # NOT installed again


async def test_legacy_dep_store_path_unchanged_when_approvals_none():
    """approvals=None (master flag off) -> legacy DepStore.request()-and-return-string path, still
    intact (back-compat)."""
    from engine.experimental.dep_store import DepStore
    store = DepStore.__new__(DepStore)   # avoid touching disk; only .request/.approved_modules used
    store.path = ""
    store.approved = {}
    store.requests = []

    reg = ToolRegistry()
    ct = CreateToolTool(reg, dep_store=store, session_id="s1")   # approvals defaults to None
    out = await ct.run(ct.Params(
        name="sdk_tool", description="uses example_sdk", code=NONSTD_CODE,
        parameters={}, test_args={}))
    assert "filed a request" in out.lower()
    assert len(store.requests) == 1
    assert reg.get("sdk_tool") is None


# ---- Engine._resume_dep: deferred install-and-resume (generalized _continue_after_dep) ----

def _engine(tmp_path):
    return Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""),
                 data_dir=str(tmp_path))


async def test_engine_resume_dep_installs_once_and_spawns_build_turn(tmp_path, monkeypatch):
    e = _engine(tmp_path)
    install_calls = []

    async def fake_install(module, timeout=300.0):
        install_calls.append(module)
        return True, "1.0", "ok"
    monkeypatch.setattr(dep_installer, "install", fake_install)

    run_calls = []

    async def fake_run_task(session_id, text, **kw):
        run_calls.append((session_id, text, kw.get("origin")))
        return "built it"
    e.run_task = fake_run_task

    delivered = []

    async def fake_deliver(session_id, text):
        delivered.append((session_id, text))
    e.scheduler.deliver = fake_deliver

    req = {"id": "ap_1", "kind": "dep-install", "target": "example_sdk",
           "session_id": "chat-1", "origin": "dashboard",
           "payload": {"module": "example_sdk", "tool_name": "sdk_tool", "code": NONSTD_CODE}}

    await e._resume_dep(req)
    await asyncio.gather(*list(e._bg_tasks))   # drain the spawned "build it now" turn

    assert install_calls == ["example_sdk"]          # installed exactly once
    assert len(run_calls) == 1
    session, text, origin = run_calls[0]
    assert session == "chat-1" and origin == "dashboard"
    assert "example_sdk" in text and "sdk_tool" in text
    assert delivered == [("chat-1", "built it")]


async def test_run_task_dep_gate_needs_both_flags_on(tmp_path, monkeypatch):
    """Spec: a gate engages only when its OWN feature flag is also on. With interactive-approvals
    ON but enable_dep_approval OFF, the CreateToolTool built for the run must get approvals=None —
    otherwise the dep gate fires without a dep_store to record the install (dep_store is only
    passed when enable_dep_approval), and a persisted tool silently fails to recompile on restart."""
    from engine.experimental import tool_creation
    from engine.protocol import ModelResponse

    captured = {}
    real_init = tool_creation.CreateToolTool.__init__

    def spy_init(self, *args, **kwargs):
        captured["approvals"] = kwargs.get("approvals")
        captured["dep_store"] = kwargs.get("dep_store")
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(tool_creation.CreateToolTool, "__init__", spy_init)

    class _CaptureModel:
        async def chat(self, messages, tools=None, max_tokens=None, temperature=None,
                       think=None, reasoning=None):
            return ModelResponse(content="ok", finish_reason="stop")

    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                 enable_tool_creation=True, tool_calling_mode="native",
                 enable_interactive_approvals=True, enable_dep_approval=False)
    e = Engine(cfg, data_dir=str(tmp_path))
    e._model_client = lambda: _CaptureModel()

    await e.run_task("s1", "hello", origin="dashboard")

    assert "approvals" in captured
    assert captured["approvals"] is None      # dep gate must NOT engage
    assert captured["dep_store"] is None       # consistent: no store either (legacy/hard-fail)


async def test_engine_resume_dep_registered_on_broker_for_dep_install(tmp_path):
    """Wiring check: the Task-6 stub is replaced — the broker's dep-install resume handler is
    Engine._resume_dep, not a no-op."""
    e = _engine(tmp_path)
    assert e.approvals._resume["dep-install"] == e._resume_dep
