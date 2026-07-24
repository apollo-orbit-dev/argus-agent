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
