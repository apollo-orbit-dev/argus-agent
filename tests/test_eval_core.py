"""Reusable eval core: run_and_capture (observation) + make_judge (backend selection)."""
from engine.eval.capture import run_and_capture
from engine.eval.judge_runner import make_judge


class _Ev:
    def __init__(self, kind, data):
        self.kind, self.data = kind, data


class _FakeEngine:
    def __init__(self, events, final="done", raise_run=False):
        self._events, self._final, self._raise = events, final, raise_run

    async def subscribe(self, session):
        for e in self._events:
            yield e

    async def run_task(self, session, prompt, origin="api"):
        if self._raise:
            raise RuntimeError("boom")
        return self._final


async def test_run_and_capture_collects_tools_and_final():
    evs = [_Ev("info", {}),
           _Ev("tool_call", {"tool": "calculator", "args": {"expression": "2+2"}}),
           _Ev("tool_call", {"tool": "create_table", "args": {"name": "t", "columns": ["a:text"]}}),
           _Ev("final", {"answer": "ok"})]
    r = await run_and_capture(_FakeEngine(evs, final="the answer is 4"), "s", "p", timeout=5)
    assert r["tools"] == ["calculator", "create_table"]
    assert r["create_table_args"] == [{"name": "t", "columns": ["a:text"]}]
    assert r["final"] == "the answer is 4" and r["error"] is None


async def test_run_and_capture_records_error_not_raise():
    r = await run_and_capture(_FakeEngine([], raise_run=True), "s", "p", timeout=5)
    assert r["error"] is not None and r["tools"] == [] and r["final"] == ""


async def test_run_and_capture_collects_observer_issues():
    """The benchmark scores `no_observer` off this key. When it was never collected, the predicate
    saw an empty set and passed VACUOUSLY — a battery could assert 'the loop never gave up' and be
    told it was true on a turn the loop killed. Ordered, one entry per firing."""
    evs = [_Ev("tool_call", {"tool": "web_search", "args": {"query": "x"}}),
           _Ev("observer", {"issue": "repeat_nudge", "tool": "web_search"}),
           _Ev("observer", {"issue": "stuck_repeating", "tool": "web_search"}),
           _Ev("observer", {}),                     # malformed / no issue -> ignored
           _Ev("final", {"answer": "gave up"})]
    r = await run_and_capture(_FakeEngine(evs), "s", "p", timeout=5)
    assert r["observer"] == ["repeat_nudge", "stuck_repeating"]


async def test_run_and_capture_reports_observer_key_even_when_nothing_fired():
    """Always present, so `no_observer` distinguishes 'clean run' from 'never recorded'."""
    r = await run_and_capture(_FakeEngine([_Ev("tool_call", {"tool": "calculator"})]), "s", "p", timeout=5)
    assert r["observer"] == []


async def test_captured_run_feeds_no_observer_scoring():
    """Guards the SEAM, not either side of it. capture and score_case were each individually
    correct while the pipeline between them was broken: capture never emitted `observer`, so
    `no_observer` scored every benchmark run vacuously. Both unit suites stayed green throughout.
    """
    from engine.eval.scoring import score_case

    killed = await run_and_capture(_FakeEngine(
        [_Ev("tool_call", {"tool": "web_search"}),
         _Ev("observer", {"issue": "stuck_repeating", "tool": "web_search"})]), "s", "p", timeout=5)
    verdict = score_case({"no_observer": ["stuck_repeating"]}, killed)
    assert verdict["chain_correct"] is False          # would be True under the old vacuous pass
    assert "stuck_repeating" in verdict["reasons"][0]

    clean = await run_and_capture(_FakeEngine([_Ev("tool_call", {"tool": "web_search"})]), "s", "p", timeout=5)
    assert score_case({"no_observer": ["stuck_repeating"]}, clean)["chain_correct"] is True


def test_make_judge_backend_selection():
    assert make_judge(None) is None
    assert callable(make_judge("claude:opus"))     # CLI backend
    assert callable(make_judge("main"))            # ModelClient backend (constructs, no live call)
