"""PodmanRuntime.

The argv-shape tests run everywhere — they assert on the command we BUILD, not on running it, so
they catch the flag mistakes that matter (a missing --network=none is a silent loss of isolation)
without needing a container runtime. The end-to-end tests are marked `podman` and skip when the
binary is absent, which is the case in CI.
"""
import shutil

import pytest

from engine.sandbox.podman import PodmanRuntime
from engine.sandbox.runtime import validate_workspace

needs_podman = pytest.mark.skipif(shutil.which("podman") is None, reason="podman not installed")


def test_container_name_is_namespaced():
    rt = PodmanRuntime(workspaces_root="/tmp/ws")
    assert rt.container_name("default") == "argus-ws-default"


def test_run_argv_is_isolated_by_default(tmp_path):
    """Stage 1 has no egress proxy, so the container must have NO network at all. If this
    assertion is ever relaxed, the sandbox has quietly gained LAN access."""
    rt = PodmanRuntime(workspaces_root=str(tmp_path), image="argus-sandbox:local")
    argv = rt._run_argv("default")
    assert "--network=none" in argv
    assert "--userns=keep-id" in argv
    assert "argus-sandbox:local" in argv
    assert argv[-2:] == ["sleep", "infinity"]
    joined = " ".join(argv)
    assert "--privileged" not in joined
    assert "/var/run/docker.sock" not in joined
    assert "-v /:" not in joined


def test_run_argv_mounts_only_the_workspace(tmp_path):
    """Exactly one mount. The harness's SQLite stores must never be reachable from inside."""
    argv = PodmanRuntime(workspaces_root=str(tmp_path))._run_argv("default")
    vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(vols) == 1
    assert vols[0].endswith("/default:/home/argus:Z")
    assert "tables.db" not in " ".join(argv)
    assert "sessions.db" not in " ".join(argv)


def test_run_argv_caps_resources(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path), memory="2g", pids_limit=256, cpus="2")
    argv = rt._run_argv("default")
    assert "--memory" in argv and "2g" in argv
    assert "--pids-limit" in argv and "256" in argv


def test_bad_workspace_name_never_reaches_argv(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    with pytest.raises(ValueError):
        rt._run_argv("--privileged")


def test_available_is_false_when_the_binary_is_missing(tmp_path):
    rt = PodmanRuntime(binary="definitely-not-a-real-binary", workspaces_root=str(tmp_path))
    assert rt.available() is False


def test_stop_on_missing_binary_does_not_raise(tmp_path):
    """Stopping a container on a runtime that is gone is a no-op, not an error — the engine calls
    stop_idle() on a timer where an exception would be noise."""
    rt = PodmanRuntime(binary="definitely-not-a-real-binary", workspaces_root=str(tmp_path))
    rt.stop("default")  # must not raise


@needs_podman
@pytest.mark.podman
def test_end_to_end_exec_round_trip(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path), image="python:3.12-slim")
    try:
        rt.ensure_workspace("default")
        r = rt.exec("default", ["python", "-c", "print(6*7)"], timeout=60)
        assert r.ok and r.stdout.strip() == "42"
    finally:
        rt.stop("default")


@needs_podman
@pytest.mark.podman
def test_end_to_end_workspace_is_shared_with_the_host(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path), image="python:3.12-slim")
    try:
        rt.ensure_workspace("default")
        rt.exec("default", ["sh", "-c", "echo hi > /home/argus/from_container.txt"], timeout=60)
        assert (tmp_path / "default" / "from_container.txt").read_text().strip() == "hi"
    finally:
        rt.stop("default")


@needs_podman
@pytest.mark.podman
def test_end_to_end_has_no_network(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path), image="python:3.12-slim")
    try:
        rt.ensure_workspace("default")
        r = rt.exec("default", ["python", "-c",
                                "import socket;socket.create_connection(('1.1.1.1',80),2)"],
                    timeout=60)
        assert not r.ok, "stage 1 containers must have no network"
    finally:
        rt.stop("default")
