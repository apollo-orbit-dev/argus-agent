import copy

import httpx
import pytest

from engine.model_client import ModelClient, ModelError


def client_with(handler):
    c = ModelClient("http://x/v1", "main", timeout=5)
    # monkeypatch the AsyncClient factory via httpx MockTransport
    c._transport = httpx.MockTransport(handler)  # type: ignore[attr-defined]
    return c


@pytest.fixture(autouse=True)
def patch_asyncclient(monkeypatch):
    """Route ModelClient's httpx.AsyncClient through a MockTransport we set per-test."""
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *a, **kw):
        kw["transport"] = patch_asyncclient.transport
        real_init(self, *a, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    yield


async def test_content_response():
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "hi there"}, "finish_reason": "stop"}],
            "usage": {"completion_tokens": 3},
        })
    patch_asyncclient.transport = httpx.MockTransport(handler)
    r = await ModelClient("http://x/v1", "main").chat([{"role": "user", "content": "hi"}])
    assert r.content == "hi there" and r.finish_reason == "stop"
    assert r.usage["completion_tokens"] == 3


async def test_tool_calls_response():
    def handler(req):
        return httpx.Response(200, json={
            "choices": [{"message": {"content": None, "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "calculator", "arguments": "{\"expression\": \"1+1\"}"}}]},
                "finish_reason": "tool_calls"}],
        })
    patch_asyncclient.transport = httpx.MockTransport(handler)
    r = await ModelClient("http://x/v1", "main").chat(
        [{"role": "user", "content": "calc"}], tools=[{"type": "function", "function": {"name": "calculator"}}])
    assert r.tool_calls[0]["function"]["name"] == "calculator"
    assert r.finish_reason == "tool_calls"


async def test_non_200_raises():
    def handler(req):
        return httpx.Response(500, text="boom")
    patch_asyncclient.transport = httpx.MockTransport(handler)
    with pytest.raises(ModelError):
        await ModelClient("http://x/v1", "main").chat([{"role": "user", "content": "x"}])


async def test_sends_tools_only_when_provided():
    seen = {}

    def handler(req):
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    await ModelClient("http://x/v1", "main").chat([{"role": "user", "content": "x"}])
    assert "tools" not in seen
    assert seen["max_tokens"] == 1536  # generous default applied


async def test_sends_sampling_params():
    """Agents-A1's recommended sampling must reach vLLM (temperature=0 greedy loops)."""
    import json
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    c = ModelClient("http://x/v1", "main", temperature=0.85, top_p=0.95, top_k=20,
                    presence_penalty=1.1)
    await c.chat([{"role": "user", "content": "x"}])
    assert seen["temperature"] == 0.85
    assert seen["top_p"] == 0.95
    assert seen["top_k"] == 20
    assert seen["presence_penalty"] == 1.1


async def test_no_sampling_params_by_default():
    """Unset sampling params must be OMITTED so the model server applies its own defaults —
    Argus must not bake in a hard-coded copy of the model's tuning."""
    import json
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    await ModelClient("http://x/v1", "main").chat([{"role": "user", "content": "x"}])
    for k in ("temperature", "top_p", "top_k", "presence_penalty"):
        assert k not in seen                      # nothing sent unless explicitly configured
    assert seen["max_tokens"] == 1536             # max_tokens is Argus-controlled, still sent


async def test_think_false_disables_reasoning():
    """think=False must send chat_template_kwargs so the reasoning model skips thinking."""
    import json
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    c = ModelClient("http://x/v1", "main")
    await c.chat([{"role": "user", "content": "x"}], think=False)
    assert seen["chat_template_kwargs"] == {"enable_thinking": False}
    # default / think=True must NOT send it (thinking stays on for the main loop)
    seen.clear()
    await c.chat([{"role": "user", "content": "x"}])
    assert "chat_template_kwargs" not in seen


async def test_openrouter_omits_vllm_only_params():
    # Pointed at OpenRouter: must NOT send chat_template_kwargs (think=False) or top_k, which
    # would break/confuse non-vLLM backends. Must send the X-Title attribution header.
    seen = {}
    def handler(req):
        import json as _j
        seen["body"] = _j.loads(req.content)
        seen["xtitle"] = req.headers.get("X-Title")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
                                                      "finish_reason": "stop"}], "usage": {}})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    c = ModelClient("https://openrouter.ai/api/v1", "anthropic/claude-sonnet-4.5",
                    top_k=40)   # top_k set, but must be dropped for OpenRouter
    assert c.provider == "openrouter"                 # auto-detected from URL
    await c.chat([{"role": "user", "content": "hi"}], think=False)
    assert "chat_template_kwargs" not in seen["body"]
    assert "top_k" not in seen["body"]
    assert seen["xtitle"] == "Argus"


async def test_vllm_still_sends_vllm_params():
    seen = {}
    def handler(req):
        import json as _j
        seen["body"] = _j.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
                                                      "finish_reason": "stop"}], "usage": {}})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    c = ModelClient("http://vllm.local/v1", "main", top_k=40)   # auto -> vllm
    assert c.provider == "vllm"
    await c.chat([{"role": "user", "content": "hi"}], think=False)
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    assert seen["body"]["top_k"] == 40


async def test_explicit_provider_overrides_autodetect():
    c = ModelClient("http://my-proxy.internal/v1", "some/model", provider="openrouter")
    assert c.provider == "openrouter"


async def test_token_count_estimates_for_non_vllm_without_network():
    # non-vLLM has no /tokenize; must estimate without any HTTP (handler would 500 if called)
    def handler(req):
        return httpx.Response(500)
    patch_asyncclient.transport = httpx.MockTransport(handler)
    c = ModelClient("https://openrouter.ai/api/v1", "openai/gpt-4o")
    n = await c.token_count("abcd" * 10)   # 40 chars -> ~10
    assert n == 10


def _capture_handler(seen):
    import json as _j
    def handler(req):
        seen["body"] = _j.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
                                                      "finish_reason": "stop"}], "usage": {}})
    return handler


def test_reasoning_translation_per_provider():
    vl = ModelClient("http://vllm.local/v1", "main")
    assert vl._reasoning_params("high") == {"chat_template_kwargs": {"enable_thinking": True}}
    assert vl._reasoning_params("off") == {"chat_template_kwargs": {"enable_thinking": False}}
    assert vl._reasoning_params("auto") == {}
    orr = ModelClient("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash")
    assert orr._reasoning_params("high") == {"reasoning": {"effort": "high"}}
    assert orr._reasoning_params("off") == {"reasoning": {"enabled": False}}
    assert orr._reasoning_params("auto") == {}
    oa = ModelClient("https://api.openai.com/v1", "o4-mini")
    assert oa._reasoning_params("medium") == {"reasoning_effort": "medium"}
    assert oa._reasoning_params("off") == {}
    # generic OpenAI-compatible: never translates reasoning (providers differ; an unknown param 400s)
    oc = ModelClient("https://api.fireworks.ai/inference/v1", "accounts/fireworks/models/x")
    assert oc._reasoning_params("high") == {}
    assert oc._reasoning_params("off") == {}


def test_auto_detects_openai_compatible_clouds():
    """Fireworks/Together/Groq/etc. speak plain OpenAI — auto must resolve them to the generic
    provider, not to 'vllm' (which would send vLLM-only params and probe /tokenize)."""
    for url in ("https://api.fireworks.ai/inference/v1", "https://api.together.xyz/v1",
                "https://api.groq.com/openai/v1", "https://api.deepinfra.com/v1/openai"):
        assert ModelClient(url, "m").provider == "openai-compatible", url
    # a bare local server is still assumed to be vLLM/Ollama
    assert ModelClient("http://vllm.local/v1", "m").provider == "vllm"


def test_openai_compatible_aliases_normalize():
    for alias in ("openai-compatible", "openai_compatible", "generic", "compatible"):
        assert ModelClient("http://my-proxy.internal/v1", "m", provider=alias).provider == "openai-compatible"


async def test_openai_compatible_sends_only_standard_params():
    """The generic provider must send NO vendor-specific params (chat_template_kwargs, top_k) and
    NO X-Title header, even with think=False and top_k set — only the plain OpenAI payload."""
    seen = {}
    def handler(req):
        import json as _j
        seen["body"] = _j.loads(req.content)
        seen["xtitle"] = req.headers.get("X-Title")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"},
                                                      "finish_reason": "stop"}], "usage": {}})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    c = ModelClient("https://api.fireworks.ai/inference/v1", "accounts/fireworks/models/x", top_k=40)
    assert c.provider == "openai-compatible"
    await c.chat([{"role": "user", "content": "hi"}], think=False)
    assert "chat_template_kwargs" not in seen["body"]
    assert "top_k" not in seen["body"]
    assert "reasoning_effort" not in seen["body"] and "reasoning" not in seen["body"]
    assert seen["xtitle"] is None


async def test_openai_compatible_token_count_estimates_without_tokenize():
    """Generic backends have no vLLM /tokenize; token_count must estimate with no HTTP call."""
    def handler(req):
        return httpx.Response(500)   # would fail the test if /tokenize were called
    patch_asyncclient.transport = httpx.MockTransport(handler)
    c = ModelClient("https://api.fireworks.ai/inference/v1", "accounts/fireworks/models/x")
    assert await c.token_count("abcd" * 10) == 10


async def test_configured_reasoning_sent_on_main_call():
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash", reasoning="high")
    await c.chat([{"role": "user", "content": "hi"}])   # think=None -> configured level
    assert seen["body"]["reasoning"] == {"effort": "high"}


async def test_aux_think_false_forces_reasoning_off():
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash", reasoning="high")
    await c.chat([{"role": "user", "content": "hi"}], think=False)   # aux -> OFF despite config
    assert seen["body"]["reasoning"] == {"enabled": False}


async def test_auto_reasoning_sends_nothing():
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    await ModelClient("http://vllm.local/v1", "main").chat([{"role": "user", "content": "hi"}])
    assert "reasoning" not in seen["body"] and "chat_template_kwargs" not in seen["body"]


async def test_explicit_reasoning_param_overrides_config():
    # the adaptive router passes a per-call level that beats the configured default and think
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash", reasoning="low")
    await c.chat([{"role": "user", "content": "hi"}], reasoning="high")
    assert seen["body"]["reasoning"] == {"effort": "high"}


async def test_per_call_reasoning_off_overrides():
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("https://openrouter.ai/api/v1", "deepseek/deepseek-v4-flash", reasoning="high")
    await c.chat([{"role": "user", "content": "hi"}], reasoning="off")
    assert seen["body"]["reasoning"] == {"enabled": False}


async def test_default_client_payload_unchanged():
    """REGRESSION GATE. A client built with no per-connection options must send EXACTLY the three
    keys it has always sent — no new field may leak a default onto the wire. If this fails, every
    existing deploy's requests just changed shape."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    await ModelClient("http://vllm.local/v1", "main").chat([{"role": "user", "content": "hi"}])
    assert set(seen["body"]) == {"model", "messages", "max_tokens"}


# ---- per-connection extra_body: a free-form blob merged verbatim, LAST ----

async def test_extra_body_merged_verbatim():
    """1. Whatever the vendor accepts, the user can send — no Argus release needed."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main",
                    extra_body={"chat_template_kwargs": {"thinking": True},
                                "guided_json": {"type": "object"}, "thinking_budget": 4096})
    await c.chat([{"role": "user", "content": "hi"}])
    assert seen["body"]["chat_template_kwargs"] == {"thinking": True}
    assert seen["body"]["guided_json"] == {"type": "object"}
    assert seen["body"]["thinking_budget"] == 4096


async def test_extra_body_overrides_sampling_and_reasoning():
    """2. extra_body is merged after sampling and after the reasoning translation, so it is the
    final word on any key it names."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main", temperature=0.7, top_p=0.95,
                    extra_body={"temperature": 0.2, "chat_template_kwargs": {"enable_thinking": True}})
    await c.chat([{"role": "user", "content": "hi"}], think=False)   # would send enable_thinking False
    assert seen["body"]["temperature"] == 0.2
    assert seen["body"]["top_p"] == 0.95                              # untouched keys survive
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": True}


async def test_extra_body_beats_per_call_temperature():
    """3. Locks the 'last word' rule. The main loop passes an explicit temperature on EVERY call,
    so letting per-call win would make an extra_body temperature permanently inert."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main", extra_body={"temperature": 0.9})
    await c.chat([{"role": "user", "content": "hi"}], temperature=0.0)
    assert seen["body"]["temperature"] == 0.9


async def test_extra_body_deep_merges_nested_dicts():
    """4. One-level deep merge: adding a sibling kwarg must not silently delete the enable_thinking
    the reasoning translation just set."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main",
                    extra_body={"chat_template_kwargs": {"foo": 1}})
    await c.chat([{"role": "user", "content": "hi"}], think=False)
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False, "foo": 1}
    # a non-dict collision still REPLACES outright
    seen.clear()
    c2 = ModelClient("http://vllm.local/v1", "main", max_tokens=99, extra_body={"max_tokens": 7})
    await c2.chat([{"role": "user", "content": "hi"}])
    assert seen["body"]["max_tokens"] == 7


async def test_extra_body_cannot_clobber_denylisted_keys():
    """5. Second line of defence: the store rejects these at write time, and the client strips them
    again at merge time so a hand-edited connections file can't hijack the request."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main",
                    extra_body={"messages": [{"role": "user", "content": "PWNED"}],
                                "model": "evil-model", "stream": True, "top_p": 0.5})
    await c.chat([{"role": "user", "content": "hi"}])
    assert seen["body"]["messages"] == [{"role": "user", "content": "hi"}]
    assert seen["body"]["model"] == "main"
    assert "stream" not in seen["body"]
    assert seen["body"]["top_p"] == 0.5              # non-denylisted keys still apply


async def test_extra_body_denylist_is_case_insensitive():
    """A hand-edited connections file could carry 'Model'/'Stream' — different JSON keys on the
    wire (case-sensitive), so not an actual override, but the merge must still strip them rather
    than send an unknown param and 400 confusingly."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main",
                    extra_body={"Model": "evil-model", "STREAM": True, " Messages ": "bogus",
                                "top_p": 0.5})
    await c.chat([{"role": "user", "content": "hi"}])
    assert seen["body"]["model"] == "main"
    assert "Model" not in seen["body"] and "STREAM" not in seen["body"]
    assert " Messages " not in seen["body"] and "Messages" not in seen["body"]
    assert seen["body"]["top_p"] == 0.5


async def test_extra_body_not_shared_between_requests():
    """The client keeps a private copy; mutating the caller's dict afterwards must not leak in."""
    src = {"chat_template_kwargs": {"thinking": True}}
    c = ModelClient("http://vllm.local/v1", "main", extra_body=src)
    src["chat_template_kwargs"]["thinking"] = False
    src["injected"] = 1
    assert c.extra_body == {"chat_template_kwargs": {"thinking": True}}


# ---- reasoning_style: pin the wire dialect when provider inference can't ----

@pytest.mark.parametrize("style,off,high", [
    ("none", {}, {}),
    ("enable_thinking",
     {"chat_template_kwargs": {"enable_thinking": False}},
     {"chat_template_kwargs": {"enable_thinking": True}}),
    ("openrouter", {"reasoning": {"enabled": False}}, {"reasoning": {"effort": "high"}}),
    ("openai_effort", {}, {"reasoning_effort": "high"}),
    ("thinking_type", {"thinking": {"type": "disabled"}}, {"thinking": {"type": "enabled"}}),
    ("prompt_tag", {}, {}),          # expressed in the messages, not the payload
])
def test_reasoning_style_dispatch(style, off, high):
    """6. The §4 table, asserted directly — and asserted against a base URL whose PROVIDER would
    infer something else, proving the style (not the URL) decides."""
    c = ModelClient("http://vllm.local/v1", "main", reasoning_style=style)
    assert c._reasoning_params("off") == off
    assert c._reasoning_params("high") == high
    assert c._reasoning_params("auto") == {}          # auto is silent for every style


async def test_reasoning_style_none_silences_aux_call():
    """7. The probe finding's fix: today an aux call (think=False) sends enable_thinking:false to
    any vLLM-detected model. A connection can now opt out of that entirely."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main", reasoning_style="none")
    await c.chat([{"role": "user", "content": "hi"}], think=False)
    assert "chat_template_kwargs" not in seen["body"]
    seen.clear()
    await c.chat([{"role": "user", "content": "hi"}], reasoning="high")
    assert set(seen["body"]) == {"model", "messages", "max_tokens"}
    seen.clear()
    await c.probe()                                   # the ping stops sending it too
    assert "chat_template_kwargs" not in seen["body"]


async def test_prompt_tag_appends_switch_without_mutating_caller():
    """8. Qwen3's soft switch — and the mutation guard. The loop reuses ONE message list across the
    steps of a turn, so an in-place append would accumulate '/no_think /no_think /no_think'."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main", reasoning_style="prompt_tag")
    msgs = [{"role": "system", "content": "be nice"},
            {"role": "user", "content": "what is 2+2"},
            {"role": "assistant", "content": "thinking"}]
    original = copy.deepcopy(msgs)
    await c.chat(msgs, think=False)
    assert seen["body"]["messages"][1]["content"] == "what is 2+2 /no_think"
    assert seen["body"]["messages"][0] == {"role": "system", "content": "be nice"}
    assert msgs == original                       # caller's list AND dicts untouched
    # a second step over the SAME list must not double-tag
    seen.clear()
    await c.chat(msgs, think=False)
    assert seen["body"]["messages"][1]["content"] == "what is 2+2 /no_think"
    # a thinking level tags /think instead; auto leaves the turn alone
    seen.clear()
    await c.chat(msgs, reasoning="high")
    assert seen["body"]["messages"][1]["content"] == "what is 2+2 /think"
    seen.clear()
    await c.chat(msgs)                            # configured default is "auto"
    assert seen["body"]["messages"] == original
    assert msgs == original


async def test_prompt_tag_handles_multimodal_content_list():
    """9. A multimodal turn's content is a LIST of parts — tag the last text part, never stringify
    the list; an images-only turn gains a text part carrying just the switch."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main", reasoning_style="prompt_tag")
    msgs = [{"role": "user", "content": [{"type": "text", "text": "what is this"},
                                         {"type": "image_url", "image_url": {"url": "data:x"}}]}]
    original = copy.deepcopy(msgs)
    await c.chat(msgs, think=False)
    parts = seen["body"]["messages"][0]["content"]
    assert isinstance(parts, list) and len(parts) == 2
    assert parts[0] == {"type": "text", "text": "what is this /no_think"}
    assert parts[1] == {"type": "image_url", "image_url": {"url": "data:x"}}
    assert msgs == original
    # images only -> a text part is added
    seen.clear()
    await c.chat([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "data:x"}}]}],
                 think=False)
    assert seen["body"]["messages"][0]["content"][-1] == {"type": "text", "text": "/no_think"}


async def test_prompt_tag_no_user_message_is_a_noop():
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main", reasoning_style="prompt_tag")
    await c.chat([{"role": "system", "content": "only a system turn"}], think=False)
    assert seen["body"]["messages"] == [{"role": "system", "content": "only a system turn"}]


async def test_unknown_reasoning_style_falls_back_to_auto():
    """10. A bad stored value must NEVER raise inside the client — a malformed connections file
    would otherwise brick every turn. It degrades to today's provider behaviour."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main", reasoning_style="bogus-vendor-thing")
    assert c.reasoning_style == "auto"
    await c.chat([{"role": "user", "content": "hi"}], think=False)
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}
    for bad in (None, "", "  AUTO  ", 17, {"nope": 1}):
        assert ModelClient("http://vllm.local/v1", "m", reasoning_style=bad).reasoning_style == "auto"
    assert ModelClient("http://vllm.local/v1", "m", extra_body="not a dict").extra_body == {}
    assert ModelClient("http://vllm.local/v1", "m", extra_body=None).extra_body == {}


async def test_reasoning_style_case_and_whitespace_normalized():
    c = ModelClient("http://vllm.local/v1", "m", reasoning_style="  OpenAI_Effort ")
    assert c.reasoning_style == "openai_effort"


# ---- probe() : the dashboard "Test connection" reachability check ----

async def test_probe_reachable():
    def handler(req):
        assert req.url.path.endswith("/chat/completions")
        assert req.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, json={"choices": [{"message": {"content": "x"}}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    r = await ModelClient("http://x/v1", "main", "secret").probe()
    assert r["ok"] is True and r["status"] == 200 and r["detail"] == "reachable"
    assert isinstance(r["latency_ms"], int)


async def test_probe_auth_failed():
    patch_asyncclient.transport = httpx.MockTransport(lambda req: httpx.Response(401, text="no"))
    r = await ModelClient("https://openrouter.ai/api/v1", "m", "bad").probe()
    assert r["ok"] is False and r["status"] == 401 and r["detail"] == "auth failed"


async def test_probe_model_not_found():
    patch_asyncclient.transport = httpx.MockTransport(
        lambda req: httpx.Response(404, text='{"error":"model not found"}'))
    r = await ModelClient("http://x/v1", "ghost").probe()
    assert r["ok"] is False and r["detail"] == "model not found"


async def test_probe_unreachable():
    def handler(req):
        raise httpx.ConnectError("refused")
    patch_asyncclient.transport = httpx.MockTransport(handler)
    r = await ModelClient("http://dead.local/v1", "m").probe()
    assert r["ok"] is False and r["status"] == 0 and r["detail"] == "unreachable"
    assert r["latency_ms"] is None


async def test_probe_sends_no_reasoning_tokens():
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    await ModelClient("http://vllm.local/v1", "main").probe()
    # a ping must stay cheap: max_tokens 1, and vLLM thinking disabled
    assert seen["body"]["max_tokens"] == 1
    assert seen["body"]["chat_template_kwargs"] == {"enable_thinking": False}


async def test_probe_embedding_uses_embeddings_endpoint():
    seen = {}
    def handler(req):
        seen["path"] = req.url.path
        return httpx.Response(200, json={"data": [{"embedding": [0.1]}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    r = await ModelClient("http://x/v1", "embed").probe(kind="embedding")
    assert r["ok"] is True and seen["path"].endswith("/embeddings")


async def test_probe_merges_extra_body():
    """A green Test means the connection's real request options are accepted by the endpoint —
    not just its bare base_url/auth/model id. If a saved extra_body would 400 on the real
    endpoint, the Test button is the one place a user finds out BEFORE every subsequent turn
    fails the same way. Same merge, same denylist, as chat()."""
    seen = {}
    patch_asyncclient.transport = httpx.MockTransport(_capture_handler(seen))
    c = ModelClient("http://vllm.local/v1", "main",
                    extra_body={"guided_json": {"type": "object"}, "max_tokens": 7,
                                "messages": [{"role": "user", "content": "PWNED"}]})
    await c.probe()
    assert seen["body"]["guided_json"] == {"type": "object"}
    assert seen["body"]["max_tokens"] == 7            # extra_body wins over probe's own max_tokens=1
    assert seen["body"]["messages"] != [{"role": "user", "content": "PWNED"}]   # denylisted, stripped


async def test_chat_captures_reasoning_field():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "51", "reasoning": "17 * 3 = 51."}, "finish_reason": "stop"}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    r = await ModelClient("http://x/v1", "main").chat([{"role": "user", "content": "17*3?"}])
    assert r.content == "51" and r.reasoning == "17 * 3 = 51."


async def test_chat_captures_reasoning_content_field():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "hi", "reasoning_content": "  vLLM style  "}, "finish_reason": "stop"}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    r = await ModelClient("http://x/v1", "main").chat([{"role": "user", "content": "hi"}])
    assert r.reasoning == "vLLM style"


async def test_chat_reasoning_absent_is_none():
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    r = await ModelClient("http://x/v1", "main").chat([{"role": "user", "content": "hi"}])
    assert r.reasoning is None


# ---- inline <think> extraction (models without a vLLM reasoning parser) ----
from engine.model_client import _split_think


def test_split_think_basic():
    ans, rz = _split_think("<think>let me add 12 and 8</think>The answer is 20.")
    assert ans == "The answer is 20." and rz == "let me add 12 and 8"


def test_split_think_unterminated():
    # model ran out of tokens mid-thought: everything after <think> is reasoning
    ans, rz = _split_think("prefix <think>still thinking and cut off")
    assert ans == "prefix" and rz == "still thinking and cut off"


def test_split_think_multiple_blocks():
    ans, rz = _split_think("<think>a</think>mid<think>b</think>end")
    assert ans == "midend" and rz == "a\n\nb"


def test_split_think_none_when_no_tag():
    ans, rz = _split_think("just a plain answer")
    assert ans == "just a plain answer" and rz is None


async def test_chat_extracts_inline_think(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "<think>17*23 = 391</think>The result is 391."}, "finish_reason": "stop"}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    r = await ModelClient("http://x/v1", "main").chat([{"role": "user", "content": "17*23?"}])
    assert r.content == "The result is 391." and r.reasoning == "17*23 = 391"


async def test_separate_reasoning_field_wins_over_inline():
    # if the backend already extracted reasoning, don't also strip content
    def handler(req):
        return httpx.Response(200, json={"choices": [{"message": {
            "content": "plain answer", "reasoning": "already parsed"}, "finish_reason": "stop"}]})
    patch_asyncclient.transport = httpx.MockTransport(handler)
    r = await ModelClient("http://x/v1", "main").chat([{"role": "user", "content": "hi"}])
    assert r.content == "plain answer" and r.reasoning == "already parsed"
