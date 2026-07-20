import pytest

from engine.events import EventBus
from engine.loop import LoopDeps, run_loop
from engine.modes.manual import ManualMode
from engine.modes.native import NativeMode
from engine.model_client import ModelError
from engine.protocol import ModelResponse
from engine.state import SessionStore
from engine.tools.base import Tool, ToolRegistry
from engine.tools.calculator import CalculatorTool
from pydantic import BaseModel


class FakeModel:
    """Returns scripted ModelResponses in order; records requests."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None, reasoning=None):
        self.requests.append({"messages": messages, "tools": tools})
        if not self._responses:
            raise AssertionError("FakeModel ran out of scripted responses")
        return self._responses.pop(0)


class BoomTool(Tool):
    name = "boom"
    description = "always raises"

    class Params(BaseModel):
        pass

    async def run(self, args):
        raise RuntimeError("kaboom")


def deps_with(model, mode, max_steps=6):
    reg = ToolRegistry()
    reg.register(CalculatorTool())
    reg.register(BoomTool())
    return LoopDeps(mode=mode, registry=reg, model_client=model,
                    store=SessionStore(), events=EventBus(), max_steps=max_steps)


def kinds(bus, session="s"):
    return [e.kind for e in bus.recent(session)]


def native_tool_call(expr, cid="c1"):
    return ModelResponse(content=None, tool_calls=[
        {"id": cid, "function": {"name": "calculator", "arguments": f'{{"expression": "{expr}"}}'}}])


async def test_ask_user_options_surface_in_final_event():
    # a clarifying question with choices must carry its options in the `final` event so the
    # dashboard can render one-tap buttons.
    import json

    from engine.tools.clarify import AskUserTool
    model = FakeModel([ModelResponse(content=None, tool_calls=[
        {"id": "c1", "function": {"name": "ask_user",
         "arguments": json.dumps({"question": "Which chart?", "options": ["bar", "line"]})}}])])
    reg = ToolRegistry()
    reg.register(AskUserTool())
    d = LoopDeps(mode=NativeMode(), registry=reg, model_client=model,
                 store=SessionStore(), events=EventBus(), max_steps=6)
    out = await run_loop(d, "s", "r", "make me a chart")
    assert "Which chart?" in out                       # terminal: the question is the answer
    fin = next(e for e in d.events.recent("s") if e.kind == "final")
    assert fin.data.get("options") == ["bar", "line"]


async def test_native_single_tool_then_final():
    model = FakeModel([native_tool_call("47*89"), ModelResponse(content="It is 4183")])
    d = deps_with(model, NativeMode())
    out = await run_loop(d, "s", "r", "what is 47*89?")
    assert out == "It is 4183"
    ks = kinds(d.events)
    for k in ("info", "model_request", "model_response", "tool_call", "validation", "tool_result", "final"):
        assert k in ks
    # tool actually executed -> result 4183 present
    tr = next(e for e in d.events.recent("s") if e.kind == "tool_result")
    assert tr.data["result"] == "4183" and tr.data["ok"] is True


async def test_validation_error_then_recovery():
    # first call omits required 'expression', second is valid
    bad = ModelResponse(content=None, tool_calls=[
        {"id": "c1", "function": {"name": "calculator", "arguments": "{}"}}])
    model = FakeModel([bad, native_tool_call("2+2", "c2"), ModelResponse(content="4")])
    d = deps_with(model, NativeMode())
    out = await run_loop(d, "s", "r", "add")
    assert out == "4"
    val = [e for e in d.events.recent("s") if e.kind == "validation"]
    assert val[0].data["ok"] is False and "expression" in val[0].data["error"]


async def test_tool_exception_is_caught_and_fed_back():
    boom = ModelResponse(content=None, tool_calls=[
        {"id": "c1", "function": {"name": "boom", "arguments": "{}"}}])
    model = FakeModel([boom, ModelResponse(content="recovered")])
    d = deps_with(model, NativeMode())
    out = await run_loop(d, "s", "r", "explode")
    assert out == "recovered"
    tr = next(e for e in d.events.recent("s") if e.kind == "tool_result")
    assert tr.data["ok"] is False and "failed" in tr.data["result"]


async def test_max_steps_graceful_stop():
    # model never finalizes: asks for the tool with DISTINCT args each time (so it's the
    # MAX_STEPS ceiling that stops it, not the observer's repeated-call detection)
    model = FakeModel([native_tool_call(f"{i}+1", f"c{i}") for i in range(10)])
    d = deps_with(model, NativeMode(), max_steps=3)
    out = await run_loop(d, "s", "r", "loop forever")
    assert "couldn't complete" in out.lower()
    assert any(e.kind == "error" and e.data.get("message") == "max steps exceeded"
               for e in d.events.recent("s"))


async def test_manual_parse_failure_then_reprompt_then_final():
    model = FakeModel([
        ModelResponse(content="I really can't do JSON, sorry"),
        ModelResponse(content='{"action":"final","answer":"done"}'),
    ])
    d = deps_with(model, ManualMode())
    out = await run_loop(d, "s", "r", "hi")
    assert out == "done"
    assert any(e.kind == "reprompt" for e in d.events.recent("s"))


async def test_manual_gives_up_after_one_reprompt():
    model = FakeModel([
        ModelResponse(content="nope"),
        ModelResponse(content="still nope"),
    ])
    d = deps_with(model, ManualMode())
    out = await run_loop(d, "s", "r", "hi")
    assert "valid response" in out.lower()


async def test_model_error_is_graceful():
    class Dead:
        async def chat(self, **kw):
            raise ModelError("connection refused")
    d = deps_with(Dead(), NativeMode())
    out = await run_loop(d, "s", "r", "hi")
    assert "couldn't reach the model" in out.lower()


async def test_truncated_tool_call_reprompts_then_recovers():
    bad = ModelResponse(content=None, finish_reason="length", tool_calls=[
        {"id": "c1", "function": {"name": "calculator", "arguments": '{"expression": "unterminated'}}])
    model = FakeModel([bad, ModelResponse(content="here you go")])
    d = deps_with(model, NativeMode())
    out = await run_loop(d, "s", "r", "write a long thing")
    assert out == "here you go"
    assert any("CUT OFF" in (m.get("content") or "") for m in d.store.conversation("s"))


async def test_truncated_twice_gives_concise_message():
    bad = ModelResponse(content=None, finish_reason="length", tool_calls=[
        {"id": "c1", "function": {"name": "calculator", "arguments": '{"expression": "unterminated'}}])
    d = deps_with(FakeModel([bad, bad]), NativeMode())
    out = await run_loop(d, "s", "r", "write a long thing")
    assert "cut off" in out.lower() and "concise" in out.lower()
