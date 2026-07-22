"""The setup endpoint runs ONE vendored script and takes no command argument, so it cannot be
turned into a remote shell. That property is what these tests exist to pin."""
import inspect
import tempfile

from fastapi.testclient import TestClient

from backend.app import create_app
from engine.engine import Engine
from tests.test_config import _mk


# sandbox_runtime is a Literal["podman", "docker"] at the Config level (config.py) — a PATCH
# /config caller can no longer hand it an arbitrary path. test_status_reports_enabled_but_unavailable
# still wants a binary that is GENUINELY missing (not simulated), so this sentinel is routed around
# Config and patched directly onto the constructed PodmanRuntime — a real, unmocked "not found".
_FAKE_BINARY = "definitely-not-a-real-binary"


def _client(**over):
    force_missing = over.get("sandbox_runtime") == _FAKE_BINARY
    if force_missing:
        over["sandbox_runtime"] = "podman"   # placeholder; must be Literal-valid to build Config
    eng = Engine(_mk(**over), data_dir=tempfile.mkdtemp())
    if force_missing:
        eng.sandbox.binary = _FAKE_BINARY
    return TestClient(create_app(eng))


def _route(app, path, method):
    for r in app.routes:
        if getattr(r, "path", None) == path and method in getattr(r, "methods", ()):
            return r
    raise AssertionError(f"no route registered for {method} {path}")


def test_status_reports_disabled_by_default():
    r = _client(enable_sandbox=False).get("/sandbox/status")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_status_reports_enabled_but_unavailable():
    r = _client(enable_sandbox=True, sandbox_runtime="definitely-not-a-real-binary").get(
        "/sandbox/status")
    body = r.json()
    assert body["enabled"] is True and body["available"] is False and body["reason"]


def test_setup_endpoint_takes_no_command_argument():
    """A body with a `command` key must be ignored entirely — the handler signature must not accept
    one, so an attacker cannot smuggle a command through it. Asserted by introspecting the actual
    registered route's endpoint signature (not by string-matching source), so it can't be fooled by
    a differently-shaped handler that happens to contain the right substrings."""
    # Introspect the actual registered route rather than actually calling the endpoint: calling it
    # would run the real (vendored, no-argument) setup-sandbox.sh, which is slow and would fail here
    # since podman isn't installed in this environment. Signature inspection proves the no-argument
    # property without ever executing the script.
    app = create_app(Engine(_mk(), data_dir=tempfile.mkdtemp()))
    route = _route(app, "/sandbox/setup", "POST")
    params = inspect.signature(route.endpoint).parameters
    allowed = {"request"}
    assert set(params) <= allowed, (
        f"setup handler must accept only {allowed}, got {set(params)} — any extra parameter "
        "(e.g. a request body) is a potential command-smuggling vector"
    )


def test_setup_is_admin_gated():
    from backend import app as app_mod
    src = inspect.getsource(app_mod.create_app)
    idx = src.index("/sandbox/setup")
    assert "_require_admin" in src[idx:idx + 600]
