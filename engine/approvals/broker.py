"""ApprovalBroker — the one object tools call to gate a sensitive action.

gate() blocks on an in-memory Future bounded by a timeout: answered -> resume same turn;
timeout or non-interactive origin -> raise TurnPaused (request stays pending, resumable later)."""
from __future__ import annotations
import asyncio, logging
from engine.approvals.types import LABELS, states_for, Decision, TurnPaused

log = logging.getLogger("argus.approvals")
_INTERACTIVE = {"dashboard", "telegram"}
_ACTION_STATE = {"always_allow": "allow", "always_deny": "deny"}


class ApprovalBroker:
    def __init__(self, store, policy, emit=None, window: float = 60, resume=None):
        self.store = store
        self.policy = policy
        # Optional `session_id -> policy` resolver. Agent profiles own the per-tool permission
        # matrix, and the active profile is PER-SESSION, so which matrix applies is a property of
        # the turn, not of the broker. Left None (the default) the broker behaves exactly as before:
        # one global PermissionStore for every session.
        self.policy_resolver = None
        self._emit = emit                       # async (session_id, run_id, step, kind, data) -> None  (dashboard trace)
        self.window = window
        self._pending: dict[str, asyncio.Future] = {}
        self._pending_meta: dict[str, dict] = {}    # req_id -> {kind,target,session_id}
        self._oneshots: set[tuple] = set()
        self._resume: dict = {}                 # kind -> async (req) -> None
        self._default_resume = None             # fallback async (req) -> None for any other kind
        self._telegram = None                   # async (session_id, req) -> None; set by engine
        self._resume_tasks: set[asyncio.Task] = set()   # GC guard for fire-and-forget resume tasks

    def policy_for(self, session_id: str):
        """The permission policy that governs this session (the active profile's matrix when
        profiles are wired, else the global store). A resolver that raises must never take a turn
        down — fall back to the global policy, which is what ran before profiles existed."""
        if self.policy_resolver is None:
            return self.policy
        try:
            return self.policy_resolver(session_id) or self.policy
        except Exception:
            log.debug("policy resolver failed for session %r; using the global policy",
                      session_id, exc_info=True)
            return self.policy

    def register_resume(self, kind: str, fn) -> None:
        self._resume[kind] = fn

    def set_default_resume(self, fn) -> None:
        self._default_resume = fn

    async def _surface(self, req: dict) -> None:
        if self._emit:
            try:
                await self._emit(req["session_id"], req.get("run_id", ""), req.get("step", 0),
                                  "approval_request", {
                    "req_id": req["id"], "kind": req["kind"], "target": req["target"],
                    "prompt": req["prompt"], "states": states_for(req["kind"])})
            except Exception:
                log.debug("approval surface (event) failed", exc_info=True)
        if req["origin"] == "telegram" and self._telegram:
            try:
                await self._telegram(req["session_id"], req)
            except Exception:
                log.debug("approval surface (telegram) failed", exc_info=True)

    async def gate(self, kind, target, session_id, run_id, prompt, origin, payload=None,
                    step: int = 0) -> Decision:
        key = (kind, target, session_id)
        if key in self._oneshots:                          # deferred pre-approval consumed once
            self._oneshots.discard(key)
            return Decision(approved=True, one_shot=True)
        state = self.policy_for(session_id).get(kind)
        if state == "allow":
            return Decision(approved=True, auto=True)
        if state == "deny":
            return Decision(denied=True, auto=True)
        # state == "ask"
        req = self.store.create(kind, target, session_id, prompt, origin, payload,
                                 run_id=run_id, step=step)
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
        return (f"⏸ Waiting for your approval: {LABELS.get(req['kind'], req['kind']).lower()} "
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
        session_id = (meta or stored)["session_id"]
        run_id = (stored or {}).get("run_id", "")
        step = (stored or {}).get("step", 0)
        approved = action in ("approve_once", "always_allow")
        if action in _ACTION_STATE:
            try:
                # Pinned into the matrix that GOVERNED this request — i.e. the profile that was
                # live for that session, not the global store. "Prompted once, then pin it into the
                # profile" is exactly what the staleness rule promises.
                self.policy_for(session_id).set(kind, _ACTION_STATE[action])
            except (ValueError, KeyError):
                return "unknown"      # e.g. always_allow on a deny-only gate, or a deleted profile
        decision = Decision(approved=approved, denied=not approved, actor=actor)
        fut = self._pending.get(req_id)
        if fut is not None and not fut.done():              # LIVE: unblock the waiting turn
            self.store.resolve(req_id, "approved" if approved else "denied", action, actor)
            fut.set_result(decision)
            self._emit_resolved(session_id, run_id, step, req_id, action, approved, actor)
            return "live"
        # DEFERRED: turn already ended (timeout/restart)
        self.store.resolve(req_id, "approved" if approved else "denied", action, actor)
        if approved and stored is not None:
            self._oneshots.add((stored["kind"], stored["target"], stored["session_id"]))
            fn = self._resume.get(stored["kind"]) or self._default_resume
            if fn is not None:
                task = asyncio.get_running_loop().create_task(fn(stored))
                self._resume_tasks.add(task)
                task.add_done_callback(self._on_resume_done)
        self._emit_resolved(session_id, run_id, step, req_id, action, approved, actor)
        return "deferred"

    def _emit_resolved(self, session_id, run_id, step, req_id, action, approved, actor) -> None:
        if self._emit is None:
            return
        data = {"req_id": req_id, "action": action,
                "outcome": "approved" if approved else "denied", "actor": actor}
        task = asyncio.get_running_loop().create_task(
            self._emit(session_id, run_id, step, "approval_resolved", data))
        self._resume_tasks.add(task)
        task.add_done_callback(self._on_resume_done)

    def _on_resume_done(self, task: asyncio.Task) -> None:
        self._resume_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error("approval resume callback failed", exc_info=exc)
