"""Auto-title: give a fresh session a real name (background aux call) once it has a first
completed turn, instead of leaving the raw session id as its display name.

THE acceptance requirement: if no chat model is configured at all, this must silently skip —
never raise, never delay the turn, never touch the id-name. That's test_no_model_configured_*
below. Every other failure mode (aux call raises/times out, empty/whitespace response) must also
be non-fatal: leave the id-name in place. And like every other non-main-loop model call in this
codebase, it MUST pass think=False (a reasoning model returns empty content with thinking on).
"""
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
    built = {"n": 0}

    def _boom():
        built["n"] += 1
        return _FakeAux(content="Should never be reached")
    e._aux_model_client = _boom

    title = await e.auto_title_session(sid)

    assert title is None
    assert built["n"] == 0                          # the aux client is never even constructed
    assert e.store.session_name(sid) == sid          # id-name preserved


async def test_no_model_configured_via_missing_name_only(tmp_path):
    # blank model_name alone (base_url set) is also "no model configured" — same guard covers it.
    cfg = Config(model_base_url="http://x/v1", model_name="", telegram_bot_token="")
    e = Engine(cfg, data_dir=str(tmp_path))
    sid = e.create_session()
    _seed_first_turn(e, sid)
    title = await e.auto_title_session(sid)
    assert title is None
    assert e.store.session_name(sid) == sid


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


async def test_title_is_sanitized_quotes_and_length(tmp_path):
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    e._aux_model_client = lambda: _FakeAux(content='  "Planning a  \n Japan Trip"  ')

    title = await e.auto_title_session(sid)

    assert title == "Planning a Japan Trip"          # quotes stripped, whitespace collapsed


async def test_already_renamed_session_is_not_overwritten(tmp_path):
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    e.rename_session(sid, "My custom title")
    built = {"n": 0}

    def _boom():
        built["n"] += 1
        return _FakeAux(content="Some Generated Title")
    e._aux_model_client = _boom

    title = await e.auto_title_session(sid)

    assert title is None
    assert built["n"] == 0                           # never even calls the model
    assert e.store.session_name(sid) == "My custom title"


async def test_aux_call_raising_is_nonfatal(tmp_path):
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    e._aux_model_client = lambda: _FakeAux(exc=TimeoutError("boom"))

    title = await e.auto_title_session(sid)          # must not raise

    assert title is None
    assert e.store.session_name(sid) == sid


async def test_empty_response_is_not_stored_as_title(tmp_path):
    # blank/whitespace-only content is the think=False failure signature (a reasoning model that
    # still burned its budget on hidden reasoning) — must be treated as "no usable title".
    e = _engine(tmp_path)
    sid = e.create_session()
    _seed_first_turn(e, sid)
    e._aux_model_client = lambda: _FakeAux(content="   \n\t  ")

    title = await e.auto_title_session(sid)

    assert title is None
    assert e.store.session_name(sid) == sid


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


async def test_ephemeral_session_skipped(tmp_path):
    e = _engine(tmp_path)
    sid = "__routine__:x"
    e.store.append_message(sid, {"role": "user", "content": "hi"})
    e._aux_model_client = lambda: _FakeAux(content="Title")

    title = await e.auto_title_session(sid)

    assert title is None                              # ephemeral sessions have no persisted name
