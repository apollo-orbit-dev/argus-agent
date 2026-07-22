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

# ensure_egress() is idempotent but not cheap — up to five `podman` subprocess calls (network
# exists, sidecar inspect x2, run/start/unpause, network connect). It used to run exactly once, at
# Engine construction; that leaves a dead sidecar (podman not ready yet at boot, or the sidecar
# container dying later) permanently invisible, because nothing ever checks again. ensure_workspace()
# now re-checks it on every call in `proxy` mode (see `_egress_ready_cached`) so a broken sidecar
# gets repaired on the next turn instead of only at the next restart — but ensure_workspace runs on
# every single exec(), so this must be cached the same TTL shape as available()/cgroup controllers,
# or every turn pays for several extra subprocess calls.
_EGRESS_TTL_S = _AVAILABILITY_TTL_S


class PodmanRuntime:
    NETWORK_NAME = "argus-internal"
    EGRESS_NAME = "argus-egress"
    PROXY_PORT = 3128
    # The sidecar's second leg (see _network_connect_argv): the default outbound-capable network
    # podman creates for every install, named literally "podman". Not to be confused with
    # self.binary, which happens to have the same value by default.
    OUTBOUND_NETWORK = "podman"

    def __init__(self, binary: str = "podman", image: str = "argus-sandbox:local",
                 workspaces_root: str = "data/workspaces", memory: str = "2g",
                 pids_limit: int = 256, cpus: str = "2", idle_minutes: int = 30,
                 sweep_interval_s: float = 60.0, network_mode: str = "proxy"):
        self.binary = binary
        self.image = image
        self.workspaces_root = os.path.abspath(workspaces_root)
        self.network_mode = network_mode
        self.memory = memory
        self.pids_limit = pids_limit
        self.cpus = cpus
        self._last_exec: dict[str, float] = {}
        self._avail_cache: "tuple[float, bool] | None" = None
        self._cgroup_cache: "tuple[float, frozenset[str]] | None" = None
        # (timestamp, ready, reason) — see _egress_ready_cached. reason is "" when ready.
        self._egress_cache: "tuple[float, bool, str] | None" = None
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

    def _network_flags(self) -> list[str]:
        """proxy: the internal network, whose only exit is the sidecar. none: no network at all.
        lan: the default bridge — full reach, including this LAN. The env vars are belt-and-braces;
        the actual enforcement is that in `proxy` mode there is no route anywhere else."""
        if self.network_mode == "none":
            return ["--network=none"]
        if self.network_mode == "lan":
            return []
        proxy = f"http://{self.EGRESS_NAME}:{self.PROXY_PORT}"
        return ["--network", self.NETWORK_NAME,
                "-e", f"HTTP_PROXY={proxy}", "-e", f"HTTPS_PROXY={proxy}",
                "-e", f"http_proxy={proxy}", "-e", f"https_proxy={proxy}"]

    def _run_argv(self, name: str) -> list[str]:
        """Network mode (`self.network_mode`, see `_network_flags`): "proxy" (default) puts the
        container on the internal network with no route off it except the egress sidecar; "lan"
        gives it the normal bridge (the documented escape hatch — a real loss of isolation); "none"
        gives it no network at all.

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
            *self._network_flags(),
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
        return [self.binary, "network", "connect", self.OUTBOUND_NETWORK, self.EGRESS_NAME]

    def _networks_argv(self, cname: str) -> list[str]:
        """Introspect which networks a container is actually attached to right now — the
        authoritative source, as opposed to a label or the argv it was (maybe) created with. Shared
        by `ensure_egress`'s sidecar-attachment check and `ensure_workspace`'s network-mismatch
        check (finding 1): both need "what is this container REALLY on", not what we assume."""
        return [self.binary, "inspect", "-f", "{{json .NetworkSettings.Networks}}", cname]

    def _proxy_liveness_argv(self) -> list[str]:
        """A short `podman exec` that opens a TCP connection to 127.0.0.1:PROXY_PORT from INSIDE
        the sidecar — the only way to prove something is actually LISTENING there, as opposed to
        merely `running`: `podman run -d` exits 0 the instant the container process starts, even
        if `python /opt/argus/proxy.py` dies immediately after — which it will on any image built
        before this branch, since /opt/argus/ did not exist then. python3 is guaranteed present
        (the proxy itself is python), so this needs no extra tooling in the image."""
        probe = ("import socket; s = socket.socket(); s.settimeout(3); "
                 f"s.connect(('127.0.0.1', {self.PROXY_PORT})); s.close()")
        return [self.binary, "exec", self.EGRESS_NAME, "python3", "-c", probe]

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

    def _egress_ready_cached(self) -> "tuple[bool, str]":
        """TTL-cached (`_EGRESS_TTL_S`, same shape as `available()`/`_cgroup_controllers`) wrapper
        around `ensure_egress()`. Backs both `ensure_workspace()`'s repair-on-use path and
        `status()`'s `egress_ready` field, so the two can never disagree about how stale the last
        check was.

        Only meaningful in `proxy` mode — callers must gate on `self.network_mode == "proxy"`
        themselves; this method always attempts the real check (it has no idea what mode it's
        being called under, and calling it in `lan`/`none` would wrongly create the internal
        network and a sidecar that mode has no use for).

        Never raises: a failed `ensure_egress()` is caught, logged once per TTL window (not on
        every call — that would spam the log every single exec() while the sidecar is down), and
        cached as `(False, reason)` so callers can surface it without ever having a broken sidecar
        block a workspace container from starting."""
        now = time.time()
        if self._egress_cache is not None and now - self._egress_cache[0] < _EGRESS_TTL_S:
            _, ready, reason = self._egress_cache
            return ready, reason
        try:
            self.ensure_egress()
            self._egress_cache = (now, True, "")
            return True, ""
        except SandboxUnavailable as e:
            reason = str(e)
            self._egress_cache = (now, False, reason)
            log.warning("sandbox egress proxy is not ready: %s", reason)
            return False, reason

    def _egress_status_fields(self) -> dict:
        """`egress_ready`/`egress_reason` for status(). Only `proxy` mode ever runs a sidecar —
        reporting a bare `False` for `lan`/`none` would read as "egress is broken" when there is in
        fact no egress sidecar to be broken, so those modes get `None` plus an explanatory reason
        instead."""
        if self.network_mode != "proxy":
            return {"egress_ready": None,
                    "egress_reason": f"not applicable — sandbox_network is {self.network_mode!r}"}
        ready, reason = self._egress_ready_cached()
        return {"egress_ready": ready, "egress_reason": reason or None}

    def status(self) -> dict:
        if not shutil.which(self.binary):
            return {"runtime": self.binary, "available": False, "reason": "binary not found",
                    "image": self.image, "workspaces": [], "dropped_limits": [],
                    "cgroup_controllers": [], "network": self.network_mode,
                    **self._egress_status_fields()}
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
            "network": self.network_mode,
            "workspaces": sorted(running),
            "dropped_limits": dropped,
            "cgroup_controllers": sorted(controllers),
            **self._egress_status_fields(),
        }

    def ensure_egress(self) -> None:
        """Idempotent: internal network + a running proxy sidecar attached to both networks.

        The second network leg is verified on EVERY call, not just performed once when the sidecar
        is first created. A prior run of this method could have created the sidecar (`podman run`
        succeeding) and then failed the `network connect` step — e.g. process killed mid-call, host
        hiccup — leaving a container that is genuinely `running` but only on the internal network,
        with no way out. Because `inspect` would report it `running`, a version of this method that
        returned as soon as it saw "running" would adopt that half-wired sidecar forever: every
        subsequent call sees "running", returns immediately, and the connect that would fix it never
        runs again. So the shape here is "ensure running" THEN "ensure attached", every time,
        regardless of which branch got the container to running.

        A THIRD check follows attachment: "ensure listening" (`_ensure_egress_listening`, finding
        2). `running` and `attached` are both still just container/network state — neither proves
        the proxy process itself is alive and bound to its port.
        """
        if self._run([self.binary, "network", "exists", self.NETWORK_NAME], timeout=20).exit_code != 0:
            r = self._run(self._network_create_argv(), timeout=30)
            if not r.ok:
                raise SandboxUnavailable(
                    f"could not create the sandbox network: {r.stderr.strip()[:200]}")
        state = self._run([self.binary, "inspect", "-f", "{{.State.Status}}", self.EGRESS_NAME],
                          timeout=20)
        if state.exit_code == 0:
            status = state.stdout.strip()
            if status != "running":
                # `start` fails on a paused container (it needs `unpause`, not `start`) — same
                # dispatch ensure_workspace uses, for the same reason.
                if status == "paused":
                    resumed = self._run([self.binary, "unpause", self.EGRESS_NAME], timeout=30)
                else:
                    resumed = self._run([self.binary, "start", self.EGRESS_NAME], timeout=30)
                if not resumed.ok:
                    raise SandboxUnavailable(
                        f"could not resume the egress proxy (state: {status}): "
                        f"{resumed.stderr.strip()[:300]}")
        else:
            r = self._run(self._proxy_run_argv(), timeout=60)
            if not r.ok:
                raise SandboxUnavailable(
                    f"could not start the egress proxy: {r.stderr.strip()[:200]}")
        self._ensure_egress_attached()
        self._ensure_egress_listening()

    def _ensure_egress_attached(self) -> None:
        """Verify the sidecar is actually attached to the outbound network, and attach it if not.
        Called every time ensure_egress gets (or adopts) a running sidecar — not just right after
        creating one — so a sidecar that came up running-but-unconnected on some earlier call gets
        repaired instead of adopted as-is forever (see ensure_egress's docstring).

        Inspecting `.NetworkSettings.Networks` is the authoritative check: it reflects what the
        container is actually attached to right now. That is preferred over the previous approach
        of string-matching "already" in `network connect`'s stderr to tell "already attached" apart
        from a real failure — that string was never verified against real podman wording, so a
        phrasing change (or a different podman version) could silently swallow a genuine failure.
        Inspect-then-connect makes no assumption about error text at all: if the network is missing,
        connect; if connect then fails, that failure is real."""
        nets = self._run(self._networks_argv(self.EGRESS_NAME), timeout=20)
        if nets.ok and f'"{self.OUTBOUND_NETWORK}":' in (nets.stdout or ""):
            return
        c = self._run(self._network_connect_argv(), timeout=30)
        if not c.ok:
            raise SandboxUnavailable(
                f"egress proxy has no outbound network: {c.stderr.strip()[:200]}")

    def _ensure_egress_listening(self) -> None:
        """Finding 2 (IMPORTANT): container *state* is not proof the proxy inside it is listening.
        `podman run -d` reports success the instant the sidecar process starts, whether or not
        `python /opt/argus/proxy.py` itself then dies (e.g. because it doesn't exist in an image
        built before this branch). Confirm something actually accepts a TCP connection on the
        proxy port from inside the container before calling egress ready — never trust `running`
        alone. This runs inside `ensure_egress()`, which is only ever invoked through
        `_egress_ready_cached()`'s TTL cache on the hot path, so it does not add a probe to every
        single exec()."""
        live = self._run(self._proxy_liveness_argv(), timeout=10)
        if not live.ok:
            raise SandboxUnavailable(
                "the egress proxy container is running but nothing is listening on "
                f"127.0.0.1:{self.PROXY_PORT} inside it — this usually means the image predates "
                "the egress proxy (/opt/argus/proxy.py did not exist when it was built); rebuild "
                f"the sandbox image via scripts/setup-sandbox.sh ({live.stderr.strip()[:200]})")

    def _container_network_matches_mode(self, cname: str) -> bool:
        """Finding 1 (CRITICAL): a container's network is fixed at CREATE time — `podman start` on
        an exited container replays the original create-time config, it never re-applies
        `--network`/`--network=none`. So a container created under yesterday's `sandbox_network`
        (or created before this stage existed at all) keeps that stale network forever unless it is
        actually recreated. This inspects the container's REAL attachment (`.NetworkSettings.Networks`
        — never a label we wrote ourselves, which could just be stale in the same way) and compares
        it against `self.network_mode` right now.

        `none` matches only an empty/absent network map; `proxy` matches only if `NETWORK_NAME` is
        among the attached networks; `lan` matches anything else non-empty (the default bridge, by
        name, differs across podman versions/hosts, so "attached to something, and specifically not
        the internal-only network" is the honest thing to check rather than guessing a literal
        name). An inspect that fails outright (e.g. the container vanished between the state check
        and here) is treated as a mismatch — recreating is always safe (see ensure_workspace), so
        that is the conservative direction to fail in."""
        nets = self._run(self._networks_argv(cname), timeout=20)
        networks_json = (nets.stdout or "").strip() if nets.ok else ""
        is_empty = networks_json in ("", "null", "{}")
        has_internal = f'"{self.NETWORK_NAME}":' in networks_json
        if self.network_mode == "none":
            return is_empty
        if self.network_mode == "proxy":
            return has_internal
        return not is_empty and not has_internal   # lan

    def ensure_workspace(self, name: str) -> None:
        if self.network_mode == "proxy":
            # Best-effort, TTL-cached repair: a startup ensure_egress() failure (podman not ready
            # yet) or a sidecar that died mid-process must not stay invisible until the whole
            # process restarts. This never raises and never blocks the workspace container below
            # from being attempted, even when the sidecar can't be readied — the container just
            # starts on `argus-internal` with nowhere to send traffic, same as before this fix,
            # except now it's actually retried and surfaced via status() instead of frozen forever.
            self._egress_ready_cached()
        cname = self.container_name(name)
        os.makedirs(self.workspace_dir(name), exist_ok=True)
        state = self._run([self.binary, "inspect", "-f", "{{.State.Status}}", cname], timeout=20)
        if state.exit_code == 0:
            if not self._container_network_matches_mode(cname):
                # Recreating is safe: these containers are stateless, the bind-mounted workspace
                # holds all state, and the create path right below already handles "the container
                # doesn't exist" — this just makes that path reachable for "it exists but is wrong"
                # too, instead of adopting (running) or resurrecting (start/unpause) a container
                # whose network will never match `sandbox_network` again until the process restarts.
                log.info(
                    "sandbox: %s's network attachment does not match the configured sandbox_network "
                    "(%r) — removing and recreating it so the new network mode actually takes "
                    "effect", cname, self.network_mode)
                self._run([self.binary, "rm", "-f", cname], timeout=30)
                # fall through to the create path below, exactly as if it never existed
            else:
                status = state.stdout.strip()
                if status == "running":
                    return                                # adopt it — reconcile is free
                # `start` fails on a paused container (it needs `unpause`, not `start`) — dispatch
                # on the reported state rather than let a call we know will fail surface a raw error.
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
