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


def test_make_judge_backend_selection():
    assert make_judge(None) is None
    assert callable(make_judge("claude:opus"))     # CLI backend
    assert callable(make_judge("main"))            # ModelClient backend (constructs, no live call)
