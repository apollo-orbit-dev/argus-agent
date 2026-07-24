from engine.events import StepEvent
from engine.reliability.collector import ReliabilityCollector


class _FakeStore:
    def __init__(self): self.rows = []
    def record(self, kind, entity, ok, ms, detail, ts):
        self.rows.append({"kind": kind, "entity": entity, "ok": ok, "ms": ms, "detail": detail, "ts": ts})


def _ev(kind, data, step=1, ts=1.0):
    return StepEvent(run_id="r", session_id="s", step=step, kind=kind, data=data, ts=ts)


def test_tool_success_with_latency_pairing():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_call", {"tool": "weather"}, ts=10.0))
    c.record(_ev("tool_result", {"tool": "weather", "ok": True}, ts=10.4))
    assert st.rows == [{"kind": "tool", "entity": "weather", "ok": True, "ms": 400, "detail": "", "ts": 10.4}]


def test_tool_failure_records_detail():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"}, ts=2.0))
    assert st.rows[0]["kind"] == "tool" and st.rows[0]["ok"] is False and "HTTP 500" in st.rows[0]["detail"]


def test_no_data_ok_result_counts_as_success():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_result", {"tool": "ask_data", "ok": True, "result": "CANNOT"}, ts=1.0))
    assert st.rows[0]["ok"] is True                           # honest outcome, not a failure


def test_validation_failure_recorded_separately():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("validation", {"tool": "weather", "ok": False, "error": "missing 'location'"}))
    assert st.rows[0]["kind"] == "validation_fail" and st.rows[0]["entity"] == "weather"


def test_valid_validation_is_not_recorded():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("validation", {"tool": "weather", "ok": True}))
    assert st.rows == []                                       # only failures are loop-health signal


def test_reprompt_and_parse_fail_are_loop_health():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("reprompt", {"reason": "no tool call"}))
    c.record(_ev("error", {"kind": "parse_failure", "reason": "bad json"}))
    kinds = [r["kind"] for r in st.rows]
    assert kinds == ["reprompt", "parse_fail"]


def test_generic_error_is_ignored():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("error", {"error": "model call failed"}))     # not a parse_failure
    assert st.rows == []


def test_routine_result_recorded():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("routine_result", {"name": "morning", "ok": True, "ms": 5000}))
    assert st.rows[0] == {"kind": "routine", "entity": "morning", "ok": True, "ms": 5000, "detail": "", "ts": 1.0}


def test_ignored_kinds_do_nothing():
    st = _FakeStore(); c = ReliabilityCollector(st)
    for k in ("info", "model_request", "model_response", "final", "skill"):
        c.record(_ev(k, {}))
    assert st.rows == []


def test_pending_map_bounded():
    st = _FakeStore(); c = ReliabilityCollector(st, max_pending=2)
    for i in range(5):
        c.record(_ev("tool_call", {"tool": "t"}, step=i, ts=float(i)))
    assert len(c._pending) <= 2                                # never leaks


# ---- error-shaped tool_result (ok=True but the content is an error) counts as a FAILURE ----
from engine.reliability.collector import _looks_like_error


def test_error_shaped_ok_result_counts_as_failure():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_result", {"tool": "hf_model_info", "ok": True,
                                 "result": "Error fetching model info: Redirect response '307'"}))
    assert st.rows[0]["ok"] is False                              # ran, but returned an error string
    assert "Error fetching" in st.rows[0]["detail"]


def test_create_tool_looks_wrong_counts_as_failure():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_result", {"tool": "create_tool", "ok": True,
                                 "result": "create_tool: 'x' was created, but its test run looks WRONG: ..."}))
    assert st.rows[0]["ok"] is False


def test_no_data_and_cannot_still_count_as_success():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_result", {"tool": "ask_data", "ok": True, "result": "CANNOT"}))
    c.record(_ev("tool_result", {"tool": "weather", "ok": True, "result": "No data found for that date."}))
    assert st.rows[0]["ok"] is True and st.rows[1]["ok"] is True   # honest empties, not failures


def test_looks_like_error_heuristic():
    assert _looks_like_error("Error: boom")
    assert _looks_like_error("Error fetching X")
    assert _looks_like_error("unit_convert error: unknown from_unit")
    assert _looks_like_error("Traceback (most recent call last): ...")
    assert _looks_like_error("... its test run looks WRONG")
    assert not _looks_like_error("Sunny, 72F")
    assert not _looks_like_error("No results found")
    assert not _looks_like_error("CANNOT")
    assert not _looks_like_error("")


# ---- (a) tightened error heuristic: only a leading "error:" counts, not any "error" prefix ----


def test_error_budget_text_is_not_error_shaped():
    assert not _looks_like_error("Error budget: 5%")


def test_error_handling_guide_text_is_not_error_shaped():
    assert not _looks_like_error("Error handling guide: X")


def test_leading_error_colon_is_error_shaped():
    assert _looks_like_error("Error: connection refused")


def test_error_marker_substring_is_still_error_shaped():
    assert _looks_like_error("Error fetching model info: 307")


# ---- (b) consecutive-identical-error signal (metric-only; does not affect the loop) ----


def test_three_identical_failures_record_one_stuck_tool_row():
    st = _FakeStore(); c = ReliabilityCollector(st)
    for i in range(3):
        c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"},
                     step=i, ts=float(i)))
    stuck = [r for r in st.rows if r["kind"] == "stuck_tool"]
    assert len(stuck) == 1
    assert stuck[0] == {"kind": "stuck_tool", "entity": "web_search", "ok": False, "ms": None,
                        "detail": "HTTP 500 timeout", "ts": 2.0}


def test_fourth_identical_failure_does_not_record_again():
    st = _FakeStore(); c = ReliabilityCollector(st)
    for i in range(4):
        c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"},
                     step=i, ts=float(i)))
    stuck = [r for r in st.rows if r["kind"] == "stuck_tool"]
    assert len(stuck) == 1


def test_success_resets_the_streak():
    st = _FakeStore(); c = ReliabilityCollector(st)
    for i in range(2):
        c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"},
                     step=i, ts=float(i)))
    c.record(_ev("tool_result", {"tool": "web_search", "ok": True, "result": "3 results"}, step=2, ts=2.0))
    for i in range(2):
        c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"},
                     step=3 + i, ts=float(3 + i)))
    stuck = [r for r in st.rows if r["kind"] == "stuck_tool"]
    assert stuck == []                                          # only 2 consecutive after the reset


def test_different_error_string_resets_count_to_one():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"}, step=0, ts=0.0))
    c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"}, step=1, ts=1.0))
    c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "DNS lookup failed"}, step=2, ts=2.0))
    c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "DNS lookup failed"}, step=3, ts=3.0))
    stuck = [r for r in st.rows if r["kind"] == "stuck_tool"]
    assert stuck == []                                          # never 3 consecutive identical


def test_interleaved_different_tool_does_not_break_streak():
    st = _FakeStore(); c = ReliabilityCollector(st)
    c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"}, step=0, ts=0.0))
    c.record(_ev("tool_result", {"tool": "weather", "ok": False, "result": "geocode failed"}, step=1, ts=1.0))
    c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"}, step=2, ts=2.0))
    c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"}, step=3, ts=3.0))
    stuck = [r for r in st.rows if r["kind"] == "stuck_tool"]
    assert len(stuck) == 1 and stuck[0]["entity"] == "web_search"


def test_failure_with_empty_detail_resets_streak_and_never_records():
    st = _FakeStore(); c = ReliabilityCollector(st)
    for i in range(5):
        c.record(_ev("tool_result", {"tool": "noop", "ok": False, "result": ""}, step=i, ts=float(i)))
    stuck = [r for r in st.rows if r["kind"] == "stuck_tool"]
    assert stuck == []


def test_streak_state_bounded_like_pending():
    st = _FakeStore(); c = ReliabilityCollector(st, max_pending=2)
    for i in range(5):
        c.record(_ev("tool_result", {"tool": f"t{i}", "ok": False, "result": f"err {i}"}, step=i, ts=float(i)))
    assert len(c._streak) <= 2                                  # never leaks


def test_loop_health_reports_stuck_tool(tmp_path):
    from engine.reliability.store import ReliabilityStore
    store = ReliabilityStore(str(tmp_path / "rel.db"), retention_days=30)
    c = ReliabilityCollector(store)
    now = 1_700_000_000.0
    for i in range(3):
        c.record(_ev("tool_result", {"tool": "web_search", "ok": False, "result": "HTTP 500 timeout"},
                     step=i, ts=now + i))
    lh = store.loop_health(days=30, now=now + 10)
    assert lh["stuck_tool"]["total"] == 1
