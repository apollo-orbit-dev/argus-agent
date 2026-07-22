"""The container-runtime seam.

Every container command in Argus goes through a SandboxRuntime. Nothing else shells out to podman.
That is what lets the default test suite run on a machine with no container runtime at all: the
logic is tested against FakeRuntime, and the real implementation is exercised only by tests marked
`podman`, which skip when the binary is absent.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# A workspace name is interpolated into a container name and a filesystem path, and ends up in a
# podman argv. Anything that could be read as a flag, a path component or a shell metacharacter is
# rejected here so it can never reach either.
WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class SandboxUnavailable(RuntimeError):
    """The container runtime is not usable. Callers turn this into a tool-level message."""


def validate_workspace(name: str) -> str:
    if not isinstance(name, str) or not WORKSPACE_RE.match(name):
        raise ValueError(
            f"invalid workspace name {name!r}: use lowercase letters, digits, '-' and '_', "
            "starting with a letter or digit, 32 characters max")
    return name


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


@runtime_checkable
class SandboxRuntime(Protocol):
    def available(self) -> bool: ...
    def status(self) -> dict: ...
    def ensure_workspace(self, name: str) -> None: ...
    def exec(self, name: str, argv: list[str], *, stdin: str = "",
             timeout: float = 120.0, run_id: str = "") -> ExecResult: ...
    def stop(self, name: str) -> None: ...
    def stop_idle(self, idle_seconds: float) -> list[str]: ...


@dataclass
class FakeRuntime:
    """In-memory SandboxRuntime for tests. Validates names exactly like the real one, so a test
    that passes here cannot pass a name the real runtime would reject."""

    available_: bool = True
    result: ExecResult | None = None
    calls: list = field(default_factory=list)
    started: set = field(default_factory=set)
    stopped: list = field(default_factory=list)

    def available(self) -> bool:
        return self.available_

    def status(self) -> dict:
        return {"runtime": "fake", "available": self.available_,
                "workspaces": sorted(self.started)}

    def ensure_workspace(self, name: str) -> None:
        validate_workspace(name)
        if not self.available_:
            raise SandboxUnavailable("fake runtime is unavailable")
        self.started.add(name)

    def exec(self, name: str, argv: list[str], *, stdin: str = "",
             timeout: float = 120.0, run_id: str = "") -> ExecResult:
        validate_workspace(name)
        if not self.available_:
            raise SandboxUnavailable("fake runtime is unavailable")
        self.calls.append((name, list(argv)))
        return self.result if self.result is not None else ExecResult(0, "", "")

    def stop(self, name: str) -> None:
        validate_workspace(name)
        self.started.discard(name)
        self.stopped.append(name)

    def stop_idle(self, idle_seconds: float) -> list[str]:
        return []
