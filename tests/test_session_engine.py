import asyncio

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


def test_engine_rename_session_emits_session_changed(tmp_path):
    # Engine.rename_session is the choke point for out-of-band renames (PATCH /sessions/{id},
    # Telegram, any internal caller) — it must publish on the "__control__" pseudo-session so a
    # dashboard tab on a DIFFERENT session's SSE stream still learns about the rename.
    e = _engine(tmp_path)
    sid = e.create_session("work")
    seen = []
    e.events.add_sink(seen.append)

    e.rename_session(sid, "work2")

    assert len(seen) == 1
    ev = seen[0]
    assert ev.session_id == "__control__"
    assert ev.kind == "session_changed"
    assert ev.data == {"session_id": sid, "action": "renamed", "name": "work2"}


def test_emit_session_changed_no_running_loop_falls_back_to_asyncio_run(tmp_path):
    # Called synchronously (no event loop running) — must fall back to asyncio.run(...) rather than
    # raising RuntimeError from asyncio.get_running_loop(), mirroring _emit_routine_result's shape.
    e = _engine(tmp_path)
    sid = e.create_session("work")
    seen = []
    e.events.add_sink(seen.append)

    e._emit_session_changed(sid, "renamed", "New Title")     # must not raise

    assert len(seen) == 1 and seen[0].session_id == "__control__"


def test_emit_session_changed_never_raises_if_publish_throws(tmp_path):
    # This is a plain sync test (no running loop), so it exercises ONLY the NO-LOOP branch of
    # _emit_session_changed: asyncio.get_running_loop() raises RuntimeError, the fallback
    # asyncio.run(publish(...)) re-raises publish's exception synchronously, and the outer
    # try/except catches it. It does NOT cover the live production branch — create_task, used
    # whenever a loop IS running — where a throwing publish escapes into the background TASK
    # instead of into this function's try/except, so this try/except never sees it and
    # add_done_callback(self._bg_tasks.discard) is what has to retrieve the exception (else
    # "Task exception was never retrieved" noise). The caller still never sees a raise either way
    # (the requirement this fix guards), but that stronger claim about the task itself is only
    # checked by the async sibling below. Even if something inside the publish pipeline blows up,
    # _emit_session_changed must swallow it (log.debug only) rather than propagate into its caller
    # (auto_title_session / rename_session).
    e = _engine(tmp_path)
    sid = e.create_session("work")

    def _boom(ev):
        raise RuntimeError("publish exploded")
    e.events.publish = _boom

    e._emit_session_changed(sid, "renamed", "New Title")     # must not raise


async def test_emit_session_changed_never_raises_if_publish_throws_running_loop(tmp_path):
    # The create_task branch (a running loop, the production shape): a throwing publish escapes
    # into the background task, not into _emit_session_changed's own try/except. The caller still
    # must not see a raise, and the task must actually be retrievable via _bg_tasks's
    # add_done_callback so the exception doesn't linger as "Task exception was never retrieved".
    e = _engine(tmp_path)
    sid = e.create_session("work")

    async def _boom(ev):
        raise RuntimeError("publish exploded")
    e.events.publish = _boom

    e._emit_session_changed(sid, "renamed", "New Title")     # must not raise
    assert len(e._bg_tasks) == 1
    task = next(iter(e._bg_tasks))
    await asyncio.sleep(0)          # let the task actually run and raise
    assert task.done()
    assert isinstance(task.exception(), RuntimeError)     # retrieved -> no "never retrieved" noise
    await asyncio.sleep(0)          # let the done-callback fire
    assert task not in e._bg_tasks


def test_raw_store_rename_session_does_not_emit(tmp_path):
    # The raw SessionStore.rename_session call (no Engine involved) must NOT emit anything — the
    # emit lives at the Engine choke point, not inside the store. Distinguishes "Engine wraps the
    # store call with an emit" from "the store itself grew event-bus awareness".
    e = _engine(tmp_path)
    sid = e.create_session("work")
    seen = []
    e.events.add_sink(seen.append)

    e.store.rename_session(sid, "work2")

    assert seen == []
