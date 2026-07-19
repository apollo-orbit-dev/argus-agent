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
