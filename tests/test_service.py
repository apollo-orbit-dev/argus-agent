import sys
from pathlib import Path

import pytest

from engine import service


def test_render_unit_has_required_directives():
    text = service.render_unit(Path("/opt/argus"), Path("/opt/argus/.venv/bin/argus"), "Argus agent (argus)")
    assert "[Unit]" in text and "[Service]" in text and "[Install]" in text
    assert "Description=Argus agent (argus)" in text
    assert "WorkingDirectory=/opt/argus" in text
    assert "ExecStart=/opt/argus/.venv/bin/argus run" in text
    assert "Type=simple" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text


def test_render_unit_quotes_exec_path_with_spaces():
    text = service.render_unit(Path("/home/x/My Argus"), Path("/home/x/My Argus/.venv/bin/argus"), "d")
    # systemd tokenizes ExecStart on spaces, so the binary must be double-quoted; the ` run` arg stays outside.
    assert 'ExecStart="/home/x/My Argus/.venv/bin/argus" run' in text
    # WorkingDirectory is a single-value setting — spaces are fine unquoted.
    assert "WorkingDirectory=/home/x/My Argus" in text


def test_port_from_env_reads_env(tmp_path):
    (tmp_path / ".env").write_text("HOST=0.0.0.0\nPORT=8711\n# PORT=9999\n")
    assert service._port_from_env(tmp_path) == 8711


def test_port_from_env_defaults_when_missing(tmp_path):
    assert service._port_from_env(tmp_path) == 8700


def test_service_supported_false_off_linux(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    ok, reason = service.service_supported()
    assert ok is False and "Linux" in reason


def test_service_supported_false_without_systemctl(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda _n: None)
    ok, reason = service.service_supported()
    assert ok is False and "systemctl" in reason


def test_service_supported_true_when_user_bus_reachable(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda _n: "/usr/bin/systemctl")
    monkeypatch.setattr(service, "_run", lambda argv, timeout=20.0: (0, "LANG=C", ""))
    ok, reason = service.service_supported()
    assert ok is True and reason == ""
