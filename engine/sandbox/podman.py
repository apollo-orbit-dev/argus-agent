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


class PodmanRuntime:
    def __init__(self, binary: str = "podman", image: str = "argus-sandbox:local",
                 workspaces_root: str = "data/workspaces", memory: str = "2g",
                 pids_limit: int = 256, cpus: str = "2"):
        self.binary = binary
        self.image = image
        self.workspaces_root = os.path.abspath(workspaces_root)
        self.memory = memory
        self.pids_limit = pids_limit
        self.cpus = cpus
        self._last_exec: dict[str, float] = {}

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
            self._run([self.binary, "start", cname], timeout=30)
            return
        created = self._run(self._run_argv(name), timeout=60)
        if not created.ok:
            raise SandboxUnavailable(
                f"could not start the sandbox container: {created.stderr.strip()[:300]}")

    def exec(self, name: str, argv: list[str], *, stdin: str = "",
             timeout: float = 120.0, run_id: str = "") -> ExecResult:
        self.ensure_workspace(name)
        self._last_exec[name] = time.time()
        return self._run([self.binary, "exec", "-i", self.container_name(name), *argv],
                         stdin=stdin, timeout=timeout)

    def stop(self, name: str) -> None:
        """Stopping a container on a runtime that is gone is a no-op, not an error: the engine
        calls stop_idle() on a timer, where an exception would just be noise."""
        try:
            self._run([self.binary, "stop", "-t", "2", self.container_name(name)], timeout=30)
        except SandboxUnavailable:
            pass
        self._last_exec.pop(name, None)

    def stop_idle(self, idle_seconds: float) -> list[str]:
        """Stop workspaces with no exec for `idle_seconds`. Called by the engine's periodic sweep."""
        now = time.time()
        stopped = []
        for name, last in list(self._last_exec.items()):
            if now - last >= idle_seconds:
                try:
                    self.stop(name)
                    stopped.append(name)
                except Exception:
                    log.exception("failed to stop idle sandbox workspace %s", name)
        return stopped
