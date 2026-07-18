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
