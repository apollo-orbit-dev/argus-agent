"""A throwaway Engine must not write the developer's real .env.

This bit twice: `persist_to_env` writes to a path that was hard-coded to the repo root, ignoring
`data_dir`. So a test (or a dashboard driven for QA) that built `Engine(cfg, data_dir=tmp)` and
changed a setting rewrote the developer's actual `.env` — breaking config-default tests and, on a
live box, silently changing the running server on next restart. The `__init__` comment promised
"pass a tmp dir and a throwaway Engine writes NOTHING into the repo"; `_env_path` was the one thing
that broke that promise.

Every persistence path — the engine's own save, and the backend's two .env-writing endpoints — must
honour the engine's env_path, which follows data_dir.
"""
import tempfile
from pathlib import Path

from config import persist_to_env
from engine.engine import Engine
from tests.test_config import _mk

REPO_ENV = Path(__file__).resolve().parents[1] / ".env"


def test_env_path_follows_data_dir():
    tmp = tempfile.mkdtemp()
    eng = Engine(_mk(), data_dir=tmp)
    assert eng.env_path == Path(tmp) / ".env"


def test_env_path_is_the_repo_env_in_production():
    """data_dir=None is production. env_path must stay the repo .env, byte-for-byte the old
    behaviour — this fix must not move where a real deploy persists config."""
    eng = Engine(_mk(), data_dir=None)
    assert eng.env_path == REPO_ENV


def test_env_path_is_explicitly_injectable():
    tmp = tempfile.mkdtemp()
    custom = Path(tmp) / "custom.env"
    eng = Engine(_mk(), data_dir=tmp, env_path=str(custom))
    assert eng.env_path == custom


def test_save_config_to_env_writes_only_the_throwaway_env():
    """The headline regression: saving config on a throwaway engine must not touch the repo .env."""
    before = REPO_ENV.read_bytes() if REPO_ENV.exists() else None
    tmp = tempfile.mkdtemp()
    eng = Engine(_mk(), data_dir=tmp)
    eng.patch_config({"max_steps": 9})
    eng.save_config_to_env()

    assert (Path(tmp) / ".env").exists(), "the throwaway env should have been written"
    assert "MAX_STEPS=9" in (Path(tmp) / ".env").read_text()
    after = REPO_ENV.read_bytes() if REPO_ENV.exists() else None
    assert after == before, "the repo .env must be untouched by a throwaway engine"


def test_persist_to_env_still_takes_an_explicit_path():
    """persist_to_env itself was never the bug — it already took a path. Pin that it stays a pure
    function of its argument, so the fix lives entirely in who supplies the path."""
    tmp = tempfile.mkdtemp()
    target = Path(tmp) / "x.env"
    persist_to_env(_mk(max_steps=7), str(target))
    assert "MAX_STEPS=7" in target.read_text()
