"""Routines — store (schema/validation/persistence), executor (linear hybrid), and the agent tools."""
import asyncio

import pytest

from engine.routines.executor import RoutineExecutor, _render_args, _substitute
from engine.routines.store import (Routine, RoutineStore, RoutineValidationError, validate_routine)
from engine.tools.routine_tools import ListRoutinesTool, RunRoutineTool


def _routine(**over):
    base = dict(name="morning", steps=[
        {"type": "tool", "id": "metrics", "tool": "fetch_metrics", "args": {}},
        {"type": "model", "id": "brief", "prompt": "Brief from:\n{{metrics}}", "skill": "report_builder"},
    ])
    base.update(over)
    return Routine(**base)


# ---- store: validation ----
def test_valid_routine_passes():
    validate_routine(_routine())


def test_bad_name_rejected():
    with pytest.raises(RoutineValidationError):
        validate_routine(_routine(name="Morning Report"))


def test_empty_steps_rejected():
    with pytest.raises(RoutineValidationError):
        validate_routine(_routine(steps=[]))


def test_unknown_tool_rejected_when_known_given():
    with pytest.raises(RoutineValidationError):
        validate_routine(_routine(), known_tools={"weather"})   # fetch_metrics not in set


def test_duplicate_step_id_rejected():
    with pytest.raises(RoutineValidationError):
        validate_routine(_routine(steps=[
            {"type": "tool", "id": "x", "tool": "weather", "args": {}},
            {"type": "tool", "id": "x", "tool": "weather", "args": {}}]))


def test_output_must_reference_a_step():
    with pytest.raises(RoutineValidationError):
        validate_routine(_routine(output="nonexistent"))


def test_default_step_id_filled():
    r = _routine(steps=[{"type": "tool", "tool": "weather", "args": {}}])
    validate_routine(r)
    assert r.steps[0]["id"] == "weather"          # default id = tool name


def test_output_id_defaults_to_last_step():
    assert _routine().output_id == "brief"
    assert _routine(output="metrics").output_id == "metrics"


# ---- store: persistence ----
def test_store_round_trip(tmp_path):
    s = RoutineStore(str(tmp_path / "routines"))
    s.save(_routine())
    s2 = RoutineStore(str(tmp_path / "routines"))
    s2.load_dir()
    assert s2.get("morning") is not None
    assert len(s2.get("morning").steps) == 2


def test_store_delete(tmp_path):
    s = RoutineStore(str(tmp_path / "routines"))
    s.save(_routine())
    assert s.delete("morning") is True
    assert s.get("morning") is None
    assert s.delete("morning") is False


# ---- templating ----
def test_substitute_builtins_and_ids():
    ctx = {"metrics": "7.7h", "today": "2026-07-14"}
    assert _substitute("On {{today}}: {{metrics}}", ctx) == "On 2026-07-14: 7.7h"
    assert _substitute("{{missing}}!", ctx) == "!"        # unknown -> empty


def test_render_args_only_touches_strings():
    ctx = {"loc": "Atlanta, GA"}
    out = _render_args({"location": "{{loc}}", "days": 7, "opts": {"x": "{{loc}}"}}, ctx)
    assert out == {"location": "Atlanta, GA", "days": 7, "opts": {"x": "Atlanta, GA"}}


# ---- executor ----
def _exec(tool_outputs, notifier=None):
    async def run_tool(session_id, name, args):
        if name not in tool_outputs:
            raise ValueError(f"unknown tool '{name}'")
        val = tool_outputs[name]
        if isinstance(val, Exception):
            raise val
        return val(args) if callable(val) else val

    async def run_model(session_id, prompt, skill):
        return f"[model|{skill}] {prompt}"

    return RoutineExecutor(run_tool, run_model, notifier=notifier)


def test_executor_runs_in_order_with_context():
    ex = _exec({"fetch_metrics": "METRICS=7.7h"})
    res = asyncio.run(ex.run(_routine(), "sess", deliver=False))
    assert res.ok
    # the model step received the tool's output via {{metrics}} and its skill
    assert "METRICS=7.7h" in res.output and "report_builder" in res.output


def test_executor_optional_step_continues_on_error():
    r = _routine(steps=[
        {"type": "tool", "id": "w", "tool": "weather", "args": {}, "optional": True},
        {"type": "model", "id": "out", "prompt": "weather was: {{w}}"},
    ])
    ex = _exec({"weather": ValueError("api down")})
    res = asyncio.run(ex.run(r, "sess", deliver=False))
    assert res.ok                                        # optional failure didn't abort
    assert "api down" in res.output                      # error text flowed into context


def test_executor_required_step_aborts():
    r = _routine(steps=[{"type": "tool", "id": "w", "tool": "weather", "args": {}}])
    ex = _exec({"weather": ValueError("boom")})
    res = asyncio.run(ex.run(r, "sess", deliver=False))
    assert not res.ok and "stopped at step 'w'" in res.error


def test_executor_delivers_output_via_notifier():
    sent = {}

    class FakeNotifier:
        async def send(self, channel, message, *, subject="", session_id=None):
            sent.update(channel=channel, message=message, subject=subject)
            return True, "ok"

    r = _routine(deliver={"channel": "telegram", "subject": "Morning"})
    ex = _exec({"fetch_metrics": "METRICS=7.7h"}, notifier=FakeNotifier())
    res = asyncio.run(ex.run(r, "sess", deliver=True))
    assert res.ok and sent["channel"] == "telegram" and sent["subject"] == "Morning"
    assert "METRICS=7.7h" in sent["message"]


def test_executor_no_delivery_when_deliver_false():
    sent = []

    class FakeNotifier:
        async def send(self, *a, **k):
            sent.append(1)
            return True, "ok"

    r = _routine(deliver={"channel": "telegram"})
    ex = _exec({"fetch_metrics": "x"}, notifier=FakeNotifier())
    asyncio.run(ex.run(r, "sess", deliver=False))
    assert sent == []                                    # on-demand path suppresses channel fan-out


# ---- agent tools ----
class _StubExec:
    def __init__(self, result):
        self._result = result

    async def run(self, routine, session_id, **kw):
        return self._result


def test_run_routine_tool_returns_output():
    from engine.routines.executor import RoutineResult
    s = RoutineStore("/tmp/does-not-exist")
    s._routines["morning"] = _routine()
    t = RunRoutineTool(s, _StubExec(RoutineResult("morning", True, "the briefing")), "sess")
    assert asyncio.run(t.run(t.Params(name="morning"))) == "the briefing"
    assert t.echo_result is True


def test_run_routine_unknown_name():
    s = RoutineStore("/tmp/does-not-exist")
    t = RunRoutineTool(s, _StubExec(None), "sess")
    out = asyncio.run(t.run(t.Params(name="nope")))
    assert "no routine named 'nope'" in out


def test_list_routines_tool():
    s = RoutineStore("/tmp/does-not-exist")
    s._routines["morning"] = _routine(description="daily briefing",
                                      trigger={"schedule": "every day at 7am"})
    out = asyncio.run(ListRoutinesTool(s).run(ListRoutinesTool(s).Params()))
    assert "morning" in out and "daily briefing" in out and "7am" in out


# ---- config gate ----
def test_enable_routines_default_and_env_field():
    from config import Config
    c = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="")
    assert c.enable_routines is True
    assert "enable_routines" in c._ENV_FIELDS          # editable via the dashboard env editor


# ---- executor delivery (the "manual run should deliver" fix) ----
class _RecordingNotifier:
    def __init__(self, ok=True, detail="sent"):
        self.ok, self.detail, self.calls = ok, detail, []

    async def send(self, channel, message, *, subject="", session_id=None, **kw):
        self.calls.append({"channel": channel, "message": message,
                           "subject": subject, "session_id": session_id})
        return self.ok, self.detail


def test_executor_delivers_when_channel_set():
    n = _RecordingNotifier()
    ex = _exec({"fetch_metrics": "METRICS=7.7h"}, notifier=n)
    r = _routine(deliver={"channel": "push", "subject": "AM"})
    res = asyncio.run(ex.run(r, "sess", deliver=True))
    assert res.ok and res.delivered and res.delivery_error is None
    assert len(n.calls) == 1 and n.calls[0]["channel"] == "push"
    assert n.calls[0]["subject"] == "AM"


def test_executor_deliver_false_skips_send():
    n = _RecordingNotifier()
    ex = _exec({"fetch_metrics": "x"}, notifier=n)
    r = _routine(deliver={"channel": "push"})
    res = asyncio.run(ex.run(r, "sess", deliver=False))
    assert res.ok and not res.delivered and not n.calls


def test_executor_surfaces_delivery_failure():
    n = _RecordingNotifier(ok=False, detail="ntfy returned HTTP 500")
    ex = _exec({"fetch_metrics": "x"}, notifier=n)
    r = _routine(deliver={"channel": "push"})
    res = asyncio.run(ex.run(r, "sess", deliver=True))
    assert res.ok                                   # the routine ran fine…
    assert not res.delivered                         # …but delivery failed, and we now KNOW
    assert res.delivery_error == "ntfy returned HTTP 500"


def test_executor_no_delivery_when_channel_none():
    n = _RecordingNotifier()
    ex = _exec({"fetch_metrics": "x"}, notifier=n)
    res = asyncio.run(ex.run(_routine(deliver={"channel": "none"}), "sess", deliver=True))
    assert not n.calls and not res.delivered


# ---- executor: routine_result outcome emit ----
def test_executor_emits_routine_result():
    ex = _exec({"comprehensive": "X"})
    events = []
    r = _routine(steps=[{"type": "tool", "id": "comprehensive", "tool": "comprehensive", "args": {}}])
    res = asyncio.run(ex.run(r, "sess", deliver=False, on_result=lambda payload: events.append(payload)))
    assert res.ok
    assert len(events) == 1
    p = events[0]
    assert p["name"] == r.name and p["ok"] is True
    assert p["steps_total"] == 1 and p["steps_ok"] == 1 and isinstance(p["ms"], int)
