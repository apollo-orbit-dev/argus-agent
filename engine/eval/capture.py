"""Run one agent turn against an (isolated) engine and capture what it did — the reusable core the
skill-eval harness and the model-capability benchmark both need. No scoring here; just observation.
"""
from __future__ import annotations

import asyncio


async def run_and_capture(engine, session: str, prompt: str, timeout: float = 120.0) -> dict:
    """Subscribe to the engine's event stream, run the turn, and return what happened:
    {"tools": [ordered tool names], "create_table_args": [args of each create_table call],
     "observer": [ordered observer issue names], "final": <final answer, truncated>,
     "error": <str|None>}. Never raises — a timeout or crash is recorded as an error cell so a
     sweep keeps going.

    `observer` is always present (empty when nothing fired). scoring.score_case's `no_observer`
    predicate reads this key and treats a MISSING one as clean, so omitting it made that predicate
    pass vacuously for every benchmark run — a battery could assert "the loop never gave up" and be
    told it held on a turn the loop actually killed."""
    events: list = []

    async def collect():
        try:
            async for ev in engine.subscribe(session):
                events.append(ev)
        except asyncio.CancelledError:
            pass

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.05)                     # let the subscription register before the turn runs
    final, err = "", None
    try:
        final = await asyncio.wait_for(engine.run_task(session, prompt, origin="api"), timeout=timeout)
    except Exception as e:                        # noqa: BLE001 - one bad turn must not kill the sweep
        err = f"{type(e).__name__}: {e}"
    await asyncio.sleep(0.1)                       # drain buffered events before cancelling the collector
    task.cancel()

    tools, ct_args, obs = [], [], []
    for ev in events:
        data = getattr(ev, "data", {}) or {}
        if ev.kind == "tool_call" and data.get("tool"):
            tools.append(data["tool"])
            if data["tool"] == "create_table":
                ct_args.append(data.get("args"))
        # observer issues are the proximate effect of a loop-level intervention: a turn killed by
        # `stuck_repeating` is a turn the loop gave up on, not one the model failed on its merits.
        elif ev.kind == "observer" and data.get("issue"):
            obs.append(data["issue"])
    return {"tools": tools, "create_table_args": ct_args, "observer": obs,
            "final": (final or "")[:2000], "error": err}
