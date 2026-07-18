from engine.state import SessionStore


def test_get_or_create_idempotent():
    s = SessionStore()
    a = s.get_or_create("x")
    b = s.get_or_create("x")
    assert a is b
    assert a.session_id == "x"


def test_append_and_conversation_copy():
    s = SessionStore()
    s.append_message("x", {"role": "user", "content": "hi"})
    conv = s.conversation("x")
    assert conv == [{"role": "user", "content": "hi"}]
    conv.append({"role": "user", "content": "corrupt"})
    # mutating the returned copy must not affect the store
    assert s.conversation("x") == [{"role": "user", "content": "hi"}]


def test_reset_clears_conversation_keeps_session():
    s = SessionStore()
    s.append_message("x", {"role": "user", "content": "hi"})
    s.reset("x")
    assert s.conversation("x") == []
    assert s.get_or_create("x").session_id == "x"


def test_conversation_missing_session_empty():
    s = SessionStore()
    assert s.conversation("nope") == []
