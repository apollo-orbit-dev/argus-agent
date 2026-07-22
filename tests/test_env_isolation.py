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
import os
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


def test_load_dotenv_skips_config_keys_but_loads_extras(tmp_path, monkeypatch):
    """The restart bug: load_dotenv_into_environ copied CONFIG keys (ENABLE_SANDBOX, ...) into
    os.environ, where a stale value survived an os.execv restart and shadowed the updated .env
    (env vars outrank the .env file). Config keys must NOT be loaded; arbitrary secret vars must be.
    """
    from config import load_dotenv_into_environ

    env = tmp_path / ".env"
    env.write_text("ENABLE_SANDBOX=true\nMODEL_NAME=main\nARGUS_TEST_SECRET_XYZ=s3cret\n")
    # ensure a clean slate for both
    monkeypatch.delenv("ENABLE_SANDBOX", raising=False)
    monkeypatch.delenv("ARGUS_TEST_SECRET_XYZ", raising=False)

    loaded = load_dotenv_into_environ(str(env))

    # the function's contract is "returns the keys it set" — config keys are NOT among them
    # (MODEL_NAME may already be a real env var here, so assert on `loaded`, not os.environ)
    assert "ENABLE_SANDBOX" not in loaded
    assert "MODEL_NAME" not in loaded
    assert "ENABLE_SANDBOX" not in os.environ   # this one we cleared, so it's a clean check
    # ...but a genuine extra var IS set, so tool secrets still work
    assert os.environ.get("ARGUS_TEST_SECRET_XYZ") == "s3cret"
    assert "ARGUS_TEST_SECRET_XYZ" in loaded
    os.environ.pop("ARGUS_TEST_SECRET_XYZ", None)   # don't leak into other tests


def test_a_toggled_config_key_survives_the_reload_cycle(tmp_path, monkeypatch):
    """End to end: a config key set to false in the OLD boot's os.environ must not win over a .env
    that now says true — because load_dotenv_into_environ no longer copies config keys, pydantic
    reads the fresh .env value. Reproduces the restart-doesn't-stick scenario."""
    from config import Config, load_dotenv_into_environ

    env = tmp_path / ".env"
    env.write_text("MODEL_BASE_URL=http://x/v1\nMODEL_NAME=main\nENABLE_SANDBOX=true\n")
    monkeypatch.delenv("ENABLE_SANDBOX", raising=False)   # simulate a clean-ish restart

    load_dotenv_into_environ(str(env))                    # must NOT set ENABLE_SANDBOX in environ
    assert "ENABLE_SANDBOX" not in os.environ, "the stale-shadow path must be gone"
