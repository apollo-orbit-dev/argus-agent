"""Engine wiring for per-connection request options.

The chat client is built from CONFIG (the chat role is projected there on switch), but the
per-connection request options are deliberately NOT projected — projecting them would persist one
model's quirks into the global .env, where they would then apply to every other connection. So the
engine resolves the relevant connection at client-build time and layers its overrides. These tests
pin that precedence, and the back-compat asymmetry that only the CHAT client inherits the global
config.model_* sampling tier.
"""
from config import Config
from engine.engine import Engine
from engine.model_presets import ModelPresetStore


def _engine(tmp_path, **cfg):
    base = dict(model_base_url="http://vllm/v1", model_name="main", telegram_bot_token="",
                memory_scope="session")
    base.update(cfg)
    e = Engine(Config(**base))
    e.model_presets_store = ModelPresetStore(str(tmp_path / "mp.json"))   # isolate from repo file
    e._env_path = tmp_path / ".env"
    return e


def _chat_conn(e, **opts):
    e.model_presets_store.add("chat-conn", "http://vllm/v1", "main", "vllm", **opts)
    e.model_presets_store.set_role("chat", "chat-conn")


# ---- 18/19/20: sampling precedence ----

def test_connection_sampling_beats_global(tmp_path):
    """18. connection sampling.<k> wins over config.model_<k>."""
    e = _engine(tmp_path, model_temperature=0.2, model_top_p=0.5, model_top_k=100,
                model_presence_penalty=1.5)
    _chat_conn(e, sampling={"temperature": 0.9, "top_k": 20})
    mc = e._model_client()
    assert mc.temperature == 0.9
    assert mc.top_k == 20
    assert mc.top_p == 0.5              # not overridden -> global still applies
    assert mc.presence_penalty == 1.5


def test_falls_back_to_global_when_no_overrides(tmp_path):
    """19. An empty sampling object changes nothing — today's behaviour exactly."""
    e = _engine(tmp_path, model_temperature=0.2, model_top_p=0.5)
    _chat_conn(e, sampling={})
    mc = e._model_client()
    assert mc.temperature == 0.2 and mc.top_p == 0.5
    assert mc.top_k is None and mc.presence_penalty is None
    assert mc.extra_body == {} and mc.reasoning_style == "auto"
    # …and with no chat connection resolvable at all (a fresh install), likewise
    e.model_presets_store.set_role("chat", None)
    assert e._model_client().temperature == 0.2


def test_sampling_zero_is_a_real_override(tmp_path):
    """20. The presence contract: a key PRESENT with 0.0 is an override, not 'unset'. This is why
    sampling is a nested object rather than four nullable columns."""
    e = _engine(tmp_path, model_temperature=0.7)
    _chat_conn(e, sampling={"temperature": 0.0})
    assert e._model_client().temperature == 0.0


def test_chat_client_gets_extra_body_and_style(tmp_path):
    e = _engine(tmp_path)
    _chat_conn(e, extra_body={"thinking_budget": 4096}, reasoning_style="thinking_type")
    mc = e._model_client()
    assert mc.extra_body == {"thinking_budget": 4096}
    assert mc.reasoning_style == "thinking_type"
    # identity still comes from config — the options do NOT leak into it
    assert mc.base_url == "http://vllm/v1" and mc.model == "main"
    env_fields = e.config._ENV_FIELDS
    for field in ("sampling", "extra_body", "reasoning_style"):
        assert not hasattr(e.config, field)
        assert field not in env_fields
        assert field.upper() not in e.config.env_pairs()


def test_projecting_a_role_does_not_persist_request_options(tmp_path):
    """The three fields must never reach .env — they are per-connection, and a global copy would
    apply to every OTHER connection that hasn't overridden them."""
    e = _engine(tmp_path)
    e.model_presets_store.add("opt", "http://other/v1", "m2", "vllm",
                              sampling={"temperature": 0.9}, extra_body={"x": 1},
                              reasoning_style="prompt_tag")
    e.set_role("chat", "opt", persist=True)
    written = (tmp_path / ".env").read_text() if (tmp_path / ".env").exists() else ""
    for token in ("EXTRA_BODY", "REASONING_STYLE", "SAMPLING"):
        assert token not in written
    assert e.config.model_temperature is None        # sampling did NOT become a global default


# ---- 21/22: the aux asymmetry ----

def test_aux_client_does_not_inherit_global_sampling(tmp_path):
    """21. Aux/probe/caption calls send NO sampling today. Inheriting the global tier there would
    start sending a temperature on calls that have never carried one — a live behaviour change."""
    e = _engine(tmp_path, model_temperature=0.2, model_top_p=0.5, model_top_k=100,
                model_presence_penalty=1.5)
    e.model_presets_store.add("cheap", "http://cheap/v1", "small", "vllm")
    e.model_presets_store.set_role("utility", "cheap")
    aux = e._aux_model_client()
    assert aux.model == "small"
    assert aux.temperature is None and aux.top_p is None
    assert aux.top_k is None and aux.presence_penalty is None
    # its OWN sampling still applies, though
    e.model_presets_store.add("cheap", "http://cheap/v1", "small", "vllm",
                              sampling={"temperature": 0.4})
    assert e._aux_model_client().temperature == 0.4
    assert e._aux_model_client().top_p is None       # still no global leak


def test_aux_uses_utility_connections_reasoning_style(tmp_path):
    """22. The aux/chat reasoning asymmetry becomes a knob: the utility connection can be silenced
    independently of the chat model."""
    e = _engine(tmp_path)
    _chat_conn(e, reasoning_style="enable_thinking")
    e.model_presets_store.add("cheap", "http://cheap/v1", "small", "vllm",
                              reasoning_style="none", extra_body={"foo": 1})
    e.model_presets_store.set_role("utility", "cheap")
    aux = e._aux_model_client()
    assert aux.reasoning_style == "none"
    assert aux.extra_body == {"foo": 1}
    assert aux._reasoning_params("off") == {}                    # aux think=False now sends nothing
    assert e._model_client().reasoning_style == "enable_thinking"   # chat unaffected


def test_conn_client_kwargs_and_test_preset_use_the_helper(tmp_path):
    """`_conn_client_kwargs` — the helper `test_preset`/`_caption_image`/`_aux_model_client` all
    build their clients from — resolves a connection's own options with no global sampling tier,
    same as aux. `test_preset` itself is exercised just enough to prove it doesn't explode."""
    import asyncio

    e = _engine(tmp_path, model_temperature=0.2)
    e.model_presets_store.add("vis", "http://vis/v1", "cap-model", "vllm",
                              sampling={"top_p": 0.3}, reasoning_style="none",
                              extra_body={"z": 1}, capabilities=["vision"])
    conn = e.model_presets_store.resolve("vis")
    kw = e._conn_client_kwargs(conn)
    assert kw == {"top_p": 0.3, "extra_body": {"z": 1}, "reasoning_style": "none"}
    assert "temperature" not in kw                    # no global tier off the chat client
    # test_preset builds a client from the same helper; just prove it doesn't explode
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        res = loop.run_until_complete(e.test_preset("nope-does-not-exist"))
    finally:
        loop.close()
    assert res["ok"] is False


def test_helper_tolerates_garbage_stored_values(tmp_path):
    """A hand-edited connections file must degrade, never raise — the engine builds a client on
    every turn."""
    e = _engine(tmp_path, model_temperature=0.2)
    for junk in (None, {}, {"sampling": "nope", "extra_body": [], "reasoning_style": 7},
                 {"sampling": {"temperature": None}, "extra_body": {}, "reasoning_style": "  "},
                 {"sampling": {"top_k": "20"}}):
        kw = e._conn_client_kwargs(junk)
        assert "extra_body" not in kw and "reasoning_style" not in kw
        assert e._conn_client_kwargs(junk, global_sampling=True)["temperature"] == 0.2
    # a numeric STRING coerces cleanly (ModelClient wants an int, not "20")
    assert e._conn_client_kwargs({"sampling": {"top_k": "20"}}) == {"top_k": 20}
    # a non-numeric string can't be cast — dropped, not propagated to crash ModelClient later
    assert e._conn_client_kwargs({"sampling": {"top_k": "nope"}}) == {}


def _run_chat(client, handler):
    """Drive one client.chat() call through a mock transport, closing the loop it borrows."""
    import asyncio
    import unittest.mock as mock

    import httpx

    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *a, **kw):
        real_init(self, *a, **{**kw, "transport": httpx.MockTransport(handler)})

    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        with mock.patch.object(httpx.AsyncClient, "__init__", fake_init):
            return loop.run_until_complete(client.chat([{"role": "user", "content": "hi"}]))
    finally:
        loop.close()


def _ok_handler(seen=None):
    import json as _j

    import httpx

    def handler(req):
        if seen is not None:
            seen["body"] = _j.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
                                                       "finish_reason": "stop"}], "usage": {}})
    return handler


def test_stored_garbage_top_k_does_not_crash_a_turn(tmp_path):
    """FIX for the crash a hand-edited connections.json used to cause EVERY turn: `sampling.top_k`
    stored as the string "20" reached ModelClient unvalidated and blew up at
    `self.top_k > 0` (str > int -> TypeError), which is not a ModelError, so the loop's
    `except ModelError` never caught it. `_conn_client_kwargs` must now coerce/drop it instead."""
    from engine.model_client import ModelClient

    e = _engine(tmp_path)
    e.model_presets_store.add("chat-conn", "http://vllm/v1", "main", "vllm",
                              sampling={"top_k": "20"})
    e.model_presets_store.set_role("chat", "chat-conn")
    mc = e._model_client()
    assert mc.top_k == 20 and isinstance(mc.top_k, int)   # coerced, not the raw string

    seen = {}
    _run_chat(mc, _ok_handler(seen))                       # must not raise
    assert seen["body"]["top_k"] == 20

    # Belt-and-braces: even if a bad-typed top_k somehow reached ModelClient directly (bypassing
    # the engine helper entirely — e.g. a caller that builds one by hand), the client itself must
    # still not crash.
    bad = ModelClient("http://vllm/v1", "main", top_k="20", provider="vllm")
    _run_chat(bad, _ok_handler())                           # must not raise
