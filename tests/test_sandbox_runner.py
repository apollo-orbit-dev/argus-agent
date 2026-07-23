"""runner.py executes a created tool's `def run(args)` INSIDE the sandbox container. It is stdlib
only (the image has no third-party packages and no Argus package) and it is the marshalling boundary
between the host and the container: the host validates args, ships {code, args} as JSON, and parses
{ok, result} / {ok, error, traceback} back. These test the pure function with no container."""
import ast
import io
import json
import pathlib
import sys

from engine.sandbox.runner import main, run_payload


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
    assert mods <= {"json", "sys", "traceback", "__future__", "contextlib", "io"}, \
        f"non-stdlib import: {mods}"


def _run_main_capturing_stdout(stdin_text: str) -> str:
    """Drive main() exactly as the host does: JSON on real stdin, read real stdout back."""
    old_stdin, old_stdout = sys.stdin, sys.stdout
    sys.stdin = io.StringIO(stdin_text)
    sys.stdout = captured = io.StringIO()
    try:
        main()
    finally:
        sys.stdin, sys.stdout = old_stdin, old_stdout
    return captured.getvalue()


def test_a_tool_that_prints_does_not_corrupt_the_stdout_json_contract():
    """A model-authored run() commonly sprinkles print() in for debugging. That must not land on
    the real stdout ahead of runner's own final JSON line, or the host's json.loads breaks."""
    code = "def run(args):\n    print('printed junk')\n    return 'fine'"
    stdout = _run_main_capturing_stdout(json.dumps({"code": code, "args": {}}))
    lines = stdout.splitlines()
    assert len(lines) == 1, f"expected exactly one line on stdout, got: {stdout!r}"
    obj = json.loads(lines[0])  # must not raise
    assert obj == {"ok": True, "result": "fine"}
    assert "printed junk" not in stdout


def test_tool_stderr_is_also_isolated():
    code = "import sys\ndef run(args):\n    print('warn', file=sys.stderr)\n    return 'fine'"
    out = run_payload({"code": code, "args": {}})
    assert out == {"ok": True, "result": "fine"}


def test_sys_exit_in_tool_code_becomes_a_clean_error_not_a_process_kill():
    """SystemExit is a BaseException, not an Exception - a bare `except Exception` lets it
    propagate and kill the process with no JSON printed. That must not happen."""
    out = run_payload({"code": "import sys\ndef run(args):\n    sys.exit(0)", "args": {}})
    assert out["ok"] is False
    assert "SystemExit" in out["error"]


def test_sys_exit_at_module_level_is_also_caught():
    out = run_payload({"code": "import sys\nsys.exit(1)", "args": {}})
    assert out["ok"] is False
    assert "SystemExit" in out["error"]


def test_run_payload_never_raises_on_a_non_dict_payload():
    for bad in (None, 42, [], "x", True):
        out = run_payload(bad)  # host-facing guarantee: never raises
        assert out["ok"] is False


def test_main_on_a_non_dict_json_payload_still_prints_clean_json():
    for bad_json in ("null", "42", "[]", '"x"'):
        stdout = _run_main_capturing_stdout(bad_json)
        lines = stdout.splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["ok"] is False
