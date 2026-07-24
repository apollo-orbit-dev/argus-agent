"""Shared core for `argus service` — install/remove a **user-level** systemd unit so a self-hosted
Argus starts on boot. Used by both the `argus service` CLI (engine/cli.py) and the dashboard endpoints
(backend/app.py), so the two surfaces call ONE implementation. Stdlib-only, Linux/systemd only.

Every subprocess call goes through `_run`, the single seam tests monkeypatch — the suite never touches
real systemctl/loginctl.
"""
from __future__ import annotations

import getpass
import shutil
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # clone dir (holds main.py, .env)


def _run(argv: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """The one subprocess seam. Returns (returncode, stdout_stripped, stderr_stripped)."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timed out"


def _unit_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        import os
        return os.environ.get("USER") or os.environ.get("LOGNAME") or "user"


def _exec_path(clone_dir: Path) -> Path:
    return clone_dir / ".venv" / "bin" / "argus"


def _port_from_env(clone_dir: Path) -> int:
    try:
        for line in (clone_dir / ".env").read_text().splitlines():
            s = line.strip()
            if s.startswith("PORT=") and not s.startswith("#"):
                v = s.split("=", 1)[1].strip()
                if v.isdigit():
                    return int(v)
    except OSError:
        pass
    return 8700


def _port_open(port: int, timeout: float = 0.5) -> bool:
    with socket.socket() as s:
        s.settimeout(timeout)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def service_supported() -> tuple[bool, str]:
    if sys.platform != "linux":
        return False, f"systemd services are Linux-only (this host is {sys.platform})."
    if not shutil.which("systemctl"):
        return False, "systemctl not found — this feature needs systemd."
    rc, _, err = _run(["systemctl", "--user", "show-environment"])
    if rc != 0:
        return False, "no user systemd instance is reachable (`systemctl --user` failed)" + (f": {err}" if err else "")
    return True, ""


def render_unit(clone_dir: Path, exec_path: Path, description: str) -> str:
    exec_str = str(exec_path)
    exec_line = f'ExecStart="{exec_str}" run' if " " in exec_str else f"ExecStart={exec_str} run"
    return "\n".join([
        "[Unit]",
        f"Description={description}",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"WorkingDirectory={clone_dir}",
        exec_line,
        "Restart=on-failure",
        "RestartSec=3",
        "Environment=PYTHONUNBUFFERED=1",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def _points_here(unit_path: Path, clone_dir: Path) -> bool:
    """True if an existing unit file's WorkingDirectory is this clone (so re-install is an update,
    not a collision) — or if the file has no WorkingDirectory line at all, i.e. it isn't a
    recognizable rendered unit and there's no evidence it belongs to a *different* clone, so we
    don't force a needless port-suffixed rename onto our own uninstall/status lookups."""
    try:
        lines = unit_path.read_text().splitlines()
    except OSError:
        return False
    target = f"WorkingDirectory={clone_dir}"
    wd_lines = [ln.strip() for ln in lines if ln.strip().startswith("WorkingDirectory=")]
    if not wd_lines:
        return True
    return target in wd_lines


def unit_name(clone_dir: Path, port: int, override: str | None = None) -> str:
    if override:
        return override if override.endswith(".service") else f"{override}.service"
    default = _unit_dir() / "argus.service"
    if default.exists() and not _points_here(default, clone_dir):
        return f"argus-{port}.service"
    return "argus.service"


def install(name: str | None = None, *, dry_run: bool = False, clone_dir: Path = ROOT) -> dict:
    ok, reason = service_supported()
    if not ok:
        return {"ok": False, "reason": reason}
    port = _port_from_env(clone_dir)
    unit = unit_name(clone_dir, port, name)
    text = render_unit(clone_dir, _exec_path(clone_dir), f"Argus agent ({clone_dir.name})")
    unit_path = _unit_dir() / unit
    if dry_run:
        return {"ok": True, "dry_run": True, "name": unit, "unit_path": str(unit_path),
                "unit_text": text, "note": "dry run — nothing written"}
    _unit_dir().mkdir(parents=True, exist_ok=True)
    unit_path.write_text(text)
    _run(["systemctl", "--user", "daemon-reload"])
    rc_en, _, err_en = _run(["systemctl", "--user", "enable", unit])
    enabled = rc_en == 0
    rc_lg, _, _ = _run(["loginctl", "enable-linger", _user()])
    linger_ok = rc_lg == 0
    started = False
    if not _port_open(port):
        rc_st, _, _ = _run(["systemctl", "--user", "start", unit])
        started = rc_st == 0
    parts = [f"installed {unit}"]
    parts.append("started now" if started else "takes over on the next restart/reboot")
    if not enabled:
        parts.append(f"WARNING: enable failed ({err_en or 'unknown'})")
    if not linger_ok:
        parts.append(f"boot-start needs linger — run: loginctl enable-linger {_user()}")
    return {"ok": enabled, "name": unit, "unit_path": str(unit_path), "unit_text": text,
            "enabled": enabled, "linger_ok": linger_ok, "started": started, "note": " · ".join(parts)}


def uninstall(name: str | None = None, clone_dir: Path = ROOT) -> dict:
    ok, reason = service_supported()
    if not ok:
        return {"ok": False, "reason": reason}
    port = _port_from_env(clone_dir)
    unit = unit_name(clone_dir, port, name)
    unit_path = _unit_dir() / unit
    if not unit_path.exists():
        return {"ok": True, "name": unit, "removed": False, "note": "no unit installed"}
    _run(["systemctl", "--user", "disable", unit])
    # Deliberately NO `stop`: removing boot-autostart must not kill the live server (which may be the
    # very process serving this request). The running instance keeps going until stopped explicitly.
    try:
        unit_path.unlink()
        removed = True
    except OSError:
        removed = False
    _run(["systemctl", "--user", "daemon-reload"])
    return {"ok": True, "name": unit, "removed": removed,
            "note": "boot-autostart removed (the running server keeps going until you stop it)"}


def status(name: str | None = None, clone_dir: Path = ROOT) -> dict:
    ok, reason = service_supported()
    if not ok:
        return {"ok": False, "supported": False, "reason": reason}
    port = _port_from_env(clone_dir)
    unit = unit_name(clone_dir, port, name)
    unit_path = _unit_dir() / unit
    _, out_en, _ = _run(["systemctl", "--user", "is-enabled", unit])
    _, out_ac, _ = _run(["systemctl", "--user", "is-active", unit])
    _, out_lg, _ = _run(["loginctl", "show-user", _user(), "--property=Linger"])
    return {"ok": True, "supported": True, "name": unit, "unit_path": str(unit_path),
            "installed": unit_path.exists(), "enabled": out_en == "enabled",
            "active": out_ac == "active", "linger": out_lg.strip() == "Linger=yes"}
