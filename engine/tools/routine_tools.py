"""Agent-facing routine tools: run_routine / list_routines. Authoring (create/edit) is dashboard-first
in v1 (see docs/routines-spec.md); an agent that composes scheduling + delivery + arbitrary tools is a
higher-trust action, deferred to phase 2 behind the approval flow."""
from __future__ import annotations

from pydantic import BaseModel, Field

from engine.tools.base import Tool


class RunRoutineTool(Tool):
    name = "run_routine"
    echo_result = True          # the routine's output IS the deliverable — the loop guarantees it lands
    description = (
        "Run one of your saved routines by name — a fixed, ordered multi-step sequence (e.g. a "
        "morning briefing that fetches weather + news and formats a report). Use when the user asks "
        "to run/trigger a named routine or one of its trigger phrases. The routine's output is "
        "returned for you to relay. Arg: name. (See list_routines for what's available.)"
    )

    class Params(BaseModel):
        name: str = Field(..., description="the routine's name")

    def __init__(self, store, executor, session_id: str = ""):
        self.store = store
        self.executor = executor
        self.session_id = session_id

    async def run(self, args: "RunRoutineTool.Params") -> str:
        r = self.store.get(args.name)
        if r is None:
            names = ", ".join(x.name for x in self.store.list()) or "(none)"
            return f"run_routine: no routine named '{args.name}'. Available: {names}."
        if not r.enabled:
            return f"run_routine: routine '{args.name}' is disabled."
        # On-demand from a chat: don't fan out to the routine's delivery channel (that would double-send
        # when the chat IS that channel) — just return the output for the current reply to carry.
        res = await self.executor.run(r, self.session_id, source="on_demand", deliver=False)
        if not res.ok:
            return res.error or f"run_routine: routine '{args.name}' failed."
        return res.output or f"Routine '{args.name}' ran but produced no output."


class ListRoutinesTool(Tool):
    name = "list_routines"
    description = "List your saved routines — name, what each does, and its schedule if any."

    class Params(BaseModel):
        pass

    def __init__(self, store):
        self.store = store

    async def run(self, args: "ListRoutinesTool.Params") -> str:
        routines = self.store.list()
        if not routines:
            return "You have no saved routines yet."
        lines = []
        for r in routines:
            sched = (r.trigger or {}).get("schedule", "")
            tag = "" if r.enabled else " (disabled)"
            extra = f"  [runs: {sched}]" if sched else ""
            lines.append(f"• {r.name} — {r.description or '(no description)'}{extra}{tag}")
        return "Your routines:\n" + "\n".join(lines)
