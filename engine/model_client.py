"""Thin async OpenAI-compatible chat client (httpx). Config-driven; no hardcoding."""
from __future__ import annotations

import re
from typing import Optional

import httpx

from engine.protocol import ModelResponse

_THINK_RE = re.compile(r"<think>(.*?)</think>", re.DOTALL | re.IGNORECASE)


def _split_think(content: str) -> tuple[Optional[str], Optional[str]]:
    """Separate inline <think>…</think> reasoning from the answer for models without a reasoning
    parser. Returns (answer, reasoning). Handles an unterminated <think> (the model ran out of
    tokens mid-thought): everything after the opening tag becomes reasoning."""
    blocks = [m.group(1).strip() for m in _THINK_RE.finditer(content) if m.group(1).strip()]
    answer = _THINK_RE.sub("", content).strip()
    if not blocks and "<think>" in content.lower():        # unterminated / truncated think
        idx = content.lower().find("<think>")
        blocks = [content[idx + len("<think>"):].strip()]
        answer = content[:idx].strip()
    reasoning = "\n\n".join(b for b in blocks if b) or None
    return (answer or None), reasoning


class ModelError(Exception):
    """Raised on transport failure / non-200 / timeout so the loop can surface it."""


class ModelClient:
    def __init__(self, base_url: str, model: str, api_key: str = "dummy",
                 timeout: float = 60.0, temperature: Optional[float] = None,
                 max_tokens: int = 1536, top_p: Optional[float] = None,
                 top_k: Optional[int] = None, presence_penalty: Optional[float] = None,
                 provider: str = "auto", reasoning: str = "auto"):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.top_p = top_p
        self.top_k = top_k
        self.presence_penalty = presence_penalty
        self.provider = self._resolve_provider(provider)
        self.reasoning = (reasoning or "auto").strip().lower()

    # Cloud hosts that speak the plain OpenAI wire format — no vLLM-only params, no /tokenize,
    # no vendor headers. Auto-detected so pointing the base URL at one just works; anything not
    # listed falls back to "vllm" (the local-server assumption). This is a convenience for the
    # common cases — for any other OpenAI-compatible endpoint, pick "openai-compatible" explicitly.
    _OPENAI_COMPATIBLE_HOSTS = ("fireworks.ai", "together.ai", "together.xyz", "groq.com",
                                "deepinfra.com", "lepton.ai", "anyscale.com", "endpoints.huggingface.cloud")
    _COMPAT_ALIASES = ("openai-compatible", "openai_compatible", "oai-compatible", "compatible", "generic")

    def _resolve_provider(self, provider: str) -> str:
        """Which backend this points at, so we send only params it accepts. 'auto' infers from
        the base URL — OpenRouter/OpenAI and the well-known OpenAI-compatible clouds are detected;
        anything else is assumed to be a local vLLM/Ollama server. 'openai-compatible' is the
        explicit generic choice: it sends ONLY the standard OpenAI params (no chat_template_kwargs,
        no top_k, no /tokenize, no vendor headers, no reasoning translation), for any endpoint that
        isn't one of the specifically-handled backends."""
        p = (provider or "auto").strip().lower()
        if p in self._COMPAT_ALIASES:
            return "openai-compatible"
        if p != "auto":
            return p
        host = self.base_url.lower()
        if "openrouter.ai" in host:
            return "openrouter"
        if "api.openai.com" in host:
            return "openai"
        if any(h in host for h in self._OPENAI_COMPATIBLE_HOSTS):
            return "openai-compatible"
        return "vllm"

    def _reasoning_params(self, level: str) -> dict:
        """Translate a normalized reasoning level (auto|off|low|medium|high) into the wire params
        the active backend understands. 'auto' sends nothing, so the model/provider default stands
        (and the local model behaves exactly as before this existed)."""
        lvl = (level or "auto").strip().lower()
        if lvl in ("", "auto", "default"):
            return {}
        if self.provider == "vllm":
            # The local reasoning model only toggles thinking on/off (no effort levels).
            return {"chat_template_kwargs": {"enable_thinking": lvl != "off"}}
        if self.provider == "openrouter":
            # OpenRouter's unified reasoning param: enabled=false to disable, else an effort level
            # (which it maps to a token budget for models that need one).
            if lvl == "off":
                return {"reasoning": {"enabled": False}}
            return {"reasoning": {"effort": lvl}}
        if self.provider == "openai":
            # o-series can't be fully disabled and base models ignore it, so 'off' just omits.
            return {} if lvl == "off" else {"reasoning_effort": lvl}
        # "openai-compatible" and any other backend: send no reasoning param. OpenAI-compatible
        # clouds differ on whether they accept reasoning_effort, and an unknown param can 400 —
        # so we let the model's own default stand rather than guess a wire format.
        return {}

    async def chat(self, messages: list[dict], tools: Optional[list[dict]] = None,
                   max_tokens: Optional[int] = None,
                   temperature: Optional[float] = None,
                   think: Optional[bool] = None,
                   reasoning: Optional[str] = None,
                   tool_choice: Optional[str] = None) -> ModelResponse:
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
        }
        # Only send sampling params that are explicitly configured. Anything left unset is omitted
        # so the model server (vLLM) applies its OWN default — we don't duplicate the model's tuning
        # here. A per-call `temperature` still wins (used e.g. to force determinism for a test).
        temp = temperature if temperature is not None else self.temperature
        if temp is not None:
            payload["temperature"] = temp
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        if self.top_k is not None and self.top_k > 0 and self.provider == "vllm":   # vLLM-only param
            payload["top_k"] = self.top_k
        if self.presence_penalty is not None:
            payload["presence_penalty"] = self.presence_penalty
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"   # native_finish forces "required"
        # Reasoning control, translated to the active backend's wire format. Priority:
        #  1. an explicit per-call `reasoning` level (the adaptive router sets this per turn),
        #  2. think=False -> "off" (auxiliary calls; also stops the local model from deliberating
        #     past its budget into empty content),
        #  3. otherwise the configured default (self.reasoning).
        level = reasoning if reasoning else ("off" if think is False else self.reasoning)
        payload.update(self._reasoning_params(level))

        url = f"{self.base_url}/chat/completions"
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        if self.provider == "openrouter":
            headers["X-Title"] = "Argus"   # optional attribution shown on OpenRouter's dashboards
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            raise ModelError(f"model request failed: {e}") from e
        if resp.status_code != 200:
            raise ModelError(f"model returned {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
            choice = data["choices"][0]
            msg = choice["message"]
        except Exception as e:
            raise ModelError(f"malformed model response: {e}") from e
        content = msg.get("content")
        # Reasoning/thinking trace, when the backend exposes one separately from content:
        # vLLM reasoning parsers -> `reasoning_content`; OpenRouter -> `reasoning` (+ `reasoning_details`).
        reasoning = msg.get("reasoning_content") or msg.get("reasoning")
        if isinstance(reasoning, list):        # OpenRouter reasoning_details-style list of parts
            reasoning = "".join(str(x.get("text", "")) if isinstance(x, dict) else str(x)
                                for x in reasoning)
        reasoning = reasoning.strip() if isinstance(reasoning, str) and reasoning.strip() else None
        # Fallback for models WITHOUT a reasoning parser: they leave the chain-of-thought inline as
        # <think>…</think> in the content. Lift it into `reasoning` and strip it from the answer so
        # the thinking shows in the trace (not dumped into the user-facing reply).
        if not reasoning and isinstance(content, str) and "<think>" in content:
            content, reasoning = _split_think(content)
        return ModelResponse(
            content=content,
            tool_calls=msg.get("tool_calls") or [],
            finish_reason=choice.get("finish_reason"),
            usage=data.get("usage") or {},
            reasoning=reasoning,
        )

    async def probe(self, kind: str = "chat") -> dict:
        """Lightweight connectivity check for the dashboard 'Test' button: one minimal request
        (a max_tokens=1 completion, or a 1-input embedding for embedding connections) so it
        validates base_url + auth + model id in a single tiny, user-initiated call. Returns
        {ok, status, detail, latency_ms, hint?} rather than raising, so the UI can show a
        specific reason."""
        import time
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        if self.provider == "openrouter":
            headers["X-Title"] = "Argus"
        if kind == "embedding":
            url = f"{self.base_url}/embeddings"
            payload = {"model": self.model, "input": "ping"}
        else:
            url = f"{self.base_url}/chat/completions"
            payload = {"model": self.model, "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 1}
            payload.update(self._reasoning_params("off"))   # don't burn reasoning tokens on a ping
        t0 = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as e:
            return {"ok": False, "status": 0, "detail": "unreachable",
                    "hint": str(e)[:160], "latency_ms": None}
        ms = int((time.perf_counter() - t0) * 1000)
        sc = resp.status_code
        if sc == 200:
            return {"ok": True, "status": 200, "detail": "reachable", "latency_ms": ms}
        if sc in (401, 403):
            return {"ok": False, "status": sc, "detail": "auth failed", "latency_ms": ms}
        body = ""
        try:
            body = resp.text
        except Exception:
            pass
        if sc == 404 or (sc == 400 and "model" in body.lower()):
            return {"ok": False, "status": sc, "detail": "model not found", "latency_ms": ms}
        return {"ok": False, "status": sc, "detail": f"HTTP {sc}",
                "hint": body[:160], "latency_ms": ms}

    async def token_count(self, text: str) -> int:
        """Exact token count via the vLLM /tokenize endpoint; estimate on failure or non-vLLM."""
        if not text:
            return 0
        if self.provider != "vllm":
            return max(1, len(text) // 4)   # OpenRouter/OpenAI expose no /tokenize; estimate
        url = self.base_url.rsplit("/v1", 1)[0].rstrip("/") + "/tokenize"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.post(url, json={"model": self.model, "prompt": text})
            if r.status_code == 200:
                return int(r.json().get("count", 0))
        except Exception:
            pass
        return max(1, len(text) // 4)  # fallback estimate
