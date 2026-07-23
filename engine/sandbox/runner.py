"""Executes a created tool's `def run(args)` INSIDE the sandbox container.

The container is on an --internal network with no route to the host, so the host cannot call in;
instead the host runs THIS module via `podman exec ... python /opt/argus/runner.py`, writing one
JSON object on stdin and reading one back on stdout. Stage 2b's marshalling boundary.

STDLIB ONLY, no Argus imports: the image has neither third-party packages nor the Argus package.
The host validates the tool's args (its pydantic Params model) BEFORE they cross in here, so `args`
is already the right shape. There is NO AST gate here — that is the point of the container path: the
tool gets the full standard library.
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
import traceback

_MAX_OUTPUT = 8000


def run_payload(payload: dict) -> dict:
    """{'code': <source defining run(args)>, 'args': {...}} -> {'ok': bool, ...}. Never raises.

    The tool's own stdout/stderr (e.g. a stray `print(...)` in model-authored code, which is very
    common) is redirected away from the real stdout/stderr for the duration of exec+call, so it can
    never land ahead of the final JSON line that main() prints. SystemExit/KeyboardInterrupt (raised
    by e.g. a tool calling sys.exit()) are BaseException, not Exception, and are also caught here so
    they turn into a normal {ok: false, ...} result instead of killing the process. A non-dict
    payload (e.g. JSON `null`/`42`/`[]`/`"x"` decoded off stdin) is also handled here rather than
    left to blow up on the `.get()` below, so this function's "never raises" guarantee holds for
    any input, not just malformed dicts.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": f"bad input: expected a JSON object, got "
                f"{type(payload).__name__}", "traceback": ""}
    code = payload.get("code") or ""
    args = payload.get("args") or {}
    ns: dict = {}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exec(compile(code, "<created_tool>", "exec"), ns)  # noqa: S102 - runs in the sandbox
    except BaseException as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
    fn = ns.get("run")
    if not callable(fn):
        return {"ok": False, "error": "code must define a function named run(args)",
                "traceback": ""}
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = fn(args)
    except BaseException as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
    return {"ok": True, "result": str(result)[:_MAX_OUTPUT]}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"bad input: {e}", "traceback": ""}), flush=True)
        return
    print(json.dumps(run_payload(payload)), flush=True)


if __name__ == "__main__":
    main()
