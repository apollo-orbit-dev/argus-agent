"""exec_python — sandboxed REPL: REPL semantics, persistent per-session state, and the sandbox gates."""
import asyncio

from engine.tools.code_interpreter import CodeInterpreter, ExecPythonTool


def _run(ci, code, session="s", reset=False):
    return asyncio.run(ci.run(session, code, reset=reset))


# ---- REPL semantics ----
def test_last_expression_value_is_shown():
    assert _run(CodeInterpreter(), "2 + 3") == "5"


def test_stdout_is_captured():
    out = _run(CodeInterpreter(), "print('hello'); 1 + 1")
    assert "hello" in out and "2" in out


def test_statement_only_has_no_output():
    assert _run(CodeInterpreter(), "z = 10") == "(no output)"


def test_allowed_stdlib_module():
    assert "4.0" in _run(CodeInterpreter(), "import math\nmath.sqrt(16)")


# ---- persistent per-session state ----
def test_variables_persist_within_a_session():
    ci = CodeInterpreter()
    assert _run(ci, "x = 21") == "(no output)"
    assert _run(ci, "x * 2") == "42"            # x survived to the next call


def test_sessions_are_isolated():
    ci = CodeInterpreter()
    _run(ci, "secret = 99", session="a")
    assert "NameError" in _run(ci, "secret", session="b")   # b can't see a's variable


def test_reset_clears_the_namespace():
    ci = CodeInterpreter()
    _run(ci, "y = 5")
    assert "NameError" in _run(ci, "y", reset=True)          # reset wiped y before running


# ---- error surfacing (the self-correction signal) ----
def test_runtime_error_returns_traceback():
    out = _run(CodeInterpreter(), "undefined_var")
    assert "NameError" in out and "undefined_var" in out


def test_syntax_error_is_reported():
    # scan_ast parses before running, so a syntax error is caught at the gate ("blocked")
    assert "syntax error" in _run(CodeInterpreter(), "def oops(").lower()


# ---- sandbox gates (reused from create_tool) ----
def test_blocks_os_import():
    out = _run(CodeInterpreter(), "import os")
    assert "blocked" in out


def test_blocks_open_builtin():
    out = _run(CodeInterpreter(), "open('/etc/passwd')")
    assert "blocked" in out and "open" in out


def test_blocks_dunder_access():
    out = _run(CodeInterpreter(), "(1).__class__")
    assert "blocked" in out


# ---- timeout ----
def test_timeout_is_enforced():
    ci = CodeInterpreter(timeout=0.05)
    out = _run(ci, "import time\ntime.sleep(0.5)")
    assert "timed out" in out


# ---- the Tool wrapper delegates with its bound session id ----
def test_tool_wrapper_roundtrip():
    ci = CodeInterpreter()
    tool = ExecPythonTool(ci, "sess1")
    assert asyncio.run(tool.run(tool.Params(code="7 * 6"))) == "42"
    # a second tool instance for the same session still sees the state (state lives on the manager)
    tool2 = ExecPythonTool(ci, "sess1")
    asyncio.run(tool2.run(tool2.Params(code="w = 3")))
    assert asyncio.run(ExecPythonTool(ci, "sess1").run(tool.Params(code="w + 1"))) == "4"
