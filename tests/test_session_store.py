from engine.state import SessionStore, _is_ephemeral


def test_roundtrip_working_set_and_log(tmp_path):
    p = str(tmp_path / "sessions.db")
    s = SessionStore(p)
    s.append_message("dashboard", {"role": "user", "content": "hi"})
    s.append_message("dashboard", {"role": "assistant", "content": "hello"})
    s.record_run("dashboard", "run_1")
    # a fresh store on the same file restores the working set + runs + full log
    s2 = SessionStore(p)
    assert s2.conversation("dashboard") == [
        {"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    assert s2.get_or_create("dashboard").runs == ["run_1"]
    log = s2.session_messages("dashboard")
    assert log["total"] == 2 and log["messages"][0]["content"] == "hi"


def test_compaction_keeps_full_log(tmp_path):
    p = str(tmp_path / "sessions.db")
    s = SessionStore(p)
    for i in range(6):
        s.append_message("dashboard", {"role": "user", "content": f"m{i}"})
    # compaction replaces the working set with a summary + recent, via set_working_set
    s.set_working_set("dashboard", [{"role": "user", "content": "[summary]"},
                                    {"role": "user", "content": "m5"}])
    assert s.conversation("dashboard") == [
        {"role": "user", "content": "[summary]"}, {"role": "user", "content": "m5"}]
    # the raw log still has all six original messages, untouched
    assert s.session_messages("dashboard")["total"] == 6
    s2 = SessionStore(p)   # and it survives restart
    assert len(s2.conversation("dashboard")) == 2
    assert s2.session_messages("dashboard")["total"] == 6


def test_ephemeral_sessions_not_persisted(tmp_path):
    p = str(tmp_path / "sessions.db")
    s = SessionStore(p)
    assert _is_ephemeral("__routine__:x") and not _is_ephemeral("dashboard")
    s.append_message("__routine__:x", {"role": "user", "content": "scratch"})
    s.reset("__routine__:x")
    assert s.session_messages("__routine__:x")["total"] == 0
    s2 = SessionStore(p)
    assert s2.conversation("__routine__:x") == []          # nothing persisted


def test_reset_clears_working_set_but_keeps_log(tmp_path):
    p = str(tmp_path / "sessions.db")
    s = SessionStore(p)
    s.append_message("dashboard", {"role": "user", "content": "keep me"})
    s.reset("dashboard")
    assert s.conversation("dashboard") == []               # model context cleared
    assert s.session_messages("dashboard")["total"] == 1   # raw transcript preserved


def test_in_memory_mode_still_works(tmp_path):
    s = SessionStore()                                     # no path -> pure in-memory
    s.append_message("x", {"role": "user", "content": "a"})
    assert s.conversation("x") == [{"role": "user", "content": "a"}]
    assert s.session_messages("x")["total"] == 0           # no db, no log


def _session_ids(store):
    return {r["id"] for r in store._db.execute("SELECT id FROM sessions").fetchall()}


def test_conversation_read_does_not_create_row(tmp_path):
    p = str(tmp_path / "sessions.db")
    s = SessionStore(p)
    assert s.conversation("never-touched") == []
    # a pure read must not have persisted a phantom session
    assert "never-touched" not in _session_ids(s)
    s2 = SessionStore(p)
    assert "never-touched" not in _session_ids(s2)


def test_session_crud(tmp_path):
    p = str(tmp_path / "sessions.db")
    s = SessionStore(p)
    sid = s.create_session("morning-ops")
    assert sid.startswith("sess-")
    s.append_message(sid, {"role": "user", "content": "hi"})
    listed = {r["id"]: r for r in s.list_sessions()}
    assert listed[sid]["name"] == "morning-ops" and listed[sid]["message_count"] == 1
    s.rename_session(sid, "renamed")
    assert {r["id"]: r["name"] for r in s.list_sessions()}[sid] == "renamed"
    s.delete_session(sid)
    assert sid not in {r["id"] for r in s.list_sessions()}
    assert s.session_messages(sid)["total"] == 0           # log rows gone too


def test_list_excludes_ephemeral(tmp_path):
    s = SessionStore(str(tmp_path / "sessions.db"))
    s.append_message("dashboard", {"role": "user", "content": "a"})
    s.append_message("__routine__:x", {"role": "user", "content": "scratch"})
    ids = {r["id"] for r in s.list_sessions()}
    assert "dashboard" in ids and "__routine__:x" not in ids
