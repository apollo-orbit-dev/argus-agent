"""PodmanRuntime.

The argv-shape tests run everywhere — they assert on the command we BUILD, not on running it, so
they catch the flag mistakes that matter (a missing --network=none is a silent loss of isolation)
without needing a container runtime. The end-to-end tests are marked `podman` and skip when the
binary is absent, which is the case in CI.
"""
import os
import shutil
import subprocess
import time

import pytest

from engine.sandbox.podman import PodmanRuntime
from engine.sandbox.runtime import ExecResult, SandboxUnavailable, validate_workspace

needs_podman = pytest.mark.skipif(shutil.which("podman") is None, reason="podman not installed")


def _real_sandbox_image_present() -> bool:
    """True only if podman is installed AND the actual shipped image (argus-sandbox:local) has
    been built locally — there is no registry for it (see the Containerfile header), so on a fresh
    machine it doesn't exist until scripts/setup-sandbox.sh has been run.

    Short-circuits on a missing binary rather than letting the subprocess call raise, since this
    runs at collection time and CI/most dev machines don't have podman at all."""
    if shutil.which("podman") is None:
        return False
    try:
        r = subprocess.run(["podman", "image", "exists", "argus-sandbox:local"],
                           capture_output=True, timeout=15)
        return r.returncode == 0
    except Exception:
        return False


# Finding 4: python:3.12-slim (the old stand-in image for these tests) runs as root and was never
# built from our Containerfile, so it cannot catch a uid/permission defect in the image that
# actually ships (finding 3) or any other Containerfile regression. These tests now target
# argus-sandbox:local — the real image — and skip cleanly (rather than erroring against a missing
# image) with a reason that says how to get it.
needs_real_sandbox_image = pytest.mark.skipif(
    not _real_sandbox_image_present(),
    reason="argus-sandbox:local is not built locally — run scripts/setup-sandbox.sh to build it "
           "(these end-to-end tests intentionally target the real shipped image, not a stand-in)")


def test_container_name_is_namespaced():
    rt = PodmanRuntime(workspaces_root="/tmp/ws")
    assert rt.container_name("default") == "argus-ws-default"


def test_run_argv_is_isolated_by_default(tmp_path):
    """Stage 2 default is "proxy": the container joins the internal network, whose only exit is
    the policy-enforcing sidecar — never the unrestricted default bridge. See
    tests/test_sandbox_network.py for the full proxy/lan/none matrix."""
    rt = PodmanRuntime(workspaces_root=str(tmp_path), image="argus-sandbox:local")
    argv = rt._run_argv("default")
    assert "--network argus-internal" in " ".join(argv)
    assert "--network=none" not in argv
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


# ---------------------------------------------------------------------------------------------
# Finding 3 (IMPORTANT): the container must run as the INVOKING host user's own uid/gid, not a
# uid baked into the image — otherwise it only works on the one machine where the operator
# happens to be uid 1000.
# ---------------------------------------------------------------------------------------------
def test_run_argv_runs_as_the_invoking_hosts_own_uid_and_gid(tmp_path):
    argv = PodmanRuntime(workspaces_root=str(tmp_path))._run_argv("default")
    assert "--user" in argv
    assert argv[argv.index("--user") + 1] == f"{os.getuid()}:{os.getgid()}"


def test_run_argv_caps_resources(tmp_path):
    """All three caps are only ever passed when their cgroup controller is actually available
    (see the cgroup-controller-detection tests below) — mock a host that reports all three."""
    rt = PodmanRuntime(workspaces_root=str(tmp_path), memory="2g", pids_limit=256, cpus="2")
    rt._cgroup_cache = (time.time(), frozenset({"memory", "pids", "cpu"}))
    argv = rt._run_argv("default")
    assert "--memory" in argv and "2g" in argv
    assert "--pids-limit" in argv and "256" in argv
    assert "--cpus" in argv and "2" in argv


# ---------------------------------------------------------------------------------------------
# cgroup controller detection: on a host booted with `cgroup_disable=memory` (the default on
# Raspberry Pi OS and common on ARM SBCs), `podman info` reports controllers `[cpu pids]` with no
# `memory` — passing `--memory` anyway does not degrade gracefully, it makes `podman run` fail
# outright. Each cap must only be passed when its controller is actually available, and a dropped
# cap must never be silent (logged + surfaced via status()).
# ---------------------------------------------------------------------------------------------
def test_run_argv_omits_only_memory_when_memory_controller_is_missing(tmp_path, caplog):
    """The exact real-world case that broke the owner's server: controllers = [cpu pids]."""
    rt = PodmanRuntime(workspaces_root=str(tmp_path), memory="2g", pids_limit=256, cpus="2")
    rt._cgroup_cache = (time.time(), frozenset({"cpu", "pids"}))
    with caplog.at_level("WARNING"):
        argv = rt._run_argv("default")
    assert "--memory" not in argv
    assert "2g" not in argv
    assert "--pids-limit" in argv and "256" in argv
    assert "--cpus" in argv and "2" in argv
    # still isolated: on the internal network (default proxy mode) and exactly one bind mount,
    # untouched by the dropped cap
    assert "--network argus-internal" in " ".join(argv)
    vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(vols) == 1
    assert any("--memory" in r.message for r in caplog.records), \
        "a dropped cap must be logged, never silent"


def test_run_argv_omits_all_caps_when_no_controllers_are_reported(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path), memory="2g", pids_limit=256, cpus="2")
    rt._cgroup_cache = (time.time(), frozenset())
    argv = rt._run_argv("default")
    assert "--memory" not in argv
    assert "--pids-limit" not in argv
    assert "--cpus" not in argv
    # isolation properties that don't depend on cgroups must survive completely uncapped resources
    assert "--network argus-internal" in " ".join(argv)
    vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(vols) == 1
    assert vols[0].endswith("/default:/home/argus:Z")
    assert "tables.db" not in " ".join(argv)
    assert "sessions.db" not in " ".join(argv)


def test_cgroup_controllers_are_cached_and_not_shelled_out_on_every_call(tmp_path, monkeypatch):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    monkeypatch.setattr("engine.sandbox.podman.shutil.which", lambda b: "/usr/bin/podman")
    calls = []

    def fake_run(argv, *, stdin="", timeout=30.0):
        calls.append(argv)
        return ExecResult(0, "[cpu pids]\n", "")

    rt._run = fake_run
    assert rt._cgroup_controllers() == frozenset({"cpu", "pids"})
    assert rt._cgroup_controllers() == frozenset({"cpu", "pids"})
    assert len(calls) == 1, "a fresh reading within the TTL must not re-shell out to `podman info`"


def test_status_reports_dropped_caps(tmp_path, monkeypatch):
    rt = PodmanRuntime(workspaces_root=str(tmp_path), memory="2g", pids_limit=256, cpus="2")
    monkeypatch.setattr("engine.sandbox.podman.shutil.which", lambda b: "/usr/bin/podman")

    def fake_run(argv, *, stdin="", timeout=30.0):
        if "--version" in argv:
            return ExecResult(0, "podman version 5.4.2\n", "")
        if "--format" in argv:
            return ExecResult(0, "[cpu pids]\n", "")   # the real-world case: no `memory`
        if "info" in argv:
            return ExecResult(0, "", "")
        if "exists" in argv:
            return ExecResult(0, "", "")
        if "ps" in argv:
            return ExecResult(0, "", "")
        raise AssertionError(f"unexpected argv: {argv}")

    rt._run = fake_run
    body = rt.status()
    assert body["dropped_limits"] == ["--memory"]
    assert body["cgroup_controllers"] == ["cpu", "pids"]


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


# ---------------------------------------------------------------------------------------------
# Finding 2 (IMPORTANT): available() is on a hot per-turn path (plus /library, /tools). A short TTL
# cache must stop it from shelling out to `podman info` on every single call.
# ---------------------------------------------------------------------------------------------
def test_available_result_is_cached_within_the_ttl(tmp_path, monkeypatch):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    monkeypatch.setattr("engine.sandbox.podman.shutil.which", lambda b: "/usr/bin/podman")
    calls = []

    def fake_run(argv, *, stdin="", timeout=30.0):
        calls.append(argv)
        return ExecResult(0, "", "")

    rt._run = fake_run
    assert rt.available() is True
    assert rt.available() is True
    assert rt.available() is True
    assert len(calls) == 1, "a fresh reading within the TTL must not re-shell out to `podman info`"


def test_available_cache_expires_after_the_ttl(tmp_path, monkeypatch):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    monkeypatch.setattr("engine.sandbox.podman.shutil.which", lambda b: "/usr/bin/podman")
    calls = []

    def fake_run(argv, *, stdin="", timeout=30.0):
        calls.append(argv)
        return ExecResult(0, "", "")

    rt._run = fake_run
    assert rt.available() is True
    assert len(calls) == 1
    # Make the cached entry look stale without sleeping in the test.
    ts, val = rt._avail_cache
    rt._avail_cache = (ts - 999, val)
    assert rt.available() is True
    assert len(calls) == 2, "an expired cache entry must trigger a fresh check"


def test_available_caches_the_unavailable_outcome_too(tmp_path):
    """Both outcomes are cached, not just success — a missing binary is the cheap path already
    (shutil.which, no subprocess), but the cache must not special-case it."""
    rt = PodmanRuntime(binary="definitely-not-a-real-binary", workspaces_root=str(tmp_path))
    assert rt.available() is False
    assert rt._avail_cache is not None and rt._avail_cache[1] is False
    assert rt.available() is False


# ---------------------------------------------------------------------------------------------
# Finding 5 (IMPORTANT): the idle sweep must actually fire — from inside exec(), after the calling
# workspace's own timestamp is bumped, so it can never reap itself.
# ---------------------------------------------------------------------------------------------
def test_exec_sweeps_idle_workspaces_but_never_the_one_it_just_used(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path), idle_minutes=1)
    rt._sweep_interval_s = 0   # sweep on every exec() call for the test

    responses = {"inspect": ExecResult(0, "running\n", ""),
                "exec": ExecResult(0, "42\n", ""),
                "stop": ExecResult(0, "", "")}
    seen = []

    def fake_run(argv, *, stdin="", timeout=30.0):
        seen.append(argv[1])
        return responses[argv[1]]

    rt._run = fake_run
    rt._last_exec["stale"] = time.time() - 999   # idle well past the 1-minute threshold

    rt.exec("default", ["python", "-c", "1"])

    assert "default" in rt._last_exec, "the workspace this call just used must survive its own sweep"
    assert "stale" not in rt._last_exec, "an idle workspace must actually get reaped"
    assert "stop" in seen


def test_exec_does_not_sweep_before_the_sweep_interval_elapses(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path), idle_minutes=1, sweep_interval_s=3600)
    responses = {"inspect": ExecResult(0, "running\n", ""), "exec": ExecResult(0, "", "")}
    rt._run = lambda argv, *, stdin="", timeout=30.0: responses[argv[1]]
    rt._last_exec["stale"] = time.time() - 999

    rt.exec("default", ["python", "-c", "1"])

    assert "stale" in rt._last_exec, "the sweep interval hasn't elapsed yet — nothing should fire"


def _script_run(responses):
    """Return a fake `_run` that maps the podman subcommand (argv[1]) to a canned ExecResult,
    so ensure_workspace can be driven without a real podman binary."""
    def fake_run(argv, *, stdin="", timeout=30.0):
        return responses[argv[1]]
    return fake_run


def test_ensure_workspace_raises_sandbox_unavailable_when_start_fails_on_a_dead_container(tmp_path):
    """A container reported as `dead` (or `exited`, `stopping`) makes `podman start` fail. Before
    the fix, ensure_workspace ignored that and returned as if the workspace were ready — the
    caller then hit a raw podman error out of exec() instead of a clean SandboxUnavailable."""
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    rt._run = _script_run({
        "inspect": ExecResult(0, "dead\n", ""),
        "start": ExecResult(1, "", "Error: crun: cannot start a dead container: OCI error"),
    })
    with pytest.raises(SandboxUnavailable) as exc_info:
        rt.ensure_workspace("default")
    assert "dead" in str(exc_info.value)
    assert "cannot start a dead container" in str(exc_info.value)


def test_ensure_workspace_raises_sandbox_unavailable_when_start_fails_on_a_paused_container_and_unpause_also_fails(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    rt._run = _script_run({
        "inspect": ExecResult(0, "paused\n", ""),
        "unpause": ExecResult(1, "", "Error: cannot unpause: container gone"),
    })
    with pytest.raises(SandboxUnavailable) as exc_info:
        rt.ensure_workspace("default")
    assert "paused" in str(exc_info.value)
    assert "cannot unpause" in str(exc_info.value)


def test_ensure_workspace_unpauses_rather_than_starts_a_paused_container(tmp_path):
    """`podman start` fails on a paused container — it needs `unpause` instead. A successful
    unpause must be treated as the workspace being ready, same as an already-running one."""
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    calls = []

    def fake_run(argv, *, stdin="", timeout=30.0):
        calls.append(argv[1])
        if argv[1] == "inspect":
            return ExecResult(0, "paused\n", "")
        if argv[1] == "unpause":
            return ExecResult(0, "", "")
        raise AssertionError(f"unexpected podman subcommand: {argv[1]}")

    rt._run = fake_run
    rt.ensure_workspace("default")  # must not raise
    assert "unpause" in calls
    assert "start" not in calls


def test_ensure_workspace_returns_when_start_succeeds_on_an_exited_container(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    rt._run = _script_run({
        "inspect": ExecResult(0, "exited\n", ""),
        "start": ExecResult(0, "", ""),
    })
    rt.ensure_workspace("default")  # must not raise


# ---------------------------------------------------------------------------------------------
# ensure_egress() state machine. Finding 1 (CRITICAL): a sidecar that came up running-but-not-
# attached to the outbound network must NOT be adopted forever just because `inspect` reports it
# "running" — the attachment itself has to be re-checked (and repaired) on every call. Finding 2
# (CRITICAL): a failed `podman start` on the sidecar must raise, not be silently swallowed. Finding
# 3 (IMPORTANT): a paused sidecar needs `unpause`, not `start`, same as ensure_workspace.
# ---------------------------------------------------------------------------------------------
def _egress_fake_run(calls, *, network_exists=True, sidecar_state="running", attached=True,
                      network_create_ok=True, run_ok=True, connect_ok=True, start_ok=True,
                      unpause_ok=True, connect_stderr="", start_stderr="", unpause_stderr="",
                      run_stderr="", network_create_stderr=""):
    """Build a fake `_run` that drives the whole ensure_egress() state machine without a real
    podman binary: network exists/create, sidecar inspect (both the `.State.Status` query and the
    `.NetworkSettings.Networks` attachment query — dispatched on the format string, since both are
    `inspect` calls), run/start/unpause, and network connect.

    `sidecar_state=None` means the sidecar container does not exist yet (inspect fails). `attached`
    controls whether the NetworkSettings inspect reports the outbound ("podman") network already
    present — this is what finding 1's regression test flips to False on an otherwise-`running`
    sidecar."""
    def fake_run(argv, *, stdin="", timeout=30.0):
        calls.append(list(argv))
        if argv[1] == "network" and argv[2] == "exists":
            return ExecResult(0 if network_exists else 1, "", "")
        if argv[1] == "network" and argv[2] == "create":
            return ExecResult(0 if network_create_ok else 1, "", network_create_stderr)
        if argv[1] == "network" and argv[2] == "connect":
            return ExecResult(0 if connect_ok else 1, "", connect_stderr)
        if argv[1] == "inspect":
            fmt = argv[3]
            if "NetworkSettings" in fmt:
                if sidecar_state is None:
                    return ExecResult(1, "", "no such container")
                networks_json = ('{"argus-internal":{},"podman":{}}' if attached
                                  else '{"argus-internal":{}}')
                return ExecResult(0, networks_json, "")
            if sidecar_state is None:
                return ExecResult(1, "", "no such container")
            return ExecResult(0, sidecar_state + "\n", "")
        if argv[1] == "run":
            return ExecResult(0 if run_ok else 1, "", run_stderr)
        if argv[1] == "start":
            return ExecResult(0 if start_ok else 1, "", start_stderr)
        if argv[1] == "unpause":
            return ExecResult(0 if unpause_ok else 1, "", unpause_stderr)
        raise AssertionError(f"unexpected podman argv: {argv}")
    return fake_run


def _network_calls(calls, subcommand):
    return [c for c in calls if c[1] == "network" and c[2] == subcommand]


def test_ensure_egress_creates_the_network_when_missing(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    calls = []
    rt._run = _egress_fake_run(calls, network_exists=False)
    rt.ensure_egress()  # must not raise
    assert len(_network_calls(calls, "create")) == 1


def test_ensure_egress_does_not_recreate_an_existing_network(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    calls = []
    rt._run = _egress_fake_run(calls, network_exists=True)
    rt.ensure_egress()
    assert _network_calls(calls, "create") == []


def test_ensure_egress_runs_and_connects_a_missing_sidecar(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    calls = []
    rt._run = _egress_fake_run(calls, sidecar_state=None, attached=False)
    rt.ensure_egress()
    assert any(c[1] == "run" for c in calls)
    assert len(_network_calls(calls, "connect")) == 1


def test_ensure_egress_reattaches_a_running_but_unconnected_sidecar(tmp_path):
    """Finding 1's regression test. Before the fix, ensure_egress returned the instant `inspect`
    reported "running", without checking attachment at all — so a sidecar that was running but not
    on the outbound network would be adopted forever and the connect that would fix it would never
    run again. This must issue a fresh `network connect` every time it finds that gap, not just once
    at creation time."""
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    calls = []
    rt._run = _egress_fake_run(calls, sidecar_state="running", attached=False)
    rt.ensure_egress()  # must not raise
    assert len(_network_calls(calls, "connect")) == 1, (
        "a running-but-unattached sidecar must be reconnected, not silently adopted")


def test_ensure_egress_skips_the_redundant_connect_when_already_attached(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    calls = []
    rt._run = _egress_fake_run(calls, sidecar_state="running", attached=True)
    rt.ensure_egress()
    assert _network_calls(calls, "connect") == [], (
        "an already-attached sidecar must not trigger a redundant `network connect`")


def test_ensure_egress_unpauses_a_paused_sidecar(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    calls = []
    rt._run = _egress_fake_run(calls, sidecar_state="paused", attached=True)
    rt.ensure_egress()  # must not raise
    assert any(c[1] == "unpause" for c in calls)
    assert not any(c[1] == "start" for c in calls)


def test_ensure_egress_raises_when_start_fails(tmp_path):
    """Finding 2. Before the fix, `podman start`'s result on an existing non-running sidecar was
    discarded entirely — a failed start still returned as if egress were ready."""
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    calls = []
    rt._run = _egress_fake_run(calls, sidecar_state="exited", start_ok=False,
                                start_stderr="Error: crun: cannot start: OCI error")
    with pytest.raises(SandboxUnavailable) as exc_info:
        rt.ensure_egress()
    assert "exited" in str(exc_info.value)
    assert "cannot start" in str(exc_info.value)


def test_ensure_egress_raises_when_unpause_fails(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    calls = []
    rt._run = _egress_fake_run(calls, sidecar_state="paused", unpause_ok=False,
                                unpause_stderr="Error: cannot unpause: container gone")
    with pytest.raises(SandboxUnavailable) as exc_info:
        rt.ensure_egress()
    assert "paused" in str(exc_info.value)
    assert "cannot unpause" in str(exc_info.value)


def test_ensure_egress_raises_when_connect_fails_for_a_real_reason(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    calls = []
    rt._run = _egress_fake_run(calls, sidecar_state="running", attached=False, connect_ok=False,
                                connect_stderr="Error: network not found")
    with pytest.raises(SandboxUnavailable) as exc_info:
        rt.ensure_egress()
    assert "network not found" in str(exc_info.value)


@needs_real_sandbox_image
@pytest.mark.podman
def test_end_to_end_exec_round_trip(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    try:
        rt.ensure_workspace("default")
        r = rt.exec("default", ["python", "-c", "print(6*7)"], timeout=60)
        assert r.ok and r.stdout.strip() == "42"
    finally:
        rt.stop("default")


@needs_real_sandbox_image
@pytest.mark.podman
def test_end_to_end_workspace_is_shared_with_the_host(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    try:
        rt.ensure_workspace("default")
        rt.exec("default", ["sh", "-c", "echo hi > /home/argus/from_container.txt"], timeout=60)
        assert (tmp_path / "default" / "from_container.txt").read_text().strip() == "hi"
    finally:
        rt.stop("default")


@needs_real_sandbox_image
@pytest.mark.podman
def test_end_to_end_has_no_network(tmp_path):
    rt = PodmanRuntime(workspaces_root=str(tmp_path))
    try:
        rt.ensure_workspace("default")
        r = rt.exec("default", ["python", "-c",
                                "import socket;socket.create_connection(('1.1.1.1',80),2)"],
                    timeout=60)
        assert not r.ok, "stage 1 containers must have no network"
    finally:
        rt.stop("default")
