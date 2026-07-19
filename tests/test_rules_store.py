from engine.rules.store import RulesStore


def test_add_list_and_record_shape(tmp_path):
    s = RulesStore(str(tmp_path / "rules.json"))
    r = s.add("Always confirm before deleting files", now=100.0)
    assert set(r) == {"id", "text", "source", "enabled", "created_at"}
    assert r["text"] == "Always confirm before deleting files"
    assert r["source"] == "user" and r["enabled"] is True and r["created_at"] == 100.0
    assert len(s.list()) == 1


def test_add_strips_and_rejects_empty(tmp_path):
    s = RulesStore(str(tmp_path / "rules.json"))
    assert s.add("   ") is None
    r = s.add("  Never use emoji  ")
    assert r["text"] == "Never use emoji"
    assert len(s.list()) == 1


def test_dedup_reenables_existing(tmp_path):
    s = RulesStore(str(tmp_path / "rules.json"))
    a = s.add("Never use emoji", now=1.0)
    s.set_enabled(a["id"], False)
    b = s.add("never USE emoji", now=2.0)          # case-insensitive dup
    assert b["id"] == a["id"] and b["enabled"] is True
    assert len(s.list()) == 1


def test_remove_and_set_enabled(tmp_path):
    s = RulesStore(str(tmp_path / "rules.json"))
    r = s.add("Ask before installing dependencies")
    assert s.set_enabled(r["id"], False) is True
    assert s.set_enabled("nope", False) is False
    assert s.remove("nope") is False
    assert s.remove(r["id"]) is True
    assert s.list() == []


def test_enabled_rules_ordering_and_filtering(tmp_path):
    s = RulesStore(str(tmp_path / "rules.json"))
    r1 = s.add("rule one", now=1.0)
    r2 = s.add("rule two", now=2.0)
    r3 = s.add("rule three", now=3.0)
    s.set_enabled(r2["id"], False)
    en = s.enabled_rules()
    assert [r["text"] for r in en] == ["rule one", "rule three"]   # oldest-first, disabled excluded
    assert [r["text"] for r in s.list()] == ["rule three", "rule two", "rule one"]  # newest-first


def test_persistence_and_corrupt_file(tmp_path):
    p = str(tmp_path / "rules.json")
    s = RulesStore(p)
    s.add("Always cite sources", now=5.0)
    assert [r["text"] for r in RulesStore(p).list()] == ["Always cite sources"]
    open(p, "w").write("{ not json")
    assert RulesStore(p).list() == []             # corrupt file tolerated -> empty
