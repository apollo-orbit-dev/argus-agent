"""Auto-title: give a fresh session a real name (background aux call) once it has a first
completed turn, instead of leaving the raw session id as its display name.

THE acceptance requirement: if no chat model is configured at all, this must silently skip —
never raise, never delay the turn, never touch the id-name. That's test_no_model_configured_*
below. Every other failure mode (aux call raises/times out, empty/whitespace response) must also
be non-fatal: leave the id-name in place. And like every other non-main-loop model call in this
codebase, it MUST pass think=False (a reasoning model returns empty content with thinking on).
"""
import asyncio

from config import Config
from engine.engine import Engine
from engine.protocol import ModelResponse


def _engine(tmp_path, **cfg_kwargs):
    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="", **cfg_kwargs)
    return Engine(cfg, data_dir=str(tmp_path))


class _FakeAux:
    """Fake aux model client — records every call so tests can assert think=False, and can be
    made to raise (simulating a timeout/network failure) instead of returning a response."""
    def __init__(self, content=None, exc=None):
        self.content = content
        self.exc = exc
        self.calls = []

    async def chat(self, messages, tools=None, max_tokens=None, temperature=None,
                   think=None, reasoning=None):
        self.calls.append({"think": think, "max_tokens": max_tokens, "messages": messages})
        if self.exc is not None:
            raise self.exc
        return ModelResponse(content=self.content, finish_reason="stop")


def _seed_first_turn(e, sid):
    """Simulate what run_task leaves behind after a first completed turn: a couple of messages
    and a persisted row (name defaults to the id, same as create_session/_persist)."""
    e.store.append_message(sid, {"role": "user", "content": "Help me plan a trip to Japan"})
    e.store.append_message(sid, {"role": "assistant", "content": "Sure — when are you thinking of going?"})


async def test_no_model_configured_skips_silently(tmp_path):
    # THE maintainer's explicit requirement: no chat model at all -> silent no-op, never raise.
    cfg = Config(model_base_url="", model_name="", telegram_bot_token="")
    e = Engine(cfg, data_dir=str(tmp_path))
    sid = e.create_session()
    _seed_first_turn(e, sid)
    seen = []
    e.events.add_sink(seen.append)
    built = {"n": 0}

    def _boom():
        built["n"] += 1
        return _FakeAux(content="Should never be reached")
    e._aux_model_client = _boom

    title = await e.auto_title_session(sid)
    await asyncio.sleep(0)                           # let any fire-and-forget publish task run

    assert title is None
    assert built["n"] == 0                          # the aux client is never even constructed
    assert e.store.session_name(sid) == sid          # id-name preserved
    assert seen == []                                # no write -> no session_changed event


async def test_no_model_configured_via_missing_name_only(tmp_path):
    # blank model_name alone (base_url set) is also "no model configured" — same guard covers it.
    cfg = Config(model_base_url="http://x/v1", model_name="", telegram_bot_token="")
    e = Engine(cfg, data_dir=str(tmp_path))
    sid = e.create_session()
    _seed_first_turn(e, sid)
    seen = []
    e.events.add_sink(seen.append)
    built = {"n": 0}

    def _boom():
        built["n"] += 1
        return _FakeAux(content="Should never be reached")
    e._aux_model_client = _boom

    title = await e.auto_title_session(sid)
    await asyncio.sleep(0)      # let any fire-and-forget publish task run

    assert title is None
    # without this, a real ModelClient would be built and fired at http://x/v1 — a live HTTP
    # request in CI — and the guard being removed would still coincidentally pass via the
    # resulting connection error being caught, same as its sibling test above.
    assert built["n"] == 0
    assert e.store.session_name(sid) == sid
    assert seen == []


async def test_normal_case_titles_session(tmp_path):
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    fake = _FakeAux(content="Planning a Japan Trip")
    e._aux_model_client = lambda: fake

    title = await e.auto_title_session(sid)

    assert title == "Planning a Japan Trip"
    assert e.store.session_name(sid) == "Planning a Japan Trip"
    assert len(fake.calls) == 1


async def test_normal_case_emits_exactly_one_session_changed_event(tmp_path):
    # The sidebar-refresh event (argus-3fa): a successful auto-title fires exactly one
    # session_changed event on the "__control__" pseudo-session, not on the renamed session's own
    # stream (per-session SSE scoping would otherwise miss out-of-band renames from another tab).
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    e._aux_model_client = lambda: _FakeAux(content="Planning a Japan Trip")
    seen = []
    e.events.add_sink(seen.append)

    title = await e.auto_title_session(sid)
    await asyncio.sleep(0)      # let the fire-and-forget publish task (create_task, not awaited) run

    assert title == "Planning a Japan Trip"
    assert len(seen) == 1
    ev = seen[0]
    assert ev.session_id == "__control__"
    assert ev.kind == "session_changed"
    assert ev.run_id == "control" and ev.step == 0
    assert ev.data == {"session_id": sid, "action": "renamed", "name": "Planning a Japan Trip"}


async def test_title_is_sanitized_quotes_and_length(tmp_path):
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    e._aux_model_client = lambda: _FakeAux(content='  "Planning a  \n Japan Trip"  ')

    title = await e.auto_title_session(sid)

    assert title == "Planning a Japan Trip"          # quotes stripped, whitespace collapsed


async def test_title_is_capped_at_max_length(tmp_path):
    from engine.engine import _TITLE_MAX_LEN

    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    long_title = "Word" * 30                          # 120 chars, well past the 60-char cap
    assert len(long_title) > _TITLE_MAX_LEN
    e._aux_model_client = lambda: _FakeAux(content=long_title)

    title = await e.auto_title_session(sid)

    assert len(title) == _TITLE_MAX_LEN
    assert title == long_title[:_TITLE_MAX_LEN]
    assert e.store.session_name(sid) == title


async def test_already_renamed_session_is_not_overwritten(tmp_path):
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    seen = []
    e.events.add_sink(seen.append)
    e.rename_session(sid, "My custom title")
    await asyncio.sleep(0)         # let the manual rename's fire-and-forget publish task actually run
    assert len(seen) == 1          # sanity: the manual rename itself does emit
    seen.clear()                                     # only care about events from auto_title_session below
    built = {"n": 0}

    def _boom():
        built["n"] += 1
        return _FakeAux(content="Some Generated Title")
    e._aux_model_client = _boom

    title = await e.auto_title_session(sid)
    await asyncio.sleep(0)      # let any fire-and-forget publish task run before asserting its absence

    assert title is None
    assert built["n"] == 0                           # never even calls the model
    assert e.store.session_name(sid) == "My custom title"
    assert seen == []                                 # no session_changed event — no write happened


async def test_manual_rename_during_aux_call_wins_the_race(tmp_path):
    # The TOCTOU this fix closes: auto_title_session reads the placeholder name (still in place),
    # then awaits the aux call — up to request_timeout (engine/model_client.py) later, a manual
    # rename can land on this exact session in the meantime. The generated title must NOT clobber
    # it. Simulated here by having the fake aux client itself perform the "concurrent" rename
    # WHILE the coroutine under test is suspended awaiting `chat(...)` — i.e. genuinely between the
    # pre-call read (already done) and the post-call write (about to happen), not just before/after
    # the whole call.
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    assert e.store.session_name(sid) == sid              # placeholder still in place at read time
    seen = []
    e.events.add_sink(seen.append)

    class _RacingAux:
        async def chat(self, messages, tools=None, max_tokens=None, temperature=None,
                       think=None, reasoning=None):
            e.rename_session(sid, "Tax stuff")            # the user's manual rename, mid-flight
            return ModelResponse(content="Generated Title", finish_reason="stop")

    e._aux_model_client = lambda: _RacingAux()

    title = await e.auto_title_session(sid)
    await asyncio.sleep(0)      # let the fire-and-forget publish task (create_task, not awaited) run

    assert title is None                     # generated title was discarded, not applied
    assert e.store.session_name(sid) == "Tax stuff"       # the manual rename survives
    # Exactly ONE session_changed event — from the manual rename (Engine.rename_session) — and NONE
    # from auto_title_session's discarded write. If the emit were placed before the rename_if_placeholder
    # rowcount check instead of after, this would see a second event advertising "Generated Title".
    assert len(seen) == 1
    assert seen[0].data == {"session_id": sid, "action": "renamed", "name": "Tax stuff"}


async def test_aux_call_raising_is_nonfatal(tmp_path):
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    seen = []
    e.events.add_sink(seen.append)
    e._aux_model_client = lambda: _FakeAux(exc=TimeoutError("boom"))

    title = await e.auto_title_session(sid)          # must not raise
    await asyncio.sleep(0)      # let any fire-and-forget publish task run

    assert title is None
    assert e.store.session_name(sid) == sid
    assert seen == []


async def test_empty_response_is_not_stored_as_title(tmp_path):
    # blank/whitespace-only content is the think=False failure signature (a reasoning model that
    # still burned its budget on hidden reasoning) — must be treated as "no usable title".
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    seen = []
    e.events.add_sink(seen.append)
    e._aux_model_client = lambda: _FakeAux(content="   \n\t  ")

    title = await e.auto_title_session(sid)
    await asyncio.sleep(0)      # let any fire-and-forget publish task run

    assert title is None
    assert e.store.session_name(sid) == sid
    assert seen == []


async def test_passes_think_false(tmp_path):
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    fake = _FakeAux(content="A Short Title")
    e._aux_model_client = lambda: fake

    await e.auto_title_session(sid)

    assert fake.calls and fake.calls[0]["think"] is False


async def test_no_history_yet_skips_without_calling_model(tmp_path):
    # a session with no messages (edge case — shouldn't normally happen post-first-turn) has
    # nothing to summarize into a title; must not call the model or rename.
    e = _engine(tmp_path)
    sid = e.create_session()
    built = {"n": 0}

    def _boom():
        built["n"] += 1
        return _FakeAux(content="Title")
    e._aux_model_client = _boom

    title = await e.auto_title_session(sid)

    assert title is None
    assert built["n"] == 0
    assert e.store.session_name(sid) == sid


async def test_telegram_session_not_titled(tmp_path):
    # Telegram sessions use the bare chat id as session_id (backend/telegram_bot.py's
    # `str(chat_id)`) and are permanent, one-per-chat. _persist gives them name == id, same
    # placeholder shape as a fresh dashboard session — so without the "ses_" prefix gate this would
    # get auto-titled too, permanently hiding the chat id from the dashboard sidebar (which renders
    # only the name; see dashboard/app.js). Unlike an ephemeral id, this IS a real persisted row, so
    # this test is non-vacuous: removing the "ses_" check would make session_name read back the
    # placeholder, the model would actually get called, and the row would actually get renamed.
    e = _engine(tmp_path)
    sid = "987654321"                                  # telegram-shaped: bare numeric chat id
    _seed_first_turn(e, sid)
    assert e.store.session_name(sid) == sid             # placeholder precondition really holds
    seen = []
    e.events.add_sink(seen.append)
    built = {"n": 0}

    def _boom():
        built["n"] += 1
        return _FakeAux(content="Should never be reached")
    e._aux_model_client = _boom

    title = await e.auto_title_session(sid)
    await asyncio.sleep(0)      # let any fire-and-forget publish task run

    assert title is None
    assert built["n"] == 0                              # the "ses_" gate blocks before any model call
    assert e.store.session_name(sid) == sid
    assert seen == []


async def test_ephemeral_session_skipped(tmp_path):
    # Ephemeral ids ("__routine__:...") are scratch: SessionStore never persists a row for them
    # (append_message's _persist/_log no-op for ids starting with '__', see engine/state.py), so
    # session_name() returns None here and auto_title_session skips via the `current is None`
    # path — the same outcome the "ses_" prefix gate above produces (an ephemeral id never starts
    # with "ses_" either). There is deliberately no dedicated ephemeral guard in auto_title_session
    # itself: one would be unreachable in practice, since state.py's own ephemeral protections
    # already make session_name()/conversation() return None/[] no matter what is checked here.
    e = _engine(tmp_path)
    sid = "__routine__:x"
    e.store.append_message(sid, {"role": "user", "content": "hi"})
    seen = []
    e.events.add_sink(seen.append)
    built = {"n": 0}

    def _boom():
        built["n"] += 1
        return _FakeAux(content="Title")
    e._aux_model_client = _boom

    title = await e.auto_title_session(sid)
    await asyncio.sleep(0)      # let any fire-and-forget publish task run

    assert title is None
    assert built["n"] == 0                              # ephemeral sessions never reach the model
    assert seen == []


async def test_enable_auto_title_session_false_skips_the_feature(tmp_path):
    # The feature is opt-out via config; run_task's tail must not even schedule the background
    # task when it's off — checked at the run_task call site (`if c.enable_auto_title_session:`),
    # not inside auto_title_session itself.
    import asyncio

    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="",
                enable_auto_title_session=False)
    e = Engine(cfg, data_dir=str(tmp_path))
    e._model_client = lambda: _FakeAux(content="ok")

    called = {"n": 0}

    async def _spy(sid):
        called["n"] += 1
        return None
    e.auto_title_session = _spy
    seen = []
    e.events.add_sink(seen.append)

    sid = e.create_session()
    await e.run_task(sid, "hello there")
    await asyncio.sleep(0)          # let any scheduled background task actually run

    assert called["n"] == 0
    assert not any(ev.kind == "session_changed" for ev in seen)


async def test_aux_model_configured_via_utility_role(tmp_path):
    # _aux_model_configured has two branches: the fallback to the main chat model (covered by every
    # other test here) and the `utility` role mapping, which this exercises directly — a connection
    # mapped to `utility` with blank fields must still read as "not configured" (silent skip), and a
    # fully-populated one as "configured".
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)

    e.model_presets_store.get_role = lambda role: "my-util" if role == "utility" else None
    e.model_presets_store.resolve = lambda label: {"base_url": "", "model_name": ""}
    assert e._aux_model_configured() is False
    built = {"n": 0}

    def _boom():
        built["n"] += 1
        return _FakeAux(content="unused")
    e._aux_model_client = _boom
    title = await e.auto_title_session(sid)
    assert title is None
    assert built["n"] == 0

    e.model_presets_store.resolve = lambda label: {"base_url": "http://util/v1", "model_name": "u"}
    assert e._aux_model_configured() is True
