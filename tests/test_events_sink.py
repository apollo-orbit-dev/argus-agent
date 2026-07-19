import asyncio
from engine.events import EventBus, StepEvent


def _ev(kind="tool_result", sid="s"):
    return StepEvent(run_id="r", session_id=sid, step=1, kind=kind, data={"ok": True}, ts=1.0)


def test_sink_receives_published_events():
    bus = EventBus()
    seen = []
    bus.add_sink(seen.append)
    asyncio.run(bus.publish(_ev()))
    assert len(seen) == 1 and seen[0].kind == "tool_result"


def test_raising_sink_does_not_break_publish():
    bus = EventBus()
    def boom(ev): raise RuntimeError("sink failure")
    ok = []
    bus.add_sink(boom)
    bus.add_sink(ok.append)          # a later good sink still runs
    asyncio.run(bus.publish(_ev()))  # must not raise
    assert len(ok) == 1
    assert bus.recent("s")           # event still reached history
