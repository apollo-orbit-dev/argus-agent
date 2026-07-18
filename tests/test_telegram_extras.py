import asyncio

import pytest

from config import Config
from engine.engine import Engine
from backend.telegram_bot import (
    BOT_COMMANDS, _short_args, cron_text, format_run_status, tools_text,
)


def test_format_run_status_working():
    out = format_run_status({"running": True, "current_step": 3, "max_steps": 6,
                             "last_tool": "web_search", "turns": 2, "messages": 5})
    assert "Working" in out and "3/6" in out and "web_search" in out


def test_format_run_status_idle():
    out = format_run_status({"running": False, "turns": 2, "messages": 5})
    assert "Idle" in out and "step" not in out.split("\n")[0]


def test_status_command_registered():
    assert "status" in [c for c, _ in BOT_COMMANDS]


async def test_engine_interrupt_awaits_unwind():
    e = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""))
    assert await e.interrupt("s") is False            # nothing running

    async def _slow():
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            raise
    t = asyncio.ensure_future(_slow())
    await asyncio.sleep(0)
    e._running["s"] = t
    assert await e.interrupt("s") is True             # cancels AND awaits unwind
    assert t.cancelled()


def test_tools_text():
    out = tools_text({"builtin": [{"name": "calculator"}], "created": [{"name": "greet"}]})
    assert "calculator" in out and "greet" in out and "Built-in" in out and "Created" in out


def test_cron_text():
    assert "No scheduled" in cron_text([])
    out = cron_text([{"instruction": "daily forecast", "schedule": "every day at 07:00", "next_run": "2026-07-13T07:00"}])
    assert "daily forecast" in out and "07:00" in out


async def test_engine_stop_cancels_running():
    """/stop cancels the in-flight run for a session."""
    e = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""))
    assert e.stop("s") is False                      # nothing running

    async def _long():
        await asyncio.sleep(30)
    t = asyncio.create_task(_long())
    await asyncio.sleep(0)
    e._running["s"] = t
    assert e.stop("s") is True                        # cancels it
    with pytest.raises(asyncio.CancelledError):
        await t
    assert e.stop("s") is False                       # nothing running anymore (task done)


def test_repetition_hint():
    """The repetition detector nudges when a non-trivial tool recurs across turns."""
    e = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""))
    assert e._repetition_hint("s") == ""                       # no history
    e._turn_tools["s"] = [{"weather"}, {"weather"}]
    hint = e._repetition_hint("s")
    assert "weather" in hint and "skill" in hint.lower()
    e._turn_tools["triv"] = [{"calculator"}, {"calculator"}]   # trivial tool -> no nudge
    assert e._repetition_hint("triv") == ""
    e._turn_tools["once"] = [{"weather"}]                       # only one turn -> no nudge
    assert e._repetition_hint("once") == ""


class _FakeMsg:
    def __init__(self):
        self.texts = []

    async def edit_text(self, text, **kw):
        self.texts.append(text)


class _FakeEngine:
    def __init__(self, events):
        self._events = events

    async def subscribe(self, session_id):
        for e in self._events:
            yield e


async def test_verbose_progress_tracks_had_tools():
    """had_tools flips when a tool runs (so the caller keeps the verbose history as its own msg)."""
    from types import SimpleNamespace as NS
    from backend.telegram_bot import _consume_progress
    events = [NS(kind="tool_call", data={"tool": "calculator", "args": {}}),
              NS(kind="final", data={})]
    msg = _FakeMsg(); state = {"had_tools": False}
    await _consume_progress(_FakeEngine(events), "s", msg, verbose=True, state=state)
    assert state["had_tools"] is True
    assert any("calculator" in t for t in msg.texts)      # stacked the call


async def test_verbose_progress_no_tools_leaves_flag_false():
    from types import SimpleNamespace as NS
    from backend.telegram_bot import _consume_progress
    events = [NS(kind="model_response", data={}), NS(kind="final", data={})]
    state = {"had_tools": False}
    await _consume_progress(_FakeEngine(events), "s", _FakeMsg(), verbose=True, state=state)
    assert state["had_tools"] is False                    # no tools → answer edits the placeholder


def test_short_args_render():
    assert _short_args({"date": "2026-07-11"}) == "(date=2026-07-11)"
    assert _short_args({}) == ""
    long = _short_args({"q": "x" * 100})
    assert long.endswith("…)") and len(long) < 80     # truncated


# ---- verbose tool/skill history is rebuilt from the authoritative event log ----
# (regression: the racy live-consumer flag could leave had_tools False and OVERWRITE the list)
from backend.telegram_bot import _verbose_history
from engine.events import StepEvent


def test_verbose_history_renders_tools_and_skill():
    evs = [
        StepEvent("r", "s", 1, "skill", {"active_skill": "report_builder"}, 100.0),
        StepEvent("r", "s", 1, "tool_call", {"tool": "get_account_data", "args": {"date_range": "last night"}}, 101.0),
        StepEvent("r", "s", 2, "model_response", {}, 102.0),
        StepEvent("r", "s", 2, "tool_call", {"tool": "ascii_chart", "args": {"chart_type": "composition"}}, 103.0),
    ]
    out = _verbose_history(evs)
    assert "🎯 skill: report_builder" in out
    assert "get_account_data" in out and "ascii_chart" in out
    assert out.count("⚙️") == 2                                  # both tool calls present


def test_verbose_history_empty_without_tools():
    assert _verbose_history([StepEvent("r", "s", 1, "model_response", {}, 1.0)]) == ""


def test_had_tools_scoped_to_this_turn_by_ts():
    # events from a PRIOR turn (older ts) must not count toward this turn's had-tools decision
    prior = StepEvent("r0", "s", 1, "tool_call", {"tool": "calculator"}, 50.0)
    now = [StepEvent("r1", "s", 1, "tool_call", {"tool": "ascii_chart"}, 200.0)]
    recent = [prior] + now
    start_ts = 100.0
    turn = [ev for ev in recent if ev.ts >= start_ts]
    assert any(ev.kind == "tool_call" for ev in turn)            # this turn's tool counts
    assert all(ev.ts >= start_ts for ev in turn)                 # prior turn's tool excluded


def test_split_for_telegram_chunks_long_text():
    from backend.telegram_bot import split_for_telegram
    assert split_for_telegram("hi") == ["hi"]
    long = "\n\n".join(f"Paragraph {i}: " + "word " * 200 for i in range(15))
    chunks = split_for_telegram(long, limit=1000)
    assert len(chunks) >= 5 and all(len(c) <= 1000 for c in chunks)
    joined = " ".join(chunks)
    for i in range(15):
        assert f"Paragraph {i}:" in joined
