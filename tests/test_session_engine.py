from config import Config
from engine.engine import Engine


def _engine(tmp_path):
    return Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""),
                  data_dir=str(tmp_path))


def test_engine_persists_sessions_under_data_dir(tmp_path):
    e = _engine(tmp_path)
    e.store.append_message("dashboard", {"role": "user", "content": "hi"})
    assert (tmp_path / "sessions.db").exists()
    # a second engine on the same data_dir restores it
    e2 = _engine(tmp_path)
    assert e2.store.conversation("dashboard") == [{"role": "user", "content": "hi"}]


def test_compaction_uses_set_working_set_keeping_log(tmp_path):
    e = _engine(tmp_path)
    for i in range(4):
        e.store.append_message("dashboard", {"role": "user", "content": f"m{i}"})
    # directly exercise the compaction seam the engine now uses
    e.store.set_working_set("dashboard", [{"role": "user", "content": "[summary]"}])
    assert e.store.session_messages("dashboard")["total"] == 4      # log intact
    assert len(e.store.conversation("dashboard")) == 1


def test_engine_session_crud_wrappers(tmp_path):
    e = _engine(tmp_path)
    sid = e.create_session("work")
    assert sid in {r["id"] for r in e.list_sessions()}
    e.rename_session(sid, "work2")
    e.delete_session(sid)
    assert sid not in {r["id"] for r in e.list_sessions()}
