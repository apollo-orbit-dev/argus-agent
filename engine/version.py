"""Single source of truth for the Argus version: read it from pyproject.toml so there's one place
to bump it (the packaging manifest), and expose it to the API/dashboard."""
from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def get_version() -> str:
    try:
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        return (data.get("project", {}).get("version")
                or data.get("tool", {}).get("poetry", {}).get("version")
                or "0.0.0")
    except Exception:
        return "0.0.0"
