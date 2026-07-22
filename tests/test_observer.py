from pydantic import BaseModel

from engine.events import EventBus
from engine.loop import LoopDeps, run_loop
from engine.modes.native import NativeMode
from engine.protocol import ModelResponse
from engine.state import SessionStore
from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool


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
