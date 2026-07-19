import pytest

from engine.engine import Engine
from config import Config


def _engine(tmp_path, **overrides):
    cfg = Config(**overrides)
    # data_dir keeps rules.json (and other state) inside the tmp dir
    return Engine(cfg, data_dir=str(tmp_path))


def test_wrappers_roundtrip(tmp_path):
    e = _engine(tmp_path)
    r = e.rules_add("Never use emoji")
    assert any(x["id"] == r["id"] for x in e.rules_list())
    assert e.rules_set_enabled(r["id"], False) is True
    assert e.rules_remove(r["id"]) is True
    assert e.rules_list() == []


def test_injection_includes_enabled_rules(tmp_path):
    e = _engine(tmp_path)
    e.rules_add("Always confirm before deleting files")
    prompt = e._compose_rules_block()          # helper the injection uses (see Step 3)
    assert "Standing instructions from your owner" in prompt
    assert "Always confirm before deleting files" in prompt


def test_injection_empty_when_none_or_disabled(tmp_path):
    e = _engine(tmp_path)
    assert e._compose_rules_block() == ""
    r = e.rules_add("Never use emoji")
    e.rules_set_enabled(r["id"], False)
    assert e._compose_rules_block() == ""


def test_injection_empty_when_flag_off(tmp_path):
    e = _engine(tmp_path, enable_rules=False)
    e.rules.add("Never use emoji")             # write directly; flag gates composition
    assert e._compose_rules_block() == ""
