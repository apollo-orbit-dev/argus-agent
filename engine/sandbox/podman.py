"""The real container runtime. The ONLY module in Argus that builds a podman argv.

Every command is idempotent, because Argus restarts more often than containers do: ensure_workspace
adopts a container that is already running rather than creating a second one, which is what makes
reconcile-on-restart free.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time

from engine.sandbox.runtime import ExecResult, SandboxUnavailable, validate_workspace

log = logging.getLogger("argus.sandbox")

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

# Which cgroup controller each resource cap depends on. A controller absent from
# `podman info --format "{{.Host.CgroupControllers}}"` means podman cannot enforce that flag at
# all — passing it anyway doesn't degrade gracefully, it makes `podman run` fail outright (see the
# module docstring in test_sandbox_podman.py's cgroup section for the real-world case: a host
# booted with `cgroup_disable=memory`, common on Raspberry Pi OS / ARM SBCs, reports controllers
# `[cpu pids]` with no `memory`). So each cap is only added to the argv when its controller is
# present, same TTL-cache shape as available() — this must not become another per-turn subprocess.
_CAP_CONTROLLER = {
    "--memory": "memory",
    "--pids-limit": "pids",
    "--cpus": "cpu",
}
_CGROUP_TTL_S = _AVAILABILITY_TTL_S


class PodmanRuntime:
    NETWORK_NAME = "argus-internal"
    EGRESS_NAME = "argus-egress"
    PROXY_PORT = 3128

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
        self._cgroup_cache: "tuple[float, frozenset[str]] | None" = None
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
        at all rather than the default bridge, which would silently hand it the LAN.

        `--user <host-uid>:<host-gid>` alongside `--userns=keep-id` is what makes the bind-mounted
        workspace writable on ANY host, not just one where the operator happens to be uid 1000 (see
        the Containerfile comment for the full story): the container process runs as exactly this
        process's own uid/gid, and keep-id maps that identity onto the same uid/gid on the host — so
        it already owns the directory it's writing into, because Argus itself (running as this same
        host user) is what created it.

        Resource caps (--memory/--pids-limit/--cpus) are added only for cgroup controllers this
        host's podman actually reports via `_resource_caps` — a controller a host doesn't have
        (e.g. `memory` on a kernel booted with `cgroup_disable=memory`, the default on Raspberry Pi
        OS and common on ARM SBCs) makes `podman run` fail outright if the flag is passed anyway, it
        does not degrade gracefully. Any cap that had to be dropped is logged and surfaced via
        status() — silently losing an isolation guarantee is never acceptable here."""
        caps, _dropped = self._resource_caps()
        return [
            self.binary, "run", "-d",
            "--name", self.container_name(name),
            "--network=none",
            "--userns=keep-id",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{self.workspace_dir(name)}:/home/argus:Z",
            "-w", "/home/argus",
            *caps,
            self.image, "sleep", "infinity",
        ]

    def _network_create_argv(self) -> list[str]:
        """--internal gives the network NO route off itself. Validated on the deploy host: a
        container on it cannot reach the internet, the LAN model host, or the Argus server."""
        return [self.binary, "network", "create", "--internal", self.NETWORK_NAME]

    def _proxy_run_argv(self) -> list[str]:
        return [self.binary, "run", "-d", "--name", self.EGRESS_NAME,
                "--network", self.NETWORK_NAME, self.image,
                "python", "/opt/argus/proxy.py", "--port", str(self.PROXY_PORT)]

    def _network_connect_argv(self) -> list[str]:
        """The sidecar's second leg. Being on BOTH networks is what makes it the only way out."""
        return [self.binary, "network", "connect", "podman", self.EGRESS_NAME]

    def _cgroup_controllers(self) -> frozenset:
        """Which cgroup controllers this host's podman actually has, per
        `podman info --format "{{.Host.CgroupControllers}}"` — the authoritative source (as opposed
        to guessing from the kernel/distro). Cached on the same TTL shape as available(): this is
        called from ensure_workspace's hot path (every container start), so an uncached call would
        turn every workspace start into an extra `podman info` subprocess."""
        now = time.time()
        if self._cgroup_cache is not None and now - self._cgroup_cache[0] < _CGROUP_TTL_S:
            return self._cgroup_cache[1]
        val = self._cgroup_controllers_uncached()
        self._cgroup_cache = (now, val)
        return val

    def _cgroup_controllers_uncached(self) -> frozenset:
        if not shutil.which(self.binary):
            return frozenset()
        try:
            r = self._run([self.binary, "info", "--format", "{{.Host.CgroupControllers}}"],
                          timeout=15)
        except SandboxUnavailable:
            return frozenset()
        if r.exit_code != 0:
            return frozenset()
        # Typical output is a bracketed, space-separated list, e.g. "[cpu io memory pids]" — strip
        # the brackets (harmless no-ops if they're already absent) and split.
        return frozenset(r.stdout.strip().strip("[]").split())

    def _resource_caps(self) -> "tuple[list[str], list[str]]":
        """Split the configured resource caps into (argv flags to actually pass, flags that had to
        be dropped because their cgroup controller isn't available). A dropped cap is a real
        reduction in isolation, so every drop is logged as a warning naming the cap and why — this
        must never fail silent."""
        controllers = self._cgroup_controllers()
        values = {"--memory": self.memory, "--pids-limit": str(self.pids_limit),
                  "--cpus": self.cpus}
        argv_caps: list[str] = []
        dropped: list[str] = []
        for flag, controller in _CAP_CONTROLLER.items():
            if controller in controllers:
                argv_caps += [flag, values[flag]]
            else:
                dropped.append(flag)
        if dropped:
            log.warning(
                "sandbox: cgroup controller(s) missing on this host (podman reports: %s) — "
                "dropping %s from the container's resource caps because they cannot be enforced; "
                "isolation is reduced accordingly",
                sorted(controllers) or ["none"], dropped)
        return argv_caps, dropped

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
                    "image": self.image, "workspaces": [], "dropped_limits": [],
                    "cgroup_controllers": []}
        ver = self._run([self.binary, "--version"], timeout=15)
        ok = self._run([self.binary, "info"], timeout=15)
        img = self._run([self.binary, "image", "exists", self.image], timeout=15)
        ps = self._run([self.binary, "ps", "--format", "{{.Names}}"], timeout=15)
        running = [n[len(_NAME_PREFIX):] for n in ps.stdout.split()
                   if n.startswith(_NAME_PREFIX)]
        # Same fields ensure_workspace's argv would actually use — so /sandbox/status (and the
        # dashboard's Sandbox card) show the operator exactly which caps this host cannot enforce,
        # rather than let a dropped --memory/--cpus/--pids-limit pass unnoticed (see _resource_caps).
        _caps, dropped = self._resource_caps()
        controllers = self._cgroup_controllers()
        return {
            "runtime": self.binary,
            "version": ver.stdout.strip() or None,
            "available": ok.exit_code == 0,
            "reason": None if ok.exit_code == 0 else (ok.stderr.strip()[:200] or "runtime error"),
            "image": self.image,
            "image_present": img.exit_code == 0,
            "workspaces": sorted(running),
            "dropped_limits": dropped,
            "cgroup_controllers": sorted(controllers),
        }

    def ensure_egress(self) -> None:
        """Idempotent: internal network + a running proxy sidecar attached to both networks."""
        if self._run([self.binary, "network", "exists", self.NETWORK_NAME], timeout=20).exit_code != 0:
            r = self._run(self._network_create_argv(), timeout=30)
            if not r.ok:
                raise SandboxUnavailable(
                    f"could not create the sandbox network: {r.stderr.strip()[:200]}")
        state = self._run([self.binary, "inspect", "-f", "{{.State.Status}}", self.EGRESS_NAME],
                          timeout=20)
        if state.exit_code == 0:
            if state.stdout.strip() != "running":
                self._run([self.binary, "start", self.EGRESS_NAME], timeout=30)
            return
        r = self._run(self._proxy_run_argv(), timeout=60)
        if not r.ok:
            raise SandboxUnavailable(f"could not start the egress proxy: {r.stderr.strip()[:200]}")
        # Second leg. If this fails the proxy exists but has no way out, which would look like
        # "the internet is down" from inside — so it is a hard failure, not a warning.
        c = self._run(self._network_connect_argv(), timeout=30)
        if not c.ok and "already" not in (c.stderr or "").lower():
            raise SandboxUnavailable(
                f"egress proxy has no outbound network: {c.stderr.strip()[:200]}")

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
