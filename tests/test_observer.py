import httpx
import pytest
from pydantic import BaseModel

from engine.events import EventBus
from engine.loop import LoopDeps, run_loop
from engine.modes.native import NativeMode
from engine.protocol import ModelResponse
from engine.state import SessionStore
from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool
from engine.tools.web_search import WebSearchTool


def calc_call(expr, cid):
    return ModelResponse(content=None, tool_calls=[
        {"id": cid, "function": {"name": "calculator",
                                 "arguments": f'{{"expression": "{expr}"}}'}}])


class _Model:
    def __init__(self, responses):
        self._r = list(responses)
    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None, reasoning=None):
        return self._r.pop(0) if self._r else calc_call("1+1", "cN")


def _deps(model, threshold=2, enable=True):
    reg = ToolRegistry(); reg.register(CalculatorTool())
    return LoopDeps(mode=NativeMode(), registry=reg, model_client=model,
                    store=SessionStore(), events=EventBus(), max_steps=8,
                    enable_observer=enable, observer_threshold=threshold)


async def test_observer_nudges_then_stops_on_repeated_calls():
    # model keeps making the SAME call forever
    d = _deps(_Model([calc_call("1+1", f"c{i}") for i in range(8)]), threshold=2)
    out = await run_loop(d, "s", "r", "loop please")
    ev = d.events.recent("s")
    issues = [e.data.get("issue") for e in ev if e.kind == "observer"]
    assert "repeat_nudge" in issues      # nudged at the 2nd identical call
    assert "stuck_repeating" in issues   # stopped at the 3rd
    assert "make progress" in out.lower()
    # it stopped well before max_steps (would be 8)
    assert max(e.step for e in ev) <= 3


def recreate_call(name, code, cid):
    return ModelResponse(content=None, tool_calls=[
        {"id": cid, "function": {"name": "create_tool",
                                 "arguments": f'{{"name": "{name}", "code": "{code}"}}'}}])


async def test_observer_stops_recreating_same_tool_name():
    """Recreating the SAME tool name with DIFFERENT code each time (the flail that blew the
    step budget) must be caught even though the args-signature differs every call."""
    reg = ToolRegistry(); reg.register(CalculatorTool())
    # a create_tool that always 'fails' to satisfy the model, so it keeps rewriting the body
    responses = [recreate_call("sleep_report", f"v{i}", f"c{i}") for i in range(9)]
    d = LoopDeps(mode=NativeMode(), registry=reg, model_client=_Model(responses),
                 store=SessionStore(), events=EventBus(), max_steps=12, observer_threshold=2)
    out = await run_loop(d, "s", "r", "fix the report")
    ev = d.events.recent("s")
    issues = [e.data.get("issue") for e in ev if e.kind == "observer"]
    assert "stuck_recreating" in issues
    assert max(e.step for e in ev) <= 5          # stops on the 5th recreate, before max_steps
    assert "rebuild" in out.lower()


class _FakeCreateTool:
    """A stand-in create_tool that succeeds so the loop executes it (the real nudge tracking
    runs after execution)."""
    name = "create_tool"
    description = "create a tool"
    terminal = False

    class Params(BaseModel):
        name: str = ""
        code: str = ""

    async def run(self, args):
        return f"create_tool: '{args.name}' created."


async def test_observer_nudges_create_without_verify():
    """Creating tool after tool (DIFFERENT names) without running one should nudge — the
    recreate-by-name breaker can't catch this, so consecutive-creates does."""
    reg = ToolRegistry(); reg.register(CalculatorTool()); reg.register(_FakeCreateTool())
    # three distinct-name creates in a row (no execution of a built tool between them)
    responses = [recreate_call(f"probe_{i}", f"c{i}", f"id{i}") for i in range(3)]
    d = LoopDeps(mode=NativeMode(), registry=reg, model_client=_Model(responses),
                 store=SessionStore(), events=EventBus(), max_steps=4, observer_threshold=2)
    await run_loop(d, "s", "r", "fix it")
    ev = d.events.recent("s")
    issues = [e.data.get("issue") for e in ev if e.kind == "observer"]
    assert "create_without_verify" in issues
    # the nudge was injected into the conversation for the model to see
    msgs = d.store.conversation("s")
    assert any("without running one" in str(m.get("content", "")) for m in msgs)


async def test_observer_off_lets_it_run_to_max_steps():
    d = _deps(_Model([calc_call("1+1", f"c{i}") for i in range(12)]), enable=False)
    out = await run_loop(d, "s", "r", "loop please")
    assert "couldn't complete" in out.lower()  # hits MAX_STEPS instead
    assert not any(e.kind == "observer" for e in d.events.recent("s"))


async def test_observer_allows_distinct_calls():
    # different args each time -> no observer trigger, ends with the final answer
    d = _deps(_Model([calc_call("1+1", "c1"), calc_call("2+2", "c2"),
                      ModelResponse(content="done")]))
    out = await run_loop(d, "s", "r", "two sums")
    assert out == "done"
    assert not any(e.kind == "observer" for e in d.events.recent("s"))


def bad_calc_call(cid):
    """calculator with a misspelled argument — fails schema validation, never executes."""
    return ModelResponse(content=None, tool_calls=[
        {"id": cid, "function": {"name": "calculator",
                                 "arguments": '{"expresion": "1+1"}'}}])


async def test_observer_nudges_repeats_that_fail_validation():
    """The validation-failure branch used to return to the top of the loop before the nudge, so a
    call repeating with MALFORMED ARGUMENTS was counted toward the abort but never interrupted —
    even though a validation error carries almost nothing the second time."""
    d = _deps(_Model([bad_calc_call(f"c{i}") for i in range(8)]), threshold=2)
    out = await run_loop(d, "s", "r", "loop on bad args")
    issues = [e.data.get("issue") for e in d.events.recent("s") if e.kind == "observer"]
    assert "repeat_nudge" in issues
    assert "stuck_repeating" in issues
    assert "make progress" in out.lower()


async def test_validation_repeats_still_stop_at_the_threshold():
    """The nudge must not delay the abort: a call that keeps failing validation still ends the turn
    rather than burning the step budget."""
    d = _deps(_Model([bad_calc_call(f"c{i}") for i in range(8)]), threshold=2)
    await run_loop(d, "s", "r", "loop on bad args")
    assert max(e.step for e in d.events.recent("s")) <= 3


# --- fuzzy repeat nudge (third observer channel) ---------------------------------------------

@pytest.fixture(autouse=True)
def _mock_websearch_transport(monkeypatch):
    """web_search actually executes once it passes validation — mock the transport so these tests
    never hit the network, and clear the tool's process-wide cache/rate-limit state so calls made
    here don't bleed into (or get suppressed by) other tests in the same process."""
    WebSearchTool._cache.clear()
    WebSearchTool._calls.clear()

    def handler(req):
        return httpx.Response(200, json={"results": [{"title": "T", "url": "u", "content": "c"}]})
    real_init = httpx.AsyncClient.__init__
    def fake_init(self, *a, **k):
        k["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **k)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    yield
    WebSearchTool._cache.clear()
    WebSearchTool._calls.clear()


def search_call(query, cid):
    return ModelResponse(content=None, tool_calls=[
        {"id": cid, "function": {"name": "web_search",
                                 "arguments": f'{{"query": "{query}"}}'}}])


def _deps_ws(model, threshold=2, window=3, jaccard=0.4, enable=True):
    reg = ToolRegistry()
    reg.register(CalculatorTool())
    reg.register(WebSearchTool("http://x"))
    return LoopDeps(mode=NativeMode(), registry=reg, model_client=model,
                    store=SessionStore(), events=EventBus(), max_steps=8,
                    enable_observer=enable, observer_threshold=threshold,
                    fuzzy_repeat_window=window, fuzzy_repeat_jaccard=jaccard)


async def test_fuzzy_nudge_on_refining_search_queries():
    """Progressively refining the same search ('X' -> 'X Y' -> 'X Y Z') is high value-token
    overlap across the window -> one fuzzy nudge, and the turn does NOT stop."""
    d = _deps_ws(_Model([search_call("X", "c1"), search_call("X Y", "c2"),
                         search_call("X Y Z", "c3"), ModelResponse(content="done")]))
    out = await run_loop(d, "s", "r", "search for X")
    ev = d.events.recent("s")
    issues = [e.data.get("issue") for e in ev if e.kind == "observer"]
    assert "fuzzy_repeat_nudge" in issues
    assert "stuck_repeating" not in issues
    assert out == "done"                              # turn ran to completion, wasn't ended


async def test_fuzzy_nudge_skipped_for_unrelated_queries():
    """Three same-tool calls with near-zero token overlap (unrelated queries) must not trip the
    fuzzy channel — this is a genuine multi-topic investigation, not a refine loop."""
    d = _deps_ws(_Model([search_call("apple", "c1"), search_call("banana", "c2"),
                         search_call("cherry", "c3"), ModelResponse(content="done")]))
    out = await run_loop(d, "s", "r", "look up three things")
    issues = [e.data.get("issue") for e in d.events.recent("s") if e.kind == "observer"]
    assert "fuzzy_repeat_nudge" not in issues
    assert out == "done"


async def test_fuzzy_nudge_fires_once_per_turn():
    """Five refining calls in a row must still only produce ONE fuzzy_repeat_nudge event
    (one-shot flag), not one per qualifying window."""
    queries = ["x", "x y", "x y z", "x y z w", "x y z w v"]
    d = _deps_ws(_Model([search_call(q, f"c{i}") for i, q in enumerate(queries)]
                        + [ModelResponse(content="done")]), threshold=10)
    out = await run_loop(d, "s", "r", "keep refining")
    issues = [e.data.get("issue") for e in d.events.recent("s") if e.kind == "observer"]
    assert issues.count("fuzzy_repeat_nudge") == 1
    assert out == "done"


async def test_fuzzy_nudge_skipped_on_interleaved_tools():
    """An interleaved different tool breaks the 'contiguous same-tool tail' requirement, so a
    genuine multi-tool investigation (search, compute, search) never trips the fuzzy channel."""
    d = _deps_ws(_Model([search_call("X", "c1"), calc_call("1+1", "c2"),
                         search_call("X Y", "c3"), ModelResponse(content="done")]))
    out = await run_loop(d, "s", "r", "search then compute then search")
    issues = [e.data.get("issue") for e in d.events.recent("s") if e.kind == "observer"]
    assert "fuzzy_repeat_nudge" not in issues
    assert out == "done"


async def test_exact_repeat_stop_takes_precedence_over_fuzzy():
    """An identical call repeated to the exact-repeat STOP threshold trips 'stuck_repeating' and
    ends the turn — even though the same window would also satisfy the fuzzy Jaccard check
    (identical args => Jaccard 1.0). The STOP path returns before the fuzzy hook ever runs, so no
    fuzzy_repeat_nudge should appear alongside it."""
    d = _deps_ws(_Model([search_call("X", f"c{i}") for i in range(6)]), threshold=2)
    out = await run_loop(d, "s", "r", "loop the same search")
    issues = [e.data.get("issue") for e in d.events.recent("s") if e.kind == "observer"]
    assert "stuck_repeating" in issues
    assert "fuzzy_repeat_nudge" not in issues
    assert "make progress" in out.lower()
