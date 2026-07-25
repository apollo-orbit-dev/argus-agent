import asyncio
import time

import pytest

from engine.events import EventBus, StepEvent


def ev(session, step, kind="info", **data):
    return StepEvent(run_id="r1", session_id=session, step=step, kind=kind,
                     data=data, ts=time.time())


async def test_recent_returns_history():
    bus = EventBus(maxlen=100)
    await bus.publish(ev("s1", 1))
    await bus.publish(ev("s1", 2))
    assert [e.step for e in bus.recent("s1")] == [1, 2]
    assert bus.recent("other") == []


async def test_clear_drops_session_history():
    bus = EventBus(maxlen=100)
    await bus.publish(ev("s1", 1))
    await bus.publish(ev("s2", 1))
    bus.clear("s1")
    assert bus.recent("s1") == []          # s1 replay buffer dropped ("new session")
    assert len(bus.recent("s2")) == 1      # other sessions untouched


def test_engine_new_session_clears_conversation_and_events(tmp_path):
    from config import Config
    from engine.engine import Engine
    eng = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""))
    eng.store.append_message("dashboard", {"role": "user", "content": "hi"})
    asyncio.run(eng.emit("run1", "dashboard", 1, "info", {"text": "hi"}))
    eng.new_session("dashboard")
    assert eng.store.conversation("dashboard") == []
    assert eng.events.recent("dashboard") == []


async def test_live_subscriber_receives_new_events():
    bus = EventBus(maxlen=100)
    got = []

    async def reader():
        async for e in bus.subscribe("s1"):
            got.append(e)
            if len(got) == 1:
                break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.01)  # let subscriber register
    await bus.publish(ev("s1", 2, kind="final"))
    await asyncio.wait_for(task, 1.0)
    assert got[0].step == 2 and got[0].kind == "final"


async def test_subscribe_none_receives_all_sessions():
    bus = EventBus(maxlen=100)
    got = []

    async def reader():
        async for e in bus.subscribe(None):
            got.append(e)
            if len(got) == 2:
                break

    task = asyncio.create_task(reader())
    await asyncio.sleep(0.01)
    await bus.publish(ev("s1", 1))
    await bus.publish(ev("s2", 1))
    await asyncio.wait_for(task, 1.0)
    assert {e.session_id for e in got} == {"s1", "s2"}


async def test_history_capped():
    bus = EventBus(maxlen=3)
    for i in range(5):
        await bus.publish(ev("s1", i))
    assert [e.step for e in bus.recent("s1")] == [2, 3, 4]


async def test_control_channel_does_not_reach_renamed_session_subscriber(tmp_path):
    # Pins the per-session scoping the "__control__" control-channel design depends on, driven
    # through the REAL Engine._emit_session_changed (not a hand-built StepEvent): a subscriber on
    # the "__control__" pseudo-session gets the session_changed event, but a subscriber on the
    # RENAMED session's own id does NOT — publish() only fans out where session_filter is None or
    # matches ev.session_id exactly. Because this goes through the actual emit call, it is a real
    # guardrail against someone later "fixing" the emit back onto the session's own stream (which
    # would break the other-tab/Telegram case, per the bead's design doc): moving the emit's
    # session_id from "__control__" to the renamed session would make this test fail.
    from config import Config
    from engine.engine import Engine

    eng = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""),
                 data_dir=str(tmp_path))
    bus = eng.events
    control_got, session_got = [], []

    async def control_reader():
        async for e in bus.subscribe("__control__"):
            control_got.append(e)
            break

    async def session_reader():
        async for e in bus.subscribe("renamed-sid"):
            session_got.append(e)
            break

    control_task = asyncio.create_task(control_reader())
    session_task = asyncio.create_task(session_reader())
    await asyncio.sleep(0.01)  # let both subscribers register

    eng._emit_session_changed("renamed-sid", "renamed", "New Title")
    await asyncio.wait_for(control_task, 1.0)

    assert len(control_got) == 1 and control_got[0].kind == "session_changed"
    assert control_got[0].session_id == "__control__"
    assert control_got[0].data == {"session_id": "renamed-sid", "action": "renamed",
                                    "name": "New Title"}
    assert session_got == []                 # never delivered — different subscription session_id

    session_task.cancel()
    try:
        await session_task
    except asyncio.CancelledError:
        pass
