import pytest
from config import Config


def _mk(**over):
    base = dict(model_base_url="http://x/v1", model_name="main", telegram_bot_token="")
    base.update(over)
    return Config(**base)


def test_defaults():
    c = _mk()
    assert c.max_steps == 6
    assert c.model_max_tokens == 2048
    assert c.port == 8700
    assert c.tool_calling_mode == "native"
    assert c.skill_selection_mode == "hybrid"


def test_patch_is_immutable_copy():
    c = _mk()
    c2 = c.patch({"tool_calling_mode": "manual"})
    assert c2.tool_calling_mode == "manual"
    assert c.tool_calling_mode == "native"  # original unchanged


def test_patch_rejects_bad_mode():
    c = _mk()
    with pytest.raises(Exception):
        c.patch({"tool_calling_mode": "bogus"})


def test_allowed_chat_ids_parses_csv():
    c = _mk(allowed_chat_ids="123, 456 ,789")
    assert c.allowed_chat_ids == [123, 456, 789]


def test_allowed_chat_ids_empty():
    c = _mk(allowed_chat_ids="")
    assert c.allowed_chat_ids == []


def test_rules_flags_default_on():
    c = _mk()
    assert c.enable_rules is True
    assert c.enable_rules_autodetect is True


def test_rules_flags_in_env_fields_and_env_pairs():
    c = _mk()
    assert "enable_rules" in c._ENV_FIELDS
    assert "enable_rules_autodetect" in c._ENV_FIELDS
    pairs = dict(c.env_pairs())
    assert "ENABLE_RULES" in pairs
    assert "ENABLE_RULES_AUTODETECT" in pairs


def test_approval_flags():
    c = _mk()
    assert c.enable_interactive_approvals is True
    assert c.approval_window_seconds == 60
    assert "enable_interactive_approvals" in c._ENV_FIELDS
    assert "approval_window_seconds" in c._ENV_FIELDS
    pairs = dict(c.env_pairs())
    assert "ENABLE_INTERACTIVE_APPROVALS" in pairs and "APPROVAL_WINDOW_SECONDS" in pairs


def test_trace_persistence_config_defaults_and_env_roundtrip():
    c = _mk()
    assert c.enable_trace_persistence is True
    assert c.trace_retention_mode == "age+runs"
    assert c.trace_retention_days == 30 and c.trace_keep_runs_per_session == 200
    assert c.trace_event_max_bytes == 16384 and c.trace_replay_runs == 50
    # the six fields are env-round-tripped (matches the existing style in this file: c._ENV_FIELDS)
    for f in ("enable_trace_persistence", "trace_retention_mode", "trace_retention_days",
              "trace_keep_runs_per_session", "trace_event_max_bytes", "trace_replay_runs"):
        assert f in c._ENV_FIELDS
    pairs = dict(c.env_pairs())
    assert "TRACE_RETENTION_MODE" in pairs


def test_trace_retention_mode_rejects_invalid():
    c = _mk()
    with pytest.raises(Exception):
        c.patch({"trace_retention_mode": "bogus"})


def test_trace_retention_days_rejects_zero():
    c = _mk()
    with pytest.raises(Exception):
        c.patch({"trace_retention_days": 0})


def test_trace_keep_runs_per_session_rejects_zero():
    c = _mk()
    with pytest.raises(Exception):
        c.patch({"trace_keep_runs_per_session": 0})
