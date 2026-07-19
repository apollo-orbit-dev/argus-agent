"""Pytest configuration — make the test suite hermetic (no .env required).

`Config` has two required fields, `model_base_url` and `model_name`, with no defaults. Locally a
`.env` file supplies them, but CI (and any fresh clone) has no `.env`, so a bare `Config()` raises a
pydantic `ValidationError` and every test that builds a `Config`/`Engine` fails there.

Set harmless placeholder values in the environment *before* any `Config` is constructed so the suite
doesn't depend on a `.env` existing. Precedence still holds — explicit `Config(model_base_url=...)`
kwargs and any real exported env var win over these (`setdefault` + pydantic-settings' order:
init kwargs > env vars > .env file > defaults) — so tests that pass values explicitly are unaffected.
No test hits a real model; these values just satisfy validation.
"""
import os

os.environ.setdefault("MODEL_BASE_URL", "http://test.invalid/v1")
os.environ.setdefault("MODEL_NAME", "test-model")
