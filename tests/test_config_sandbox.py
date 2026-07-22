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
    assert c.sandbox_idle_minutes == 30
    assert c.sandbox_exec_timeout == 120.0


def test_sandbox_fields_round_trip_through_env():
    c = _mk()
    for name in ("enable_sandbox", "sandbox_runtime", "sandbox_image", "sandbox_workspace",
                 "sandbox_idle_minutes", "sandbox_exec_timeout"):
        assert name in c._ENV_FIELDS, f"{name} missing from _ENV_FIELDS"
