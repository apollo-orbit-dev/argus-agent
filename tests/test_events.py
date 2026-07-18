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
