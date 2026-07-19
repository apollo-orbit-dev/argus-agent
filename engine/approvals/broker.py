"""ApprovalBroker — the one object tools call to gate a sensitive action.

gate() blocks on an in-memory Future bounded by a timeout: answered -> resume same turn;
timeout or non-interactive origin -> raise TurnPaused (request stays pending, resumable later)."""
from __future__ import annotations
import asyncio, logging
from engine.approvals.types import GATES, Decision, TurnPaused

log = logging.getLogger("argus.approvals")
_INTERACTIVE = {"dashboard", "telegram"}
_ACTION_STATE = {"always_allow": "allow", "always_deny": "deny"}


class ApprovalBroker:
    def __init__(self, store, policy, emit=None, window: float = 60, resume=None):
        self.store = store
        self.policy = policy
        self._emit = emit                       # async (session_id, kind, data) -> None  (dashboard trace)
        self.window = window
        self._pending: dict[str, asyncio.Future] = {}
        self._pending_meta: dict[str, dict] = {}    # req_id -> {kind,target,session_id}
        self._oneshots: set[tuple] = set()
        self._resume: dict = {}                 # kind -> async (req) -> None
        self._telegram = None                   # async (session_id, req) -> None; set by engine
        self._resume_tasks: set[asyncio.Task] = set()   # GC guard for fire-and-forget resume tasks

    def register_resume(self, kind: str, fn) -> None:
        self._resume[kind] = fn

    async def _surface(self, req: dict) -> None:
        if self._emit:
            try:
                await self._emit(req["session_id"], "approval_request", {
                    "req_id": req["id"], "kind": req["kind"], "target": req["target"],
                    "prompt": req["prompt"], "states": list(GATES[req["kind"]].states)})
            except Exception:
                log.debug("approval surface (event) failed", exc_info=True)
        if req["origin"] == "telegram" and self._telegram:
            try:
                await self._telegram(req["session_id"], req)
            except Exception:
                log.debug("approval surface (telegram) failed", exc_info=True)

    async def gate(self, kind, target, session_id, run_id, prompt, origin, payload=None) -> Decision:
        key = (kind, target, session_id)
        if key in self._oneshots:                          # deferred pre-approval consumed once
            self._oneshots.discard(key)
            return Decision(approved=True, one_shot=True)
        state = self.policy.get(kind)
        if state == "allow":
            return Decision(approved=True, auto=True)
        if state == "deny":
            return Decision(denied=True, auto=True)
        # state == "ask"
        req = self.store.create(kind, target, session_id, prompt, origin, payload)
        await self._surface(req)
        if origin not in _INTERACTIVE:                     # nobody attached -> straight to pending
            raise TurnPaused(req["id"], kind, self._pending_msg(req))
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req["id"]] = fut
        self._pending_meta[req["id"]] = {"kind": kind, "target": target, "session_id": session_id}
        try:
            done, _ = await asyncio.wait({fut}, timeout=self.window)
            if fut in done and not fut.cancelled():
                return fut.result()
            raise TurnPaused(req["id"], kind, self._pending_msg(req))   # timeout
        finally:
            self._pending.pop(req["id"], None)
            self._pending_meta.pop(req["id"], None)

    def _pending_msg(self, req: dict) -> str:
        return (f"⏸ Waiting for your approval: {GATES[req['kind']].label.lower()} "
                f"({req['target']}). Approve it in the dashboard or Telegram and I'll continue. "
                f"(request {req['id']})")

    def resolve(self, req_id, action, actor) -> str:
        meta = self._pending_meta.get(req_id)
        stored = self.store.get(req_id)
        if meta is None and stored is None:
            return "unknown"
        if stored is not None and stored.get("status") != "pending":
            return "unknown"          # already resolved — idempotent no-op
        kind = (meta or stored)["kind"]
        approved = action in ("approve_once", "always_allow")
        if action in _ACTION_STATE:
            try:
                self.policy.set(kind, _ACTION_STATE[action])
            except ValueError:
                return "unknown"                            # e.g. always_allow on a deny-only gate
        decision = Decision(approved=approved, denied=not approved, actor=actor)
        fut = self._pending.get(req_id)
        if fut is not None and not fut.done():              # LIVE: unblock the waiting turn
            self.store.resolve(req_id, "approved" if approved else "denied", action, actor)
            fut.set_result(decision)
            return "live"
        # DEFERRED: turn already ended (timeout/restart)
        self.store.resolve(req_id, "approved" if approved else "denied", action, actor)
        if approved and stored is not None:
            self._oneshots.add((stored["kind"], stored["target"], stored["session_id"]))
            fn = self._resume.get(stored["kind"])
            if fn is not None:
                task = asyncio.get_running_loop().create_task(fn(stored))
                self._resume_tasks.add(task)
                task.add_done_callback(self._on_resume_done)
        return "deferred"

    def _on_resume_done(self, task: asyncio.Task) -> None:
        self._resume_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("approval resume callback failed", exc_info=exc)
