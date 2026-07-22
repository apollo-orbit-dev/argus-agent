"""Fail-closed is the property under test.

If the owner turns the sandbox ON and the runtime is missing, model-authored code must NOT quietly
run under the weaker AST sandbox instead — that would hand them less isolation than they asked for,
silently. exec_python is simply not registered.
"""
import logging
import os
import tempfile
from pathlib import Path

import pytest

from config import Config
from engine.engine import Engine, _migrate_legacy_workspace_dir, _workspace_dir_for
from engine.sandbox.podman import PodmanRuntime
from engine.sandbox.runtime import ExecResult, FakeRuntime
from tests.test_config import _mk


# sandbox_runtime is now a Literal["podman", "docker"] at the Config level (a PATCH /config caller
# can no longer smuggle an arbitrary path into a subprocess argv[0] — see config.py). These tests
# need a binary that is GENUINELY missing (not simulated) to exercise the real fail-closed path, so
# _engine keeps accepting the old sentinel value and, instead of routing it through Config, patches
# it onto the constructed PodmanRuntime directly — still a real, unmocked "not found" reading.
_FAKE_BINARY = "definitely-not-a-real-binary"


def _engine(**over):
    force_missing = over.get("sandbox_runtime") == _FAKE_BINARY
    if force_missing:
        over["sandbox_runtime"] = "podman"   # placeholder; must be Literal-valid to build Config
    cfg = _mk(**over)
    eng = Engine(cfg, data_dir=tempfile.mkdtemp())
    if force_missing:
        eng.sandbox.binary = _FAKE_BINARY
    return eng


def test_sandbox_off_keeps_todays_behaviour():
    eng = _engine(enable_sandbox=False, enable_code_interpreter=True)
    assert eng.sandbox is None
    assert "exec_python" in eng.tools_overview_names()


def test_sandbox_on_but_runtime_missing_does_not_register_exec_python():
    eng = _engine(enable_sandbox=True, enable_code_interpreter=True,
                  sandbox_runtime="definitely-not-a-real-binary")
    assert eng.sandbox is not None
    assert eng.sandbox.available() is False
    assert "exec_python" not in eng.tools_overview_names(), \
        "fail closed: exec_python must not fall back to the soft sandbox"


def test_sandbox_status_reports_why_it_is_unavailable():
    eng = _engine(enable_sandbox=True, sandbox_runtime="definitely-not-a-real-binary")
    st = eng.sandbox_status()
    assert st["enabled"] is True
    assert st["available"] is False
    assert st["reason"]


def test_sandbox_status_when_disabled():
    eng = _engine(enable_sandbox=False)
    assert eng.sandbox_status() == {"enabled": False, "available": False,
                                    "reason": "sandbox is disabled", "runtime": "podman",
                                    "image": "argus-sandbox:local", "workspaces": []}


async def test_exec_python_runs_through_the_runtime_when_sandboxed():
    from engine.tools.code_interpreter import CodeInterpreter
    fake = FakeRuntime(result=ExecResult(0, "42\n", ""))
    interp = CodeInterpreter(allow_network=False, timeout=10, runtime=fake, workspace="default")
    out = await interp.run("sess", "print(6*7)")
    assert "42" in out
    assert fake.calls and fake.calls[0][0] == "default"
    assert fake.calls[0][1][:2] == ["python", "-c"]


async def test_exec_python_reports_a_container_error_without_raising():
    from engine.tools.code_interpreter import CodeInterpreter
    fake = FakeRuntime(result=ExecResult(1, "", "Traceback: boom"))
    interp = CodeInterpreter(allow_network=False, timeout=10, runtime=fake, workspace="default")
    out = await interp.run("sess", "raise SystemExit(1)")
    assert "boom" in out


async def test_exec_python_reports_a_timeout_as_a_timeout():
    from engine.tools.code_interpreter import CodeInterpreter
    fake = FakeRuntime(result=ExecResult(124, "", "", timed_out=True))
    interp = CodeInterpreter(allow_network=False, timeout=10, runtime=fake, workspace="default")
    out = await interp.run("sess", "while True: pass")
    assert "timed out" in out.lower()


# ---------------------------------------------------------------------------------------------
# Finding 1 (CRITICAL): the runtime must be resolved LIVE, in lockstep with the fail-closed gate —
# never frozen at Engine construction. Reproduces the exact boot sequence from the review: sandbox
# ON, runtime unavailable at boot -> becomes available mid-process (podman comes up late).
# ---------------------------------------------------------------------------------------------
class _CaptureModel:
    """A model client stub that ends the turn immediately with no tool calls, so run_task's
    per-run registry-build block (where the sandbox wiring lives) executes for real without a
    network call or a real model."""

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None,
                   think=None, reasoning=None):
        from engine.protocol import ModelResponse
        return ModelResponse(content="ok", finish_reason="stop")


def _wire_capture_model(eng):
    eng._model_client = lambda: _CaptureModel()


async def test_runtime_is_resolved_live_not_frozen_at_construction():
    """Boot with the container unavailable -> code_interp.runtime stays None and exec_python is not
    registered. The runtime then becomes available (simulating podman starting up after Argus) ->
    the VERY NEXT turn must pick that up and route execution through the container. Before the fix,
    `self.code_interp.runtime` was frozen at Engine.__init__ time (based on a one-time availability
    check), so this second turn would have run model code through the soft AST sandbox despite the
    owner's sandbox switch being on — the exact silent weaker-isolation the fail-closed design exists
    to prevent."""
    eng = _engine(enable_sandbox=True, enable_code_interpreter=True,
                 sandbox_runtime="definitely-not-a-real-binary")
    _wire_capture_model(eng)

    # Boot state: the binary genuinely doesn't exist, so this is a real (not simulated) unavailable
    # reading — matching "podman is down / socket not ready yet" at process start.
    await eng.run_task("s1", "hello")
    assert "exec_python" not in eng.tools_overview_names()
    assert eng.code_interp.runtime is None, \
        "unavailable at boot: nothing should be wired for execution"

    # Podman "comes up" mid-process. Flip only the live availability check (never a real binary is
    # exercised here per the task's constraints) and drive a second turn.
    eng.sandbox.available = lambda: True
    await eng.run_task("s1", "hello")

    assert "exec_python" in eng.tools_overview_names(), \
        "the gate must reflect the NEW live availability, not the boot-time reading"
    # The critical assertion: exec_python must now execute through the container runtime, never
    # silently through the soft AST sandbox just because that's what was frozen in at construction.
    assert eng.code_interp.runtime is eng.sandbox, \
        "runtime must be resolved live, in lockstep with the gate — never frozen at construction"


async def test_runtime_cleared_again_if_the_container_goes_back_down():
    """Symmetric case: available at boot, then the container disappears mid-process. The next turn
    must fail closed again — not keep executing through a runtime reference that is no longer live."""
    eng = _engine(enable_sandbox=True, enable_code_interpreter=True,
                 sandbox_runtime="definitely-not-a-real-binary")
    _wire_capture_model(eng)
    eng.sandbox.available = lambda: True

    await eng.run_task("s1", "hello")
    assert eng.code_interp.runtime is eng.sandbox

    eng.sandbox.available = lambda: False
    await eng.run_task("s1", "hello")
    assert "exec_python" not in eng.tools_overview_names()


# ---------------------------------------------------------------------------------------------
# Finding 3 (IMPORTANT): the description the model sees must be accurate for whichever mode is
# actually in effect, and tools_overview() must never drift from what the live tool advertises.
# ---------------------------------------------------------------------------------------------
async def test_exec_python_description_is_accurate_and_matches_tools_overview_in_soft_mode():
    from engine.tools.code_interpreter import ExecPythonTool

    eng = _engine(enable_sandbox=False, enable_code_interpreter=True)
    _wire_capture_model(eng)
    await eng.run_task("s1", "hello")

    tool = ExecPythonTool(eng.code_interp, "s1")
    assert "persist" in tool.description.lower()
    assert "stateless" not in tool.description.lower()

    overview_desc = next(t["description"] for t in eng.tools_overview()["conditional_enabled"]
                         if t["name"] == "exec_python")
    assert overview_desc == tool.description


async def test_exec_python_description_is_accurate_and_matches_tools_overview_in_container_mode():
    from engine.tools.code_interpreter import ExecPythonTool

    eng = _engine(enable_sandbox=True, enable_code_interpreter=True,
                 sandbox_runtime="definitely-not-a-real-binary")
    _wire_capture_model(eng)
    eng.sandbox.available = lambda: True
    await eng.run_task("s1", "hello")

    tool = ExecPythonTool(eng.code_interp, "s1")
    assert "stateless" in tool.description.lower()
    assert "do not persist" in tool.description.lower() or "not persist" in tool.description.lower()
    # must not make the SOFT sandbox's persistence promise while running in container mode
    assert "variables persist between calls" not in tool.description.lower()

    overview_desc = next(t["description"] for t in eng.tools_overview()["conditional_enabled"]
                         if t["name"] == "exec_python")
    assert overview_desc == tool.description


# ---------------------------------------------------------------------------------------------
# Finding 4 (IMPORTANT): sandbox_exec_timeout must be the timeout actually used on the container
# path, independent of code_interpreter_timeout (the soft-sandbox path's timeout).
# ---------------------------------------------------------------------------------------------
def test_sandbox_exec_timeout_flows_to_the_container_path_not_code_interpreter_timeout():
    eng = _engine(enable_sandbox=True, code_interpreter_timeout=10.0, sandbox_exec_timeout=45.0,
                 sandbox_runtime="definitely-not-a-real-binary")
    assert eng.code_interp.timeout == 10.0
    assert eng.code_interp.container_timeout == 45.0


async def test_run_sandboxed_uses_container_timeout_for_the_runtime_exec_call():
    from engine.tools.code_interpreter import CodeInterpreter

    fake = FakeRuntime(result=ExecResult(0, "hi\n", ""))
    captured = {}
    real_exec = fake.exec

    def spy_exec(name, argv, *, stdin="", timeout=120.0, run_id=""):
        captured["timeout"] = timeout
        return real_exec(name, argv, stdin=stdin, timeout=timeout, run_id=run_id)

    fake.exec = spy_exec
    interp = CodeInterpreter(timeout=10.0, runtime=fake, workspace="default",
                             container_timeout=45.0)
    await interp.run("sess", "print(1)")
    assert captured["timeout"] == 45.0


# ---------------------------------------------------------------------------------------------
# Finding 5 (IMPORTANT): sandbox_idle_minutes must actually do something — wired into the runtime,
# swept opportunistically from exec(), and reaped cross-process at startup.
# ---------------------------------------------------------------------------------------------
def test_sandbox_idle_minutes_flows_to_the_runtime():
    eng = _engine(enable_sandbox=True, sandbox_idle_minutes=5,
                 sandbox_runtime="definitely-not-a-real-binary")
    assert eng.sandbox.idle_minutes == 5


def test_engine_startup_reaps_leftover_containers(monkeypatch, tmp_path):
    """The no-op stop_idle() call at Engine.__init__ (always a no-op: _last_exec is empty at
    construction) is replaced by a real cross-process reaper: stop whatever `podman ps` reports as
    still running under our naming prefix, since stage-1 containers are stateless and safe to stop."""
    stopped = []

    class _StubRuntime:
        def __init__(self, **kw):
            pass

        def available(self):
            return True

        def status(self):
            return {"runtime": "stub", "available": True,
                    "workspaces": ["default", "orphan"]}

        def stop(self, name):
            stopped.append(name)

        def stop_idle(self, idle_seconds):
            return []

        def ensure_workspace(self, name):
            pass

        def exec(self, *a, **kw):
            raise NotImplementedError

    import engine.sandbox.podman as podman_mod
    monkeypatch.setattr(podman_mod, "PodmanRuntime", _StubRuntime)

    _engine(enable_sandbox=True)   # constructs with data_dir=tempfile.mkdtemp(), triggers __init__
    assert sorted(stopped) == ["default", "orphan"]


def test_engine_startup_reap_is_skipped_when_the_runtime_is_unavailable():
    """No podman here -> nothing to reap, and status()/stop() must not be called in a way that
    raises (status() on a missing binary reports available=False with an empty workspace list)."""
    eng = _engine(enable_sandbox=True, sandbox_runtime="definitely-not-a-real-binary")
    assert eng.sandbox is not None   # construction itself must not have raised


# ---------------------------------------------------------------------------------------------
# Finding 1 (CRITICAL): the file tools' workspace and the container's bind-mount target must be
# the SAME directory — before this fix they were <data_dir>/workspace vs
# <data_dir>/workspaces/<name>, so exec_python wrote where read_file/list_files never looked. The
# review noted the suite still passed with the two pointed at each other; these assertions are
# the thing that was missing.
# ---------------------------------------------------------------------------------------------
def test_workspace_dir_matches_the_sandbox_mount_dir_when_sandbox_enabled():
    eng = _engine(enable_sandbox=True, sandbox_runtime="definitely-not-a-real-binary")
    assert eng.sandbox is not None
    assert (os.path.realpath(eng.workspace.root)
            == eng.sandbox.workspace_dir(eng._config.sandbox_workspace))


def test_workspace_dir_matches_the_sandbox_mount_dir_even_when_sandbox_disabled():
    """The path change is unconditional — the file tools must land on the unified directory
    whether or not the sandbox feature itself is on."""
    data_dir = tempfile.mkdtemp()
    cfg = _mk(enable_sandbox=False)
    eng = Engine(cfg, data_dir=data_dir)
    rt = PodmanRuntime(workspaces_root=str(Path(data_dir) / "workspaces"))
    assert eng.sandbox is None
    assert os.path.realpath(eng.workspace.root) == rt.workspace_dir(cfg.sandbox_workspace)


# ---------------------------------------------------------------------------------------------
# Finding 1 migration: this directory holds live user data on the owner's server, so the old
# <data_dir>/workspace layout must be MOVED into the unified location, never orphaned — and never
# merged/overwritten if both already exist (a wrong guess there destroys the owner's files).
# ---------------------------------------------------------------------------------------------
def test_migration_moves_the_old_workspace_when_only_it_exists(tmp_path):
    old = tmp_path / "workspace"
    old.mkdir()
    (old / "notes.txt").write_text("hello")
    (old / "sub").mkdir()
    (old / "sub" / "nested.txt").write_text("nested")
    cfg = _mk()

    _migrate_legacy_workspace_dir(tmp_path, cfg)

    new = _workspace_dir_for(tmp_path, cfg)
    assert not old.exists()
    assert (new / "notes.txt").read_text() == "hello"
    assert (new / "sub" / "nested.txt").read_text() == "nested"


def test_migration_leaves_both_untouched_and_warns_when_both_exist(tmp_path, caplog):
    old = tmp_path / "workspace"
    old.mkdir()
    (old / "old.txt").write_text("old")
    cfg = _mk()
    new = _workspace_dir_for(tmp_path, cfg)
    new.mkdir(parents=True)
    (new / "new.txt").write_text("new")

    with caplog.at_level(logging.WARNING, logger="argus.engine"):
        _migrate_legacy_workspace_dir(tmp_path, cfg)

    # Never merged, never overwritten — both survive exactly as they were.
    assert old.exists() and (old / "old.txt").read_text() == "old"
    assert new.exists() and (new / "new.txt").read_text() == "new"
    assert any("both" in r.message.lower() and str(old) in r.message and str(new) in r.message
              for r in caplog.records), "must name BOTH paths in the warning"


def test_migration_is_a_noop_on_a_fresh_install(tmp_path):
    """Neither directory exists yet — nothing to move, nothing to warn about."""
    cfg = _mk()
    _migrate_legacy_workspace_dir(tmp_path, cfg)   # must not raise
    assert not (tmp_path / "workspace").exists()
    assert not _workspace_dir_for(tmp_path, cfg).exists()


def test_engine_init_migrates_the_legacy_workspace_end_to_end(tmp_path):
    """The migration must actually be wired into Engine.__init__, not just exist as a standalone
    helper — construct a real Engine over a pre-existing legacy workspace dir and confirm the file
    tools see the migrated content."""
    old = tmp_path / "workspace"
    old.mkdir()
    (old / "report.md").write_text("hi from the old workspace")
    cfg = _mk(enable_sandbox=False)

    eng = Engine(cfg, data_dir=str(tmp_path))

    assert not old.exists()
    assert eng.workspace.exists("report.md")
    assert eng.workspace.read_text("report.md") == "hi from the old workspace"
