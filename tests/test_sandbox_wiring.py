"""Fail-closed is the property under test.

If the owner turns the sandbox ON and the runtime is missing, model-authored code must NOT quietly
run under the weaker AST sandbox instead — that would hand them less isolation than they asked for,
silently. exec_python is simply not registered.
"""
import tempfile

import pytest

from config import Config
from engine.engine import Engine
from engine.sandbox.runtime import ExecResult, FakeRuntime
from tests.test_config import _mk


def _engine(**over):
    cfg = _mk(**over)
    return Engine(cfg, data_dir=tempfile.mkdtemp())


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
