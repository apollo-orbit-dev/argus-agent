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


@pytest.fixture
def unit_dir(tmp_path, monkeypatch):
    d = tmp_path / "systemd-user"
    monkeypatch.setattr(service, "_unit_dir", lambda: d)
    monkeypatch.setattr(service, "_user", lambda: "tester")
    # supported by default in these tests
    monkeypatch.setattr(service, "service_supported", lambda: (True, ""))
    return d


def _clone(tmp_path, port=8700):
    c = tmp_path / "clone"
    c.mkdir(exist_ok=True)
    (c / ".env").write_text(f"PORT={port}\n")
    return c


def test_unit_name_default(unit_dir, tmp_path):
    assert service.unit_name(_clone(tmp_path), 8700) == "argus.service"


def test_unit_name_override_gets_suffix(unit_dir, tmp_path):
    assert service.unit_name(_clone(tmp_path), 8700, "myargus") == "myargus.service"
    assert service.unit_name(_clone(tmp_path), 8700, "keep.service") == "keep.service"


def test_unit_name_second_clone_suffixes_by_port(unit_dir, tmp_path):
    # A different clone already owns argus.service → this clone must not collide.
    other = tmp_path / "other-clone"
    other.mkdir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "argus.service").write_text(service.render_unit(other, other / ".venv/bin/argus", "d"))
    assert service.unit_name(_clone(tmp_path, 8701), 8701) == "argus-8701.service"


def test_install_writes_unit_and_enables_but_does_not_start_when_up(unit_dir, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "_run", lambda argv, timeout=20.0: (calls.append(argv) or (0, "", "")))
    monkeypatch.setattr(service, "_port_open", lambda port, timeout=0.5: True)   # already running
    r = service.install(clone_dir=_clone(tmp_path))
    assert r["ok"] and r["enabled"] and r["linger_ok"]
    assert r["started"] is False
    assert (unit_dir / "argus.service").exists()
    joined = [" ".join(a) for a in calls]
    assert any("daemon-reload" in j for j in joined)
    assert any(j.startswith("systemctl --user enable argus.service") for j in joined)
    assert any("loginctl enable-linger tester" in j for j in joined)
    assert not any("start" in j for j in joined)          # never start when the port is up
    assert "next restart" in r["note"]


def test_install_surfaces_linger_failure(unit_dir, tmp_path, monkeypatch):
    def fake(argv, timeout=20.0):
        return (1, "", "Failed to enable linger") if "enable-linger" in argv else (0, "", "")
    monkeypatch.setattr(service, "_run", fake)
    monkeypatch.setattr(service, "_port_open", lambda port, timeout=0.5: True)
    r = service.install(clone_dir=_clone(tmp_path))
    assert r["ok"] and r["linger_ok"] is False
    assert "loginctl enable-linger tester" in r["note"]


def test_install_dry_run_writes_nothing(unit_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_run", lambda argv, timeout=20.0: pytest.fail("dry run must not shell out"))
    r = service.install(clone_dir=_clone(tmp_path), dry_run=True)
    assert r["ok"] and r["dry_run"] and "[Service]" in r["unit_text"]
    assert not (unit_dir / "argus.service").exists()


def test_uninstall_disables_and_removes_but_never_stops(unit_dir, tmp_path, monkeypatch):
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "argus.service").write_text("x")
    calls = []
    monkeypatch.setattr(service, "_run", lambda argv, timeout=20.0: (calls.append(argv) or (0, "", "")))
    r = service.uninstall(clone_dir=_clone(tmp_path))
    assert r["ok"] and r["removed"] is True
    assert not (unit_dir / "argus.service").exists()
    joined = [" ".join(a) for a in calls]
    assert any("disable argus.service" in j for j in joined)
    assert not any(" stop " in f" {j} " for j in joined)   # must not kill the live process


def test_uninstall_noop_when_absent(unit_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_run", lambda argv, timeout=20.0: (0, "", ""))
    r = service.uninstall(clone_dir=_clone(tmp_path))
    assert r["ok"] and r["removed"] is False


def test_status_parses_systemctl_output(unit_dir, tmp_path, monkeypatch):
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / "argus.service").write_text("x")
    def fake(argv, timeout=20.0):
        if "is-enabled" in argv: return (0, "enabled", "")
        if "is-active" in argv:  return (0, "active", "")
        if argv[0] == "loginctl": return (0, "Linger=yes", "")
        return (0, "", "")
    monkeypatch.setattr(service, "_run", fake)
    r = service.status(clone_dir=_clone(tmp_path))
    assert r["ok"] and r["installed"] and r["enabled"] and r["active"] and r["linger"]


def test_status_unsupported_returns_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "service_supported", lambda: (False, "nope"))
    r = service.status(clone_dir=_clone(tmp_path))
    assert r["ok"] is False and r["supported"] is False and r["reason"] == "nope"


from engine import cli


def test_cli_service_status_calls_service(monkeypatch, capsys):
    monkeypatch.setattr(service, "status", lambda name=None: {
        "ok": True, "supported": True, "name": "argus.service", "installed": True,
        "enabled": True, "active": True, "linger": False, "unit_path": "/x/argus.service"})
    rc = cli.main(["service", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "argus.service" in out and "enabled" in out


def test_cli_service_install_dry_run(monkeypatch, capsys):
    seen = {}
    def fake_install(name=None, dry_run=False):
        seen["dry_run"] = dry_run
        return {"ok": True, "dry_run": True, "name": "argus.service", "unit_text": "[Service]\n...",
                "unit_path": "/x/argus.service", "note": "dry run — nothing written"}
    monkeypatch.setattr(service, "install", fake_install)
    rc = cli.main(["service", "install", "--dry-run"])
    assert rc == 0 and seen["dry_run"] is True
    assert "[Service]" in capsys.readouterr().out


def test_cli_service_unsupported_returns_1(monkeypatch):
    monkeypatch.setattr(service, "status", lambda name=None: {"ok": False, "supported": False, "reason": "nope"})
    assert cli.main(["service", "status"]) == 1


def test_cli_service_uninstall_rejects_dry_run(monkeypatch, capsys):
    # --dry-run only means something for `install`; on uninstall it must NOT perform a real removal.
    monkeypatch.setattr(service, "uninstall", lambda name=None: pytest.fail("uninstall must not run under --dry-run"))
    rc = cli.main(["service", "uninstall", "--dry-run"])
    assert rc == 2
    assert "no effect" in capsys.readouterr().out
