"""runner.py executes a created tool's `def run(args)` INSIDE the sandbox container. It is stdlib
only (the image has no third-party packages and no Argus package) and it is the marshalling boundary
between the host and the container: the host validates args, ships {code, args} as JSON, and parses
{ok, result} / {ok, error, traceback} back. These test the pure function with no container."""
import ast
import pathlib

from engine.sandbox.runner import run_payload


def test_runs_and_returns_the_result():
    out = run_payload({"code": "def run(args):\n    return str(args['a'] + args['b'])",
                       "args": {"a": 2, "b": 3}})
    assert out == {"ok": True, "result": "5"}


def test_non_string_return_is_coerced():
    out = run_payload({"code": "def run(args):\n    return {'x': 1}", "args": {}})
    assert out["ok"] is True and out["result"] == "{'x': 1}"


def test_full_stdlib_is_available():
    """The whole point: no AST gate. os/sqlite3/subprocess import fine in here."""
    code = "import os, sqlite3, subprocess, pathlib\ndef run(args):\n    return 'ok'"
    assert run_payload({"code": code, "args": {}}) == {"ok": True, "result": "ok"}


def test_a_raise_is_reported_with_a_traceback_not_propagated():
    out = run_payload({"code": "def run(args):\n    raise ValueError('boom')", "args": {}})
    assert out["ok"] is False
    assert "ValueError: boom" in out["error"]
    assert "boom" in out["traceback"] and "run" in out["traceback"]


def test_missing_run_function_is_an_error():
    out = run_payload({"code": "x = 1", "args": {}})
    assert out["ok"] is False and "run" in out["error"].lower()


def test_syntax_error_is_an_error_not_a_crash():
    out = run_payload({"code": "def run(args):\n    return (", "args": {}})
    assert out["ok"] is False and "error" in out


def test_output_is_capped():
    out = run_payload({"code": "def run(args):\n    return 'x' * 20000", "args": {}})
    assert out["ok"] is True and len(out["result"]) <= 8000


def test_module_imports_only_stdlib():
    """COPYed into the image, which has no third-party packages and no Argus package."""
    src = pathlib.Path("engine/sandbox/runner.py").read_text()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(n.name.split(".")[0] for n in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            mods.add(node.module.split(".")[0])
    assert mods <= {"json", "sys", "traceback", "__future__"}, f"non-stdlib import: {mods}"
