"""Network wiring. Everything here asserts on the argv we BUILD — no podman required, which is what
lets these run in CI. The real-runtime behaviour was validated separately on the deploy host."""
import pytest

from engine.sandbox.podman import PodmanRuntime
from engine.sandbox.runtime import ExecResult, FakeRuntime


def _rt(tmp_path, **kw):
    return PodmanRuntime(workspaces_root=str(tmp_path), **kw)


def test_egress_argv_creates_an_internal_network(tmp_path):
    """--internal is what removes the container's route to the LAN. Without it the sidecar is
    pointless: the workspace could reach the internet directly."""
    argv = _rt(tmp_path)._network_create_argv()
    assert "--internal" in argv and "argus-internal" in argv


def test_proxy_run_argv_is_on_the_internal_network_and_runs_the_proxy(tmp_path):
    argv = _rt(tmp_path)._proxy_run_argv()
    assert "--network" in argv and "argus-internal" in argv
    assert "argus-egress" in argv
    assert "/opt/argus/proxy.py" in " ".join(argv)
    assert "3128" in " ".join(argv)


def test_proxy_gets_a_second_network_so_it_can_actually_reach_out(tmp_path):
    argv = _rt(tmp_path)._network_connect_argv()
    assert argv[:3] == ["podman", "network", "connect"]
    assert "argus-egress" in argv


def test_fake_runtime_tracks_egress():
    fake = FakeRuntime()
    assert fake.egress_ready is False
    fake.ensure_egress()
    assert fake.egress_ready is True
