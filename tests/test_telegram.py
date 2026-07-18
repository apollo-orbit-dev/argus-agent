"""Unit tests for the pure decision helpers in ``backend.telegram_bot``.

No network, no real Telegram, no real model — we test only the module-level
helper logic the handlers delegate to.
"""
from config import Config
from backend.telegram_bot import (
    TELEGRAM_MAX_CHARS,
    is_allowed,
    mode_command,
    progress_line_for,
    skills_text,
    truncate_for_telegram,
)


def _cfg(**over):
    base = dict(model_base_url="http://x/v1", model_name="main",
                telegram_bot_token="")
    base.update(over)
    return Config(**base)


# ---- is_allowed ----------------------------------------------------------
def test_is_allowed_true_for_listed_id():
    c = _cfg(allowed_chat_ids="100,200,300")
    assert is_allowed(200, c) is True
    assert is_allowed(100, c) is True


def test_is_allowed_false_for_unlisted_id():
    c = _cfg(allowed_chat_ids="100,200")
    assert is_allowed(999, c) is False


def test_is_allowed_false_for_empty_allowlist():
    c = _cfg(allowed_chat_ids="")
    assert is_allowed(100, c) is False


# ---- progress_line_for ---------------------------------------------------
def test_progress_line_known_tools():
    assert progress_line_for("web_search") == "🔍 searching the web…"
    assert progress_line_for("fetch_page") == "📄 reading a page…"
    assert progress_line_for("calculator") == "🧮 calculating…"
    assert progress_line_for("get_current_time") == "🕐 checking the time…"


def test_progress_line_unknown_tool_default():
    assert progress_line_for("frobnicate") == "⚙️ using frobnicate…"


# ---- mode_command --------------------------------------------------------
class _FakeEngine:
    """Minimal engine stand-in wrapping a real Config for validation."""

    def __init__(self, config: Config):
        self._config = config

    def get_config(self) -> dict:
        return self._config.model_dump()

    def patch_config(self, patch: dict) -> dict:
        # Mirrors the real Engine: Config.patch validates and raises on bad enum.
        self._config = self._config.patch(patch)
        return self._config.model_dump()


def test_mode_command_no_arg_reports_current():
    eng = _FakeEngine(_cfg(tool_calling_mode="native"))
    reply = mode_command(eng, None)
    assert "native" in reply
    # unchanged
    assert eng.get_config()["tool_calling_mode"] == "native"


def test_mode_command_valid_flip():
    eng = _FakeEngine(_cfg(tool_calling_mode="native"))
    reply = mode_command(eng, "manual")
    assert "manual" in reply
    assert eng.get_config()["tool_calling_mode"] == "manual"


def test_mode_command_invalid_value_no_change():
    eng = _FakeEngine(_cfg(tool_calling_mode="native"))
    reply = mode_command(eng, "bogus")
    assert "Invalid" in reply
    # engine left unchanged after the failed patch
    assert eng.get_config()["tool_calling_mode"] == "native"


def test_mode_command_strips_whitespace():
    eng = _FakeEngine(_cfg(tool_calling_mode="native"))
    reply = mode_command(eng, "  manual  ")
    assert "manual" in reply
    assert eng.get_config()["tool_calling_mode"] == "manual"


# ---- truncate_for_telegram ----------------------------------------------
def test_truncate_leaves_short_text():
    s = "hello world"
    assert truncate_for_telegram(s) == s


def test_truncate_at_limit_unchanged():
    s = "x" * TELEGRAM_MAX_CHARS
    assert truncate_for_telegram(s) == s


def test_truncate_long_text():
    s = "x" * (TELEGRAM_MAX_CHARS + 500)
    out = truncate_for_telegram(s)
    assert len(out) <= TELEGRAM_MAX_CHARS
    assert out.endswith("(truncated)")


def test_truncate_handles_none():
    assert truncate_for_telegram(None) == ""


# ---- skills_text ---------------------------------------------------------
def test_skills_text_empty():
    assert skills_text([]) == "No skills loaded yet."


def test_skills_text_lists_names_and_desc():
    out = skills_text([
        {"name": "research", "description": "web research", "tools": []},
        {"name": "math", "description": "", "tools": []},
    ])
    assert "research" in out
    assert "web research" in out
    assert "math" in out
