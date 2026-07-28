import asyncio

from engine.rules.detect import has_rule_cue
from engine.protocol import ModelResponse
from engine.engine import Engine
from config import Config


def test_cue_detection():
    assert has_rule_cue("Don't do that again")
    assert has_rule_cue("Always confirm before deleting")
    assert has_rule_cue("From now on, use metric units")
    assert has_rule_cue("never use emoji")
    assert not has_rule_cue("What's the weather in London?")
    assert not has_rule_cue("Thanks, that looks great")


class _FakeAux:
    def __init__(self, content):
        self.content = content
        self.calls = 0

    async def chat(self, messages, tools=None, max_tokens=None,
                   temperature=None, think=None, reasoning=None):
        self.calls += 1
        assert think is False          # aux calls MUST disable the reasoning pass
        return ModelResponse(content=self.content, finish_reason="stop")


async def test_autodetect_saves_rule(tmp_path):
    e = Engine(Config(), data_dir=str(tmp_path))
    fake = _FakeAux("Never use emoji")
    e._aux_model_client = lambda: fake
    saved = await e.autodetect_rule("sess", "stop using emoji, don't do it again")
    assert [r["text"] for r in saved] == ["Never use emoji"]
    assert [r["source"] for r in e.rules_list()] == ["auto"]
    assert fake.calls == 1


async def test_autodetect_saves_nothing_on_none(tmp_path):
    e = Engine(Config(), data_dir=str(tmp_path))
    e._aux_model_client = lambda: _FakeAux("NONE")
    saved = await e.autodetect_rule("sess", "don't worry about it")
    assert saved == [] and e.rules_list() == []


# ---- rule_saved must never look like a run (see Engine._notify_rule_saved) ----
# The notification used to emit with the synthetic run_id "autodetect" on the affected session's
# OWN stream. dashboard/app.js's processEvent registers any unseen run_id as a new run, so an
# auto-detected rule added a phantom row to the Runs list that never gets a `final` and never
# completes (observed live: a "autodete" run row stuck at dot-error). It now goes out on the
# reserved __control__ pseudo-session instead.

async def _drain(e):
    """Let the fire-and-forget control emit (create_task) actually run."""
    await asyncio.sleep(0)
    if e._bg_tasks:
        await asyncio.gather(*list(e._bg_tasks), return_exceptions=True)


async def test_rule_saved_emits_on_control_channel_not_the_session_stream(tmp_path):
    e = Engine(Config(), data_dir=str(tmp_path))
    seen = []
    e.events.add_sink(seen.append)
    await e._notify_rule_saved("sess", [{"id": 1, "text": "Never use emoji"}])
    await _drain(e)
    assert [ev.session_id for ev in seen] == ["__control__"]
    ev = seen[0]
    assert ev.kind == "rule_saved" and ev.run_id == "control" and ev.step == 0
    assert ev.data["session_id"] == "sess"                       # which session it came from
    assert [r["text"] for r in ev.data["rules"]] == ["Never use emoji"]


async def test_autodetect_puts_no_synthetic_run_on_the_session_replay_buffer(tmp_path):
    """End to end: nothing reaches the stream the dashboard subscribes to for a session, so no
    phantom run can be registered from it. (recent() is exactly what /events replays on connect.)"""
    e = Engine(Config(), data_dir=str(tmp_path))
    e._aux_model_client = lambda: _FakeAux("Never use emoji")
    saved = await e.autodetect_rule("sess", "stop using emoji, don't do it again")
    assert saved
    await _drain(e)
    assert e.events.recent("sess") == []
    control = e.events.recent("__control__")
    assert [ev.kind for ev in control] == ["rule_saved"]
    assert {ev.run_id for ev in control} == {"control"}


async def test_rule_saved_survives_a_publish_failure(tmp_path):
    """Best-effort: a dead event bus must not raise into the caller, and must not swallow the
    owner-facing push that follows it."""
    e = Engine(Config(), data_dir=str(tmp_path))
    attempted = []

    def boom(ev):
        attempted.append(ev)
        raise RuntimeError("bus down")

    e.events.publish = boom
    delivered = []

    async def deliver(sid, msg):
        delivered.append((sid, msg))

    e.scheduler.deliver = deliver
    await e._notify_rule_saved("sess", [{"id": 1, "text": "Never use emoji"}])   # must not raise
    assert attempted and all(ev.session_id == "__control__" for ev in attempted)
    assert delivered and delivered[0][0] == "sess"
    assert "Never use emoji" in delivered[0][1]
