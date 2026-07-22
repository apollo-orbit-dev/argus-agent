"""The real container runtime. The ONLY module in Argus that builds a podman argv.

Every command is idempotent, because Argus restarts more often than containers do: ensure_workspace
adopts a container that is already running rather than creating a second one, which is what makes
reconcile-on-restart free.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time

from engine.sandbox.runtime import ExecResult, SandboxUnavailable, validate_workspace

_NAME_PREFIX = "argus-ws-"

# available() is called on the hot per-turn path (Engine.run_task's fail-closed gate) plus every
# /library and Telegram /tools hit, and each uncached call is a `podman info` subprocess (up to 15s
# on a slow/loaded host). A few seconds of staleness is a fine trade against blocking the event loop
# on every single turn; it only needs to stop per-turn hammering, not track podman in real time. A
# stale reading here only ever feeds ONE decision within a given call to run_task (the gate reads it
# once; the runtime actually wired into CodeInterpreter is derived from the `enable_sandbox` flag,
# not from a second, possibly-differently-stale call to available()) — so caching cannot reintroduce
# the split-brain that finding 1's fix eliminated.
_AVAILABILITY_TTL_S = 5.0


class PodmanRuntime:
    def __init__(self, binary: str = "podman", image: str = "argus-sandbox:local",
                 workspaces_root: str = "data/workspaces", memory: str = "2g",
                 pids_limit: int = 256, cpus: str = "2", idle_minutes: int = 30,
                 sweep_interval_s: float = 60.0):
        self.binary = binary
        self.image = image
        self.workspaces_root = os.path.abspath(workspaces_root)
        self.memory = memory
        self.pids_limit = pids_limit
        self.cpus = cpus
        self._last_exec: dict[str, float] = {}
        self._avail_cache: "tuple[float, bool] | None" = None
        # Idle sweep (see _maybe_stop_idle): no background task, just an opportunistic check on
        # every exec() — same shape as TraceStore._maybe_prune (engine/trace/store.py).
        self.idle_minutes = idle_minutes
        self._sweep_interval_s = sweep_interval_s
        self._last_sweep = time.time()   # avoid a cold-start sweep before any workspace has run

    # ---------------- naming & paths ----------------

    def container_name(self, name: str) -> str:
        return _NAME_PREFIX + validate_workspace(name)

    def workspace_dir(self, name: str) -> str:
        return os.path.join(self.workspaces_root, validate_workspace(name))

    # ---------------- argv construction (unit-tested without podman) ----------------

    def _run_argv(self, name: str) -> list[str]:
        """Stage 1: --network=none. There is no egress proxy yet, so the container gets no network
        at all rather than the default bridge, which would silently hand it the LAN."""
        return [
            self.binary, "run", "-d",
            "--name", self.container_name(name),
            "--network=none",
            "--userns=keep-id",
            "-v", f"{self.workspace_dir(name)}:/home/argus:Z",
            "-w", "/home/argus",
            "--memory", self.memory,
            "--pids-limit", str(self.pids_limit),
            "--cpus", self.cpus,
            self.image, "sleep", "infinity",
        ]

    # ---------------- process helpers ----------------

    def _run(self, argv: list[str], *, stdin: str = "", timeout: float = 30.0) -> ExecResult:
        try:
            p = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return ExecResult(124, "", f"timed out after {timeout}s", timed_out=True)
        except FileNotFoundError:
            raise SandboxUnavailable(f"{self.binary} not found")
        return ExecResult(p.returncode, p.stdout, p.stderr)

    # ---------------- SandboxRuntime ----------------

    def available(self) -> bool:
        now = time.time()
        if self._avail_cache is not None and now - self._avail_cache[0] < _AVAILABILITY_TTL_S:
            return self._avail_cache[1]
        val = self._available_uncached()
        self._avail_cache = (now, val)
        return val

    def _available_uncached(self) -> bool:
        if not shutil.which(self.binary):
            return False
        try:
            return self._run([self.binary, "info"], timeout=15).exit_code == 0
        except SandboxUnavailable:
            return False

    def status(self) -> dict:
        if not shutil.which(self.binary):
            return {"runtime": self.binary, "available": False, "reason": "binary not found",
                    "image": self.image, "workspaces": []}
        ver = self._run([self.binary, "--version"], timeout=15)
        ok = self._run([self.binary, "info"], timeout=15)
        img = self._run([self.binary, "image", "exists", self.image], timeout=15)
        ps = self._run([self.binary, "ps", "--format", "{{.Names}}"], timeout=15)
        running = [n[len(_NAME_PREFIX):] for n in ps.stdout.split()
                   if n.startswith(_NAME_PREFIX)]
        return {
            "runtime": self.binary,
            "version": ver.stdout.strip() or None,
            "available": ok.exit_code == 0,
            "reason": None if ok.exit_code == 0 else (ok.stderr.strip()[:200] or "runtime error"),
            "image": self.image,
            "image_present": img.exit_code == 0,
            "workspaces": sorted(running),
        }

    def ensure_workspace(self, name: str) -> None:
        cname = self.container_name(name)
        os.makedirs(self.workspace_dir(name), exist_ok=True)
        state = self._run([self.binary, "inspect", "-f", "{{.State.Status}}", cname], timeout=20)
        if state.exit_code == 0:
            status = state.stdout.strip()
            if status == "running":
                return                                    # adopt it — reconcile is free
            # `start` fails on a paused container (it needs `unpause`, not `start`) — dispatch on
            # the reported state rather than let a call we know will fail surface a raw error.
            if status == "paused":
                resumed = self._run([self.binary, "unpause", cname], timeout=30)
            else:
                resumed = self._run([self.binary, "start", cname], timeout=30)
            if not resumed.ok:
                raise SandboxUnavailable(
                    f"could not resume the sandbox container (state: {status}): "
                    f"{resumed.stderr.strip()[:300]}")
            return
        created = self._run(self._run_argv(name), timeout=60)
        if not created.ok:
            raise SandboxUnavailable(
                f"could not start the sandbox container: {created.stderr.strip()[:300]}")

    def exec(self, name: str, argv: list[str], *, stdin: str = "",
             timeout: float = 120.0, run_id: str = "") -> ExecResult:
        self.ensure_workspace(name)
        self._last_exec[name] = time.time()
        result = self._run([self.binary, "exec", "-i", self.container_name(name), *argv],
                           stdin=stdin, timeout=timeout)
        # After _last_exec[name] is updated, never before — so the workspace this very call just
        # used can never be the one the sweep reaps.
        self._maybe_stop_idle()
        return result

    def stop(self, name: str) -> None:
        """Stopping a container on a runtime that is gone is a no-op, not an error: the opportunistic
        idle sweep (_maybe_stop_idle) calls this from inside exec(), where an exception would just be
        noise."""
        try:
            self._run([self.binary, "stop", "-t", "2", self.container_name(name)], timeout=30)
        except SandboxUnavailable:
            pass
        self._last_exec.pop(name, None)

    def stop_idle(self, idle_seconds: float) -> list[str]:
        """Stop workspaces with no exec for `idle_seconds`. Called opportunistically by
        _maybe_stop_idle(); also safe to call directly (e.g. from a dashboard action)."""
        now = time.time()
        stopped = []
        for name, last in list(self._last_exec.items()):
            if now - last >= idle_seconds:
                # stop() already swallows SandboxUnavailable internally (see its docstring) and
                # raises nothing else, so there is no exception here to catch — a handler around
                # it would imply a failure mode that cannot occur.
                self.stop(name)
                stopped.append(name)
        return stopped

    def _maybe_stop_idle(self) -> None:
        """Opportunistic idle sweep, same shape as TraceStore._maybe_prune (engine/trace/store.py):
        no background task, just a check tacked onto a call that was going to happen anyway. Must
        be called from the END of exec(), after _last_exec[name] is bumped, so a workspace cannot
        reap itself on the very call that just used it.

        HONEST LIMITATION: this is the only trigger. If the model calls exec_python once and never
        again, nothing runs the sweep until either another exec() happens (any workspace, since this
        method lives on the shared runtime, not per-workspace) or the process restarts — there is no
        timer. Engine.__init__ stopping leftover argus-ws-* containers at startup is the cross-process
        backstop for exactly that case."""
        now = time.time()
        if now - self._last_sweep < self._sweep_interval_s:
            return
        self._last_sweep = now
        try:
            self.stop_idle(self.idle_minutes * 60)
        except Exception:
            pass   # the sweep must never break the exec() call it rode in on
