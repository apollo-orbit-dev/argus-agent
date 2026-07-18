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
