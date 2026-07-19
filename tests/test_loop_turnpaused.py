import pytest

from engine.approvals.types import TurnPaused
from engine.events import EventBus
from engine.loop import LoopDeps, run_loop
from engine.modes.native import NativeMode
from engine.protocol import ModelResponse
from engine.state import SessionStore
from engine.tools.base import Tool, ToolRegistry
from pydantic import BaseModel

from tests.test_loop import FakeModel, native_tool_call


class PauseTool(Tool):
    name = "dep_install"
    description = "raises TurnPaused to simulate an in-flight approval gate"

    class Params(BaseModel):
        pass

    async def run(self, args):
        raise TurnPaused("r1", "dep-install", "waiting...")


def deps_with_pause(model, mode, max_steps=6):
    reg = ToolRegistry()
    reg.register(PauseTool())
    return LoopDeps(mode=mode, registry=reg, model_client=model,
                    store=SessionStore(), events=EventBus(), max_steps=max_steps)


def pause_tool_call(cid="c1"):
    return ModelResponse(content=None, tool_calls=[
        {"id": cid, "function": {"name": "dep_install", "arguments": "{}"}}])


async def test_turnpaused_ends_turn_with_message_and_event():
    model = FakeModel([pause_tool_call()])
    d = deps_with_pause(model, NativeMode())
    out = await run_loop(d, "s", "r", "install numpy please")

    assert out == "waiting..."

    paused = [e for e in d.events.recent("s") if e.kind == "paused"]
    assert len(paused) == 1
    assert paused[0].data == {"req_id": "r1", "kind": "dep-install"}

    assert any(m.get("role") == "assistant" and m.get("content") == "waiting..."
               for m in d.store.conversation("s"))
