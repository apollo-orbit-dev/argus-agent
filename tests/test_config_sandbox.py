import pytest
from pydantic import ValidationError

from config import Config
from tests.test_config import _mk          # the repo's existing Config factory helper


def test_sandbox_is_off_by_default():
    c = _mk()
    assert c.enable_sandbox is False


def test_sandbox_defaults():
    c = _mk()
    assert c.sandbox_runtime == "podman"
    assert c.sandbox_image == "argus-sandbox:local"
    assert c.sandbox_workspace == "default"
    assert c.sandbox_instance == ""
    assert c.sandbox_idle_minutes == 30
    assert c.sandbox_exec_timeout == 120.0


def test_sandbox_fields_round_trip_through_env():
    c = _mk()
    for name in ("enable_sandbox", "sandbox_runtime", "sandbox_image", "sandbox_workspace",
                 "sandbox_instance", "sandbox_idle_minutes", "sandbox_exec_timeout"):
        assert name in c._ENV_FIELDS, f"{name} missing from _ENV_FIELDS"


# ---------------------------------------------------------------------------------------------
# Confused-deputy fix: sandbox_runtime/sandbox_image reach a subprocess (setup-sandbox.sh execs
# $RUNTIME; sandbox_image becomes a `podman build -t` argument), and PATCH /config has no
# admin gate. The schema itself must reject anything dangerous, so every consumer is covered.
# ---------------------------------------------------------------------------------------------
def test_sandbox_runtime_docker_is_coerced_to_podman():
    """docker was never actually implemented (PodmanRuntime is podman-only), so the dashboard option
    and config value were a footgun. A legacy `docker` value is coerced to podman rather than
    hard-rejected, so an existing .env doesn't crash on load — both at construction and via patch()."""
    c = _mk(sandbox_runtime="docker")
    assert c.sandbox_runtime == "podman"
    c2 = c.patch({"sandbox_runtime": "docker"})
    assert c2.sandbox_runtime == "podman"


def test_sandbox_image_valid_value_round_trips():
    c = _mk(sandbox_image="my-registry.example.com/argus/sandbox_img:v1.2")
    assert c.sandbox_image == "my-registry.example.com/argus/sandbox_img:v1.2"
    c2 = c.patch({"sandbox_image": "argus-sandbox:local"})
    assert c2.sandbox_image == "argus-sandbox:local"


def test_sandbox_runtime_rejects_an_arbitrary_path():
    """The confused-deputy finding: PATCH /config had no admin gate and could set sandbox_runtime
    to an absolute path that setup-sandbox.sh would later exec as the Argus user. Constraining the
    field to Literal["podman"] makes pydantic reject this before it reaches a subprocess."""
    with pytest.raises(ValidationError):
        _mk(sandbox_runtime="/tmp/evil")
    c = _mk()
    with pytest.raises(ValidationError):
        c.patch({"sandbox_runtime": "/tmp/evil"})


def test_sandbox_image_rejects_a_flag_like_value():
    """sandbox_image is passed to `podman build -t <image>`; a leading '-' would be read as a flag."""
    with pytest.raises(ValidationError):
        _mk(sandbox_image="-rf")
    c = _mk()
    with pytest.raises(ValidationError):
        c.patch({"sandbox_image": "-rf"})


def test_sandbox_image_rejects_whitespace():
    with pytest.raises(ValidationError):
        _mk(sandbox_image="argus sandbox:local")
    c = _mk()
    with pytest.raises(ValidationError):
        c.patch({"sandbox_image": "argus sandbox:local"})


def test_sandbox_instance_valid_value_round_trips():
    c = _mk(sandbox_instance="dev")
    assert c.sandbox_instance == "dev"
    c2 = c.patch({"sandbox_instance": ""})
    assert c2.sandbox_instance == ""


@pytest.mark.parametrize("bad", ["-x", "a b", "A", "a" * 17])
def test_sandbox_instance_rejects_invalid_values(bad):
    """sandbox_instance reaches a podman argv (container/network names). PATCH /config has no admin
    gate, so the schema must reject a leading-flag value, whitespace, uppercase, and anything over
    the 16-char limit before it can ever get there — mirrors the sandbox_image confused-deputy
    tests."""
    with pytest.raises(ValidationError):
        _mk(sandbox_instance=bad)
    c = _mk()
    with pytest.raises(ValidationError):
        c.patch({"sandbox_instance": bad})


def test_sandbox_network_defaults_to_proxy():
    from tests.test_config import _mk
    assert _mk().sandbox_network == "proxy"


@pytest.mark.parametrize("mode", ["proxy", "lan", "none"])
def test_sandbox_network_accepts_the_three_modes(mode):
    from tests.test_config import _mk
    assert _mk(sandbox_network=mode).sandbox_network == mode


def test_sandbox_network_rejects_anything_else():
    """It selects a security posture, so an unrecognised value must fail loudly rather than
    falling through to some default the operator did not choose."""
    from tests.test_config import _mk
    with pytest.raises(ValidationError):
        _mk(sandbox_network="wide-open")
