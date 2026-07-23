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

import json
import sys
import traceback

_MAX_OUTPUT = 8000


def run_payload(payload: dict) -> dict:
    """{'code': <source defining run(args)>, 'args': {...}} -> {'ok': bool, ...}. Never raises."""
    code = payload.get("code") or ""
    args = payload.get("args") or {}
    ns: dict = {}
    try:
        exec(compile(code, "<created_tool>", "exec"), ns)  # noqa: S102 - runs in the sandbox
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
    fn = ns.get("run")
    if not callable(fn):
        return {"ok": False, "error": "code must define a function named run(args)",
                "traceback": ""}
    try:
        result = fn(args)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
    return {"ok": True, "result": str(result)[:_MAX_OUTPUT]}


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception as e:
        print(json.dumps({"ok": False, "error": f"bad input: {e}", "traceback": ""}))
        return
    print(json.dumps(run_payload(payload)))


if __name__ == "__main__":
    main()
