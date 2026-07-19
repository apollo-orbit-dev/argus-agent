import asyncio
import time
from config import Config
from engine.engine import Engine


def _engine(tmp_path, enabled=True):
    return Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="",
                         enable_reliability=enabled), data_dir=str(tmp_path))


def test_engine_records_tool_outcomes_via_sink(tmp_path):
    e = _engine(tmp_path)
    from engine.events import StepEvent
    # Real (current) timestamps, not the epoch — ReliabilityStore.summary() buckets by calendar day
    # off time.time(), so an event stamped near 1970 would fall outside ANY "last N days" window
    # and never be counted regardless of correctness.
    now = time.time()
    asyncio.run(e.events.publish(StepEvent("r", "s", 1, "tool_call", {"tool": "weather"}, now)))
    asyncio.run(e.events.publish(StepEvent("r", "s", 1, "tool_result", {"tool": "weather", "ok": True}, now + 0.2)))
    out = e.reliability_summary(days=30)
    assert out["enabled"] and out["tool_calls"] == 1 and out["tool_success_pct"] == 100.0


def test_disabled_reliability_returns_disabled(tmp_path):
    e = _engine(tmp_path, enabled=False)
    assert e.reliability_summary()["enabled"] is False
    assert e._reliability is None
