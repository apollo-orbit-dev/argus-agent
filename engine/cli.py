"""The `argus` command — manage the local server: start / stop / restart / status / logs.

Installed as a console entry point (`pip install -e .` → `argus`). Stdlib-only and cross-platform
(Windows/macOS/Linux); process control is pidfile + port based. On Windows, start uses
DETACHED_PROCESS and stop falls back to taskkill.
"""
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]      # project dir (holds main.py, .env)
PIDFILE = ROOT / ".argus.pid"


def _config():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from config import Config
    return Config.load()


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    h = "127.0.0.1" if host in ("0.0.0.0", "", None) else host
    with socket.socket() as s:
        s.settimeout(timeout)
        try:
            s.connect((h, port))
            return True
        except OSError:
            return False


def _read_pid():
    try:
        return int(PIDFILE.read_text().strip())
    except Exception:
        return None


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


def cmd_start(_):
    c = _config()
    if _port_open(c.host, c.port):
        print(f"Argus is already running → http://localhost:{c.port}")
        return 0
    logf = open(ROOT / c.log_file, "a", buffering=1)
    kwargs = {"start_new_session": True} if os.name == "posix" else {"creationflags": 0x00000008}
    proc = subprocess.Popen([sys.executable, str(ROOT / "main.py")], cwd=str(ROOT),
                            stdout=logf, stderr=subprocess.STDOUT, **kwargs)
    PIDFILE.write_text(str(proc.pid))
    for _ in range(60):                          # wait up to ~30s for the port
        if _port_open(c.host, c.port):
            print(f"Argus started (pid {proc.pid}) → http://localhost:{c.port}")
            return 0
        if proc.poll() is not None:
            print("Argus failed to start — check `argus logs`.")
            return 1
        time.sleep(0.5)
    print("Argus is starting (port not up yet). Check `argus logs`.")
    return 0


def cmd_stop(_):
    c = _config()
    pid = _read_pid()
    if not _pid_alive(pid) and not _port_open(c.host, c.port):
        print("Argus is not running.")
        PIDFILE.unlink(missing_ok=True)
        return 0
    if pid:
        try:
            if os.name == "posix":
                os.kill(pid, signal.SIGTERM)
            else:
                subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True)
        except Exception as e:
            print(f"stop: {e}")
    for _ in range(30):
        if not _port_open(c.host, c.port):
            break
        time.sleep(0.3)
    PIDFILE.unlink(missing_ok=True)
    print("Argus stopped.")
    return 0


def cmd_restart(args):
    cmd_stop(args)
    time.sleep(1)
    return cmd_start(args)


def cmd_status(_):
    c = _config()
    up = _port_open(c.host, c.port)
    pid = _read_pid()
    tag = f" (pid {pid})" if up and pid else ""
    print(f"Argus: {'running' if up else 'stopped'}{tag} · http://localhost:{c.port}")
    return 0 if up else 1


def cmd_logs(_):
    c = _config()
    path = ROOT / c.log_file
    from engine.logtail import tail_lines
    for ln in tail_lines(str(path), 40):
        print(ln)
    try:
        with open(path) as f:
            f.seek(0, os.SEEK_END)
            while True:
                line = f.readline()
                if line:
                    print(line, end="")
                else:
                    time.sleep(0.5)
    except FileNotFoundError:
        print(f"(no log file yet at {path} — start the server first)")
    except KeyboardInterrupt:
        pass
    return 0


def cmd_run(_):
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, str(ROOT / "main.py")])   # foreground; replaces process


def cmd_version(_):
    from engine.version import get_version
    print(f"argus {get_version()}")
    return 0


def cmd_service(args):
    from engine import service
    action = getattr(args, "action", None) or "status"
    if action == "install":
        r = service.install(name=args.name, dry_run=args.dry_run)
    elif action == "uninstall":
        r = service.uninstall(name=args.name)
    else:
        r = service.status(name=args.name)
    if not r.get("ok", True) and "reason" in r:
        print(f"service: {r['reason']}")
        return 1
    if r.get("dry_run"):
        print(f"# {r['unit_path']}\n{r['unit_text']}")
        return 0
    if action == "status":
        flags = [k for k in ("installed", "enabled", "active", "linger") if r.get(k)]
        print(f"{r['name']}: " + (", ".join(flags) if flags else "not installed") + f"  ({r['unit_path']})")
        return 0
    print(r.get("note", r.get("name", "done")))
    return 0 if r.get("ok", True) else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="argus", description="Argus — small-model agent control")
    sub = p.add_subparsers(dest="cmd")
    for name, help_ in [
        ("start", "start the server in the background"),
        ("stop", "stop the running server"),
        ("exit", "alias for stop"),
        ("restart", "restart the server"),
        ("status", "show whether it's running"),
        ("logs", "tail the server log (Ctrl-C to quit)"),
        ("run", "run in the foreground (Ctrl-C to quit)"),
        ("version", "print the version"),
        ("help", "show this help"),
    ]:
        sub.add_parser(name, help=help_)
    svc = sub.add_parser("service", help="install/remove a systemd user service (Linux)")
    svc.add_argument("action", nargs="?", choices=["install", "uninstall", "status"], default="status")
    svc.add_argument("--name", default=None, help="override the unit name (default: auto-derived)")
    svc.add_argument("--dry-run", action="store_true", help="print the unit file without installing")
    args = p.parse_args(argv)
    handlers = {"start": cmd_start, "stop": cmd_stop, "exit": cmd_stop, "restart": cmd_restart,
                "status": cmd_status, "logs": cmd_logs, "run": cmd_run, "version": cmd_version,
                "service": cmd_service}
    if not args.cmd or args.cmd == "help":
        p.print_help()
        return 0
    return handlers[args.cmd](args) or 0


if __name__ == "__main__":
    sys.exit(main())
