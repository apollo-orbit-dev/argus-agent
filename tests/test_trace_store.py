import time
from engine.events import StepEvent
from engine.trace.store import TraceStore


def _ev(session, run, step, kind, data=None, ts=None):
    return StepEvent(run_id=run, session_id=session, step=step, kind=kind,
                     data=data or {}, ts=ts if ts is not None else time.time())


def test_record_skips_model_request_and_caps(tmp_path):
    s = TraceStore(str(tmp_path / "events.db"), event_max_bytes=200)
    s.record(_ev("sess", "run1", 0, "info", {"text": "hi"}))
    s.record(_ev("sess", "run1", 1, "model_request", {"messages": ["big"] * 999}))   # skipped
    s.record(_ev("sess", "run1", 2, "tool_result", {"result": "x" * 5000}))          # capped
    evs = s.recent("sess")
    kinds = [e["kind"] for e in evs]
    assert "model_request" not in kinds and "info" in kinds and "tool_result" in kinds
    big = [e for e in evs if e["kind"] == "tool_result"][0]
    assert len(str(big["data"])) <= 260 and "…[truncated]" in str(big["data"])


def test_recent_returns_last_runs_oldest_first(tmp_path):
    s = TraceStore(str(tmp_path / "events.db"), replay_runs=2)
    for i, run in enumerate(["a", "b", "c"]):
        s.record(_ev("sess", run, 0, "final", {"answer": run}, ts=1000 + i))
    runs = [e["run_id"] for e in s.recent("sess")]
    assert runs == ["b", "c"]                 # last 2 runs, oldest-first


def test_recent_decodes_data(tmp_path):
    s = TraceStore(str(tmp_path / "events.db"))
    s.record(_ev("sess", "r", 0, "tool_call", {"tool": "calculator", "args": {"expression": "2+2"}}))
    ev = s.recent("sess")[0]
    assert ev["data"]["tool"] == "calculator" and ev["data"]["args"]["expression"] == "2+2"


def test_prune_age(tmp_path):
    s = TraceStore(str(tmp_path / "events.db"))
    now = time.time()
    s.record(_ev("sess", "old", 0, "final", ts=now - 40 * 86400))
    s.record(_ev("sess", "new", 0, "final", ts=now))
    s.prune("age", days=30, keep_runs=100)
    assert [e["run_id"] for e in s.recent("sess")] == ["new"]


def test_prune_runs_per_session(tmp_path):
    s = TraceStore(str(tmp_path / "events.db"), replay_runs=100)
    for i in range(5):
        s.record(_ev("sess", f"r{i}", 0, "final", ts=1000 + i))
    s.prune("runs", days=30, keep_runs=2)
    assert sorted(e["run_id"] for e in s.recent("sess")) == ["r3", "r4"]


def test_prune_off_is_noop(tmp_path):
    s = TraceStore(str(tmp_path / "events.db"))
    s.record(_ev("sess", "r", 0, "final", ts=time.time() - 999 * 86400))
    s.prune("off", days=1, keep_runs=1)
    assert len(s.recent("sess")) == 1


def test_record_skips_ephemeral_session(tmp_path):
    s = TraceStore(str(tmp_path / "events.db"))
    s.record(_ev("__routine__", "routine", 0, "final", {"answer": "x"}))
    assert s.recent("__routine__") == []


def test_delete_session_drops_only_that_session(tmp_path):
    s = TraceStore(str(tmp_path / "events.db"))
    s.record(_ev("a", "run1", 0, "final", {"answer": "a"}))
    s.record(_ev("b", "run2", 0, "final", {"answer": "b"}))
    s.delete_session("a")
    assert s.recent("a") == []
    assert [e["run_id"] for e in s.recent("b")] == ["run2"]


def test_prune_runs_is_per_session_even_if_run_ids_collide(tmp_path):
    s = TraceStore(str(tmp_path / "events.db"), replay_runs=100)
    # same run_id "dup" used by two different sessions
    s.record(_ev("s1", "dup", 0, "final", ts=1000))
    s.record(_ev("s1", "newer", 0, "final", ts=2000))
    s.record(_ev("s2", "dup", 0, "final", ts=1500))
    s.prune("runs", days=30, keep_runs=1)          # s1 keeps "newer" only
    assert [e["run_id"] for e in s.recent("s1")] == ["newer"]
    assert [e["run_id"] for e in s.recent("s2")] == ["dup"]   # s2 untouched


def test_not_wal_mode_so_there_are_no_sidecars_to_lose(tmp_path):
    # Deliberate: WAL buys nothing here (single connection, lock-serialized) but its -wal/-shm
    # sidecars are silently discarded by a deploy that rsyncs --delete, taking recent rows with them.
    p = tmp_path / "events.db"
    s = TraceStore(str(p))
    mode = s._rw.execute("PRAGMA journal_mode;").fetchone()[0]
    assert mode.lower() != "wal", f"expected a rollback journal, got {mode}"
    s.record(_ev("sess", "r", 0, "final", {"answer": "x"}))
    assert not (tmp_path / "events.db-wal").exists()
    assert s.recent("sess")[0]["data"]["answer"] == "x"     # still records fine
