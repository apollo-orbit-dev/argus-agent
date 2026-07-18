"""The ONE place that runs `pip install`. Isolated so the approval flow can be
tested without touching the network or the venv (patch `install`).

Security note: this executes arbitrary package code at install time (setup hooks)
and makes the module importable in-process. It runs ONLY after a human has approved
the specific module by name via an admin-gated path — never automatically.
"""
from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import logging
import re
import sys

log = logging.getLogger("argus.deps")

# A conservative package-name guard: PyPI names are ASCII letters/digits/._- and
# must start alphanumeric. Blocks shell-injection / option-injection via the name.
_VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")


def valid_package_name(name: str) -> bool:
    return bool(_VALID_NAME.match(name or ""))


async def install(module: str, timeout: float = 300.0) -> tuple[bool, str, str]:
    """pip-install `module` into the running venv. Returns (ok, version, log_tail).

    `module` is the top-level import name the tool wants; for most libraries that's
    also the PyPI distribution name. The resolved installed version is returned so
    the approver record can pin it.
    """
    if not valid_package_name(module):
        return False, "", f"refused: '{module}' is not a valid package name"
    cmd = [sys.executable, "-m", "pip", "install", "--no-input", module]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return False, "", f"pip install timed out after {timeout:.0f}s"
    except Exception as e:
        return False, "", f"could not launch pip: {type(e).__name__}: {e}"

    tail = (out or b"").decode("utf-8", "replace")[-1500:]
    if proc.returncode != 0:
        return False, "", tail or f"pip exited {proc.returncode}"

    # Make the freshly-installed package importable in this live process.
    importlib.invalidate_caches()
    version = ""
    try:
        version = importlib.metadata.version(module)
    except Exception:
        try:                                   # dist name may differ from import name
            importlib.import_module(module.split(".")[0])
        except Exception:
            pass
    return True, version, tail
