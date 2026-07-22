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


def _argv(tmp_path, mode):
    return PodmanRuntime(workspaces_root=str(tmp_path), network_mode=mode)._run_argv("default")


def test_proxy_mode_joins_the_internal_network_and_sets_proxy_env(tmp_path):
    argv = _argv(tmp_path, "proxy")
    joined = " ".join(argv)
    assert "--network argus-internal" in joined
    assert "--network=none" not in joined
    assert "HTTP_PROXY=http://argus-egress:3128" in joined
    assert "HTTPS_PROXY=http://argus-egress:3128" in joined


def test_none_mode_is_airgapped_and_sets_no_proxy_env(tmp_path):
    argv = _argv(tmp_path, "none")
    joined = " ".join(argv)
    assert "--network=none" in joined
    assert "PROXY" not in joined


def test_lan_mode_is_the_documented_escape_hatch(tmp_path):
    """`lan` deliberately gives up the boundary. It must not silently look like `proxy` — no proxy
    env, and not on the internal network, or the user would think they were still constrained."""
    argv = _argv(tmp_path, "lan")
    joined = " ".join(argv)
    assert "argus-internal" not in joined
    assert "--network=none" not in joined
    assert "PROXY" not in joined


@pytest.mark.parametrize("mode", ["proxy", "lan", "none"])
def test_every_mode_still_mounts_only_the_workspace(tmp_path, mode):
    argv = _argv(tmp_path, mode)
    vols = [argv[i + 1] for i, a in enumerate(argv) if a == "-v"]
    assert len(vols) == 1 and vols[0].endswith("/default:/home/argus:Z")
    assert "tables.db" not in " ".join(argv) and "sessions.db" not in " ".join(argv)
