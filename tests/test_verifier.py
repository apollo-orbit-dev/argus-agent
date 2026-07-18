"""The (deliberately narrow) post-action completeness verifier + interrupt().

The verifier guards ONE failure: a batch removal request ("delete them all") where the
model over-claims after removing only some. It must NOT fire on single-item requests, on
create/switch turns, or judge the QUALITY of a tool's output (that regression once turned a
one-line "use tool B instead of A" fix into a 16-step rebuild flail).
"""
import types

import pytest

from config import Config
from engine.engine import Engine
from engine.events import EventBus, StepEvent
from engine.loop import LoopDeps
from engine.modes.native import NativeMode
from engine.state import SessionStore
from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool


def _engine():
    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="")
    return Engine(cfg)


def _stub_model(e, reply: str):
    async def _chat(messages, **kw):
        _chat.calls += 1
        return types.SimpleNamespace(content=reply)
    _chat.calls = 0
    e._model_client = lambda: types.SimpleNamespace(chat=_chat)
    return _chat


def _deps():
    reg = ToolRegistry(); reg.register(CalculatorTool())
    return LoopDeps(mode=NativeMode(), registry=reg, model_client=None,
                    store=SessionStore(), events=EventBus(), max_steps=8)


async def _emit(e, session_id, run_id, tool):
    await e.events.publish(StepEvent(run_id, session_id, 1, "tool_call", {"tool": tool}))


# ---- the gate: only batch removals get checked ----

async def test_skips_single_item_request():
    """'use comprehensive instead of daily' has no batch quantifier — no check at all.
    This is the exact regression: a simple switch must not be second-guessed."""
    e = _engine()
    chat = _stub_model(e, "INCOMPLETE: report still shows TBD values")
    await _emit(e, "s", "r1", "create_skill")
    out = await e._verify_completion(_deps(), "s", "r1",
                                     "use comprehensive_sleep_report instead of daily_sleep_report",
                                     "Done — switched the skill to the comprehensive tool.")
    assert out.startswith("Done")
    assert chat.calls == 0                       # never even asked the judge


async def test_skips_non_removal_mutation():
    """A batch word IS present but the ops were creates, not removals — still skip."""
    e = _engine()
    chat = _stub_model(e, "INCOMPLETE")
    await _emit(e, "s", "r1", "create_tool")
    out = await e._verify_completion(_deps(), "s", "r1", "make all three helper tools", "made them")
    assert out == "made them"
    assert chat.calls == 0


async def test_batch_removal_complete():
    e = _engine()
    chat = _stub_model(e, "COMPLETE")
    await _emit(e, "s", "r1", "delete_tool")
    out = await e._verify_completion(_deps(), "s", "r1", "delete all the youtube tools",
                                     "deleted them all")
    assert out == "deleted them all"
    assert chat.calls == 1                        # judged, and it was fine


async def test_batch_removal_incomplete_reruns_bounded(monkeypatch):
    e = _engine()
    _stub_model(e, "INCOMPLETE: 2 of 3 still present")
    await _emit(e, "s", "r1", "delete_tool")

    captured = {}

    async def _fake_loop(deps, session_id, run_id, text):
        captured["max_steps"] = deps.max_steps
        captured["tools"] = deps.registry.names()
        captured["text"] = text
        return "removed the remaining two"
    monkeypatch.setattr("engine.engine.run_loop", _fake_loop)

    out = await e._verify_completion(_deps(), "s", "r1", "delete all the youtube tools",
                                     "deleted them all")
    assert out == "removed the remaining two"
    assert captured["max_steps"] <= 4                          # bounded
    assert "create_tool" not in captured["tools"]              # creation disabled
    assert "create_skill" not in captured["tools"]
    assert "do not create or revise" in captured["text"].lower()


async def test_never_judges_output_quality():
    """Even a batch removal: the judge is told to ignore output data quality. We assert the
    system prompt forbids judging report/data completeness (the scope-creep guard)."""
    assert "NEVER the quality" in Engine.VERIFY_PROMPT
    assert "placeholder values" in Engine.VERIFY_PROMPT


# ---- interrupt ----

async def test_interrupt_no_run():
    e = _engine()
    assert await e.interrupt("s") is False
