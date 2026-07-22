"""On Windows the sandbox can't run — the setup script is bash and the container model assumes a
Linux host with rootless podman + bind mounts. Rather than fail on a missing `bash`, both the status
readout and the setup button must say so plainly. These patch sys.platform so the real predicate is
exercised, not a stub.
"""
import sys
import tempfile

import pytest
from fastapi.testclient import TestClient

from backend import app as app_mod
from backend.app import create_app
from engine.engine import Engine
from engine.sandbox.runtime import SANDBOX_UNSUPPORTED_REASON, sandbox_supported
from tests.test_config import _mk


def _client(**over):
    return TestClient(create_app(Engine(_mk(**over), data_dir=tempfile.mkdtemp())))


def test_predicate_tracks_the_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert sandbox_supported() is False
    monkeypatch.setattr(sys, "platform", "linux")
    assert sandbox_supported() is True


def test_status_reports_unsupported_on_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    st = _client(enable_sandbox=True).get("/sandbox/status").json()
    assert st["supported"] is False
    assert st["available"] is False
    assert st["reason"] == SANDBOX_UNSUPPORTED_REASON


def test_setup_refuses_on_windows_without_shelling_out(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    # If the endpoint ever reaches the bash invocation on Windows, blow up — proving it returned early.
    def _boom(*a, **k):
        raise AssertionError("must not run the setup script on Windows")
    monkeypatch.setattr(app_mod, "_run_sandbox_setup", _boom)

    r = _client().post("/sandbox/setup")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["output"] == SANDBOX_UNSUPPORTED_REASON


def test_linux_is_unchanged(monkeypatch):
    """The default path must be byte-identical: no `supported` key sneaks in, sandbox still reads as
    just disabled when off."""
    monkeypatch.setattr(sys, "platform", "linux")
    st = _client(enable_sandbox=False).get("/sandbox/status").json()
    assert st == {"enabled": False, "available": False, "reason": "sandbox is disabled",
                  "runtime": "podman", "image": "argus-sandbox:local", "workspaces": []}
