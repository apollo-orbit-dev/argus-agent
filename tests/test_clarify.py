import asyncio

from engine.events import EventBus
from engine.loop import LoopDeps, run_loop
from engine.modes.native import NativeMode
from engine.protocol import ModelResponse
from engine.state import SessionStore
from engine.tools.base import ToolRegistry
from engine.tools.clarify import AskUserTool


def test_ask_user_formats_question_and_options():
    t = AskUserTool()
    assert t.terminal is True
    out = asyncio.run(t.run(t.Params(question="Which city?", options=["Paris", "London"])))
    assert "Which city?" in out and "1. Paris" in out and "2. London" in out
    assert asyncio.run(t.run(t.Params(question="What time?"))) == "What time?"


class _FakeModel:
    def __init__(self, responses):
        self._r = list(responses)
    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None, reasoning=None):
        return self._r.pop(0)


async def test_clarify_ends_the_turn():
    reg = ToolRegistry()
    reg.register(AskUserTool())
    # model asks a clarifying question via ask_user, then would keep going — but it must NOT
    model = _FakeModel([
        ModelResponse(content=None, tool_calls=[
            {"id": "c1", "function": {"name": "ask_user",
                                      "arguments": '{"question": "Which city do you mean?"}'}}]),
        ModelResponse(content="this must never be reached"),
    ])
    deps = LoopDeps(mode=NativeMode(), registry=reg, model_client=model,
                    store=SessionStore(), events=EventBus(), max_steps=6)
    out = await run_loop(deps, "s", "r", "what's the weather there?")
    assert out == "Which city do you mean?"
    kinds = [e.kind for e in deps.events.recent("s")]
    assert kinds.count("model_response") == 1  # loop stopped after the terminal tool
    assert "final" in kinds
