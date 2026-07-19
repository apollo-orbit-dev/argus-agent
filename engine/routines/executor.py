"""RoutineExecutor — run a routine's steps in order (linear, hybrid).

Tool steps run deterministically via an injected `run_tool` (the engine resolves the tool in the
session's full registry, so created tools + their CALL_TOOL chains work). Model steps run via an
injected `run_model` (one bounded agent turn, optional skill). Outputs accumulate in a context dict
keyed by step id; `{{id}}` (and built-ins {{today}}/{{now}}/{{weekday}}/{{routine}}) substitute into
later steps. The `output` step's result is delivered via the Notifier (owner-only) when `deliver`.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Awaitable, Callable, Optional

log = logging.getLogger("argus.routines")

_VAR_RE = re.compile(r"\{\{\s*([a-z0-9_]+)\s*\}\}", re.IGNORECASE)
# A tool-step arg that is EXACTLY one {{var}} (nothing else) is a whole-value reference: it gets the
# step's raw output, parsed as JSON when possible — so structured rows/lists/objects flow into a
# later tool's typed args (e.g. query_rows -> make_chart.data). An embedded {{var}} stays a string.
_WHOLE_VAR_RE = re.compile(r"^\s*\{\{\s*([a-z0-9_]+)\s*\}\}\s*$", re.IGNORECASE)


def _builtin_vars(name: str, now: Optional[datetime] = None) -> dict:
    now = now or datetime.now()
    return {"today": now.strftime("%Y-%m-%d"), "now": now.strftime("%H:%M"),
            "weekday": now.strftime("%A"), "routine": name}


def _substitute(template: str, ctx: dict) -> str:
    def repl(m):
        key = m.group(1).lower()
        if key not in ctx:
            log.warning("routine template: unknown var {{%s}}", key)
        return str(ctx.get(key, ""))
    return _VAR_RE.sub(repl, template or "")


def _whole_value(key: str, ctx: dict):
    """Resolve a whole-value {{key}} reference to structured data: the step's raw output, parsed as
    JSON if it is a JSON list/object, else the raw string. Missing key -> "" (with a warning)."""
    if key not in ctx:
        log.warning("routine template: unknown var {{%s}}", key)
        return ""
    raw = ctx[key]
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return raw
        if isinstance(parsed, (list, dict)):
            return parsed          # structured data flows through with its real type
        return raw                 # a bare JSON scalar ("5") is more useful to a tool as its string
    return raw


def _render_args(args, ctx):
    """Substitute {{var}} inside a tool step's args (recursively). A whole-value {{var}} yields the
    referenced step's structured output (JSON-parsed); an embedded {{var}} does string substitution;
    non-string leaves are untouched."""
    if isinstance(args, str):
        m = _WHOLE_VAR_RE.match(args)
        if m:
            return _whole_value(m.group(1).lower(), ctx)
        return _substitute(args, ctx)
    if isinstance(args, dict):
        return {k: _render_args(v, ctx) for k, v in args.items()}
    if isinstance(args, list):
        return [_render_args(v, ctx) for v in args]
    return args


@dataclass
class StepResult:
    id: str
    type: str
    ok: bool
    output: str = ""
    error: Optional[str] = None
    ms: int = 0


@dataclass
class RoutineResult:
    name: str
    ok: bool
    output: str = ""
    steps: list = field(default_factory=list)
    error: Optional[str] = None
    delivered: bool = False
    delivery_error: Optional[str] = None


class RoutineExecutor:
    def __init__(self,
                 run_tool: Callable[[str, str, dict], Awaitable[str]],
                 run_model: Callable[[str, str, Optional[str]], Awaitable[str]],
                 notifier=None, timeout: float = 300.0):
        self.run_tool = run_tool          # async (session_id, name, args) -> str  (raises on failure)
        self.run_model = run_model        # async (session_id, prompt, skill) -> str
        self.notifier = notifier
        self.timeout = timeout

    def _finish(self, result, on_result, start, results):
        if on_result:
            try:
                on_result({
                    "name": result.name,
                    "ok": result.ok,
                    "delivered": result.delivered,
                    "delivery_error": result.delivery_error,
                    "ms": int((time.time() - start) * 1000),
                    "steps_ok": sum(1 for s in results if s.ok),
                    "steps_total": len(results),
                })
            except Exception:
                log.warning("routine on_result emit failed", exc_info=True)
        return result

    async def run(self, routine, session_id: str, *, source: str = "on_demand",
                  emit=None, deliver: bool = True, seed: Optional[dict] = None,
                  on_result=None) -> RoutineResult:
        ctx = _builtin_vars(routine.name)
        if seed:                          # caller-supplied initial vars, e.g. {{input}} for a skill
            ctx.update(seed)
        results: list[StepResult] = []
        start = time.time()
        for step in routine.steps:
            sid, typ = step["id"], step["type"]
            t0 = time.time()
            failed, out = False, ""
            try:
                if typ == "tool":
                    args = _render_args(step.get("args", {}) or {}, ctx)
                    out = await self.run_tool(session_id, step["tool"], args)
                else:  # model — run_task turns model errors into answer strings, so it rarely raises
                    out = await self.run_model(session_id, _substitute(step["prompt"], ctx),
                                               step.get("skill"))
            except Exception as e:
                failed, out = True, f"{type(e).__name__}: {e}"
            ms = int((time.time() - t0) * 1000)
            ctx[sid] = out
            results.append(StepResult(sid, typ, not failed, "" if failed else out,
                                      out if failed else None, ms))
            if emit:
                try:
                    emit(sid, typ, not failed, ms, (out or "")[:120])
                except Exception:
                    pass
            if failed and not step.get("optional"):
                err = f"Routine '{routine.name}' stopped at step '{sid}': {out[:200]}"
                return self._finish(RoutineResult(routine.name, False, err, results, err),
                                    on_result, start, results)
            if time.time() - start > self.timeout:
                err = f"Routine '{routine.name}' exceeded its {self.timeout:.0f}s time budget"
                return self._finish(RoutineResult(routine.name, False, err, results, err),
                                    on_result, start, results)

        output = ctx.get(routine.output_id, "")
        channel = (routine.deliver or {}).get("channel")
        delivered, delivery_error = False, None
        if deliver and self.notifier and channel and channel != "none":
            if not (output or "").strip():
                delivery_error = "routine produced no output to deliver"
                log.warning("routine '%s': %s", routine.name, delivery_error)
            else:
                try:
                    ok, detail = await self.notifier.send(
                        channel, output,
                        subject=(routine.deliver or {}).get("subject", routine.name),
                        session_id=session_id)
                    delivered = ok
                    if not ok:
                        delivery_error = detail
                        log.warning("routine '%s' delivery via %s failed: %s",
                                    routine.name, channel, detail)
                except Exception as e:
                    delivery_error = str(e)
                    log.warning("routine '%s' delivery via %s failed: %s",
                                routine.name, channel, e)
        return self._finish(
            RoutineResult(routine.name, True, output, results,
                         delivered=delivered, delivery_error=delivery_error),
            on_result, start, results)
