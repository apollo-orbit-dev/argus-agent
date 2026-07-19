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
