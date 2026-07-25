"""Server-side model CONNECTIONS + capability ROLES — the unified model layer.

A CONNECTION is how to reach a model:
    {label, base_url, provider, model_name, api_key, context_window, capabilities}
A ROLE maps a capability (chat, embedding, vision, tts, stt, image_gen, video_gen) to a
connection label, so every subsystem — chat, embeddings, and later vision/audio — resolves its
model through the SAME registry. Add a provider (Ollama, another host, …) once as a connection
and remap; no per-tool connection code to rewrite.

A connection also carries its own REQUEST OPTIONS, so one Argus can drive heterogeneous hosts
without a global .env that only fits one of them:
    {"sampling": {...}, "extra_body": {...}, "reasoning_style": "auto"}
`extra_body` is the primary escape hatch — a free-form JSON object merged verbatim into the request,
so any vendor param (a thinking toggle Argus has never heard of, vLLM guided decoding, OpenRouter
routing preferences) works with no code change. `sampling` stays typed because top_k is
provider-gated (raw in extra_body it 400s on OpenAI/OpenRouter) and because it participates in the
per-call precedence chain. `reasoning_style` covers only what a static blob cannot reach: values that
must change per call, and the one convention that mutates the MESSAGES.

Persisted as one JSON file (shared by dashboard + Telegram), format:
    {"connections": [ ... ], "roles": {"chat": "<label>", "embedding": "<label>", ...}}
A legacy bare-list file (the old presets format) is migrated to `connections` on load — older files
simply lack the request-option keys and read back as the defaults, so nothing needs migrating.
"""
from __future__ import annotations

import copy
import json
import os
import threading

from engine.model_client import EXTRA_BODY_DENYLIST, REASONING_STYLES

# Capabilities the harness understands, modelled on the aux-tool model slots agents like Hermes
# expose. chat + embedding + utility are wired; the rest are reserved slots so their tools have a
# home the moment they're built (no re-plumbing).
#   utility  = cheap model for background work (compaction, autoextract, routing, captioning)
#   reasoning/coding = dedicated models for those tasks (else the chat model handles them)
ROLES = ("chat", "utility", "reasoning", "coding", "embedding", "vision",
         "tts", "stt", "image_gen", "video_gen")

# Per-connection sampling overrides. "unset" vs "explicitly 0.0" is expressed by KEY PRESENCE, not
# by a sentinel — which is why this is a nested object rather than four nullable columns.
SAMPLING_KEYS = {"temperature": float, "top_p": float, "top_k": int, "presence_penalty": float}


def _clean_sampling(value) -> dict:
    """Validate a sampling override object: unknown keys are dropped, known keys are coerced to
    their numeric type. A non-object, or a key whose value isn't a number, is a loud error."""
    if not isinstance(value, dict):
        raise ValueError("sampling must be a JSON object")
    out: dict = {}
    for key, cast in SAMPLING_KEYS.items():
        if key not in value:
            continue                      # ABSENT = unset; PRESENT (even 0.0) = an override
        v = value[key]
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            raise ValueError(f"sampling.{key} must be a number")
        try:
            out[key] = cast(v)
        except (TypeError, ValueError):
            raise ValueError(f"sampling.{key} must be a number") from None
    return out


def _clean_extra_body(value) -> dict:
    """Validate a free-form request-extras object. Denylisted keys are rejected LOUDLY here (and
    stripped again at merge time, so a hand-edited file can't sneak them through either)."""
    if not isinstance(value, dict):
        raise ValueError("extra_body must be a JSON object")
    for k in value:
        if isinstance(k, str) and k.strip().lower() in EXTRA_BODY_DENYLIST:
            raise ValueError(f"extra_body may not set '{k}' "
                             f"(reserved: {', '.join(EXTRA_BODY_DENYLIST)})")
    return copy.deepcopy(value)


def _clean_reasoning_style(value) -> str:
    s = (value if isinstance(value, str) else "").strip().lower()
    if s not in REASONING_STYLES:
        raise ValueError(f"reasoning_style must be one of: {', '.join(REASONING_STYLES)}")
    return s


class ModelPresetStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conns, self._roles = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return [], {}
        if isinstance(data, list):                       # legacy bare-list of presets
            return data, {}
        conns = data.get("connections")
        roles = data.get("roles")
        return (conns if isinstance(conns, list) else []), (roles if isinstance(roles, dict) else {})

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"connections": self._conns, "roles": self._roles}, f, indent=2)
        os.replace(tmp, self.path)                        # atomic

    # ---- connections ----
    def list(self) -> list:
        return [dict(p) for p in self._conns]

    def add(self, label: str, base_url: str, model_name: str, provider: str = "auto",
            context_window=None, api_key=None, capabilities=None,
            sampling=None, extra_body=None, reasoning_style=None) -> dict:
        """Add or replace a connection (by label, case-insensitive). Omitted api_key / capabilities
        / sampling / extra_body / reasoning_style on an UPDATE are preserved from the existing entry
        (so correcting the ctx window or model id never silently wipes a stored key or a tuned
        extra_body); pass `{}` to actually clear one. Bad request options raise ValueError, which
        the HTTP layer turns into a 400 — better a refused save than a connection that 400s on
        every turn weeks later."""
        label = (label or model_name).strip()
        samp = _clean_sampling(sampling) if sampling is not None else None
        extra = _clean_extra_body(extra_body) if extra_body is not None else None
        style = _clean_reasoning_style(reasoning_style) if reasoning_style is not None else None
        # An API key never contains '/' and never equals the model id — reject such a value (a model
        # name fumbled into the key field) so it can't clobber a real key or send garbage as auth.
        if isinstance(api_key, str) and ("/" in api_key or api_key.strip() == (model_name or "").strip()):
            api_key = None
        with self._lock:
            existing = next((p for p in self._conns if p.get("label", "").lower() == label.lower()), {})
            conn = {
                "label": label, "base_url": base_url, "model_name": model_name,
                "provider": (provider or "auto"), "context_window": context_window,
                "api_key": api_key if api_key is not None else existing.get("api_key", ""),
                "capabilities": list(capabilities if capabilities is not None
                                     else existing.get("capabilities", [])),
                "sampling": samp if samp is not None else dict(existing.get("sampling") or {}),
                "extra_body": (extra if extra is not None
                               else copy.deepcopy(existing.get("extra_body") or {})),
                "reasoning_style": style if style is not None else (existing.get("reasoning_style")
                                                                    or "auto"),
            }
            self._conns = [p for p in self._conns if p.get("label", "").lower() != label.lower()]
            self._conns.append(conn)
            self._save()
        return dict(conn)

    def remove(self, arg: str) -> int:
        a = (arg or "").strip().lower()
        with self._lock:
            before = len(self._conns)
            self._conns = [p for p in self._conns
                           if p.get("label", "").lower() != a and p.get("model_name", "").lower() != a]
            self._roles = {k: v for k, v in self._roles.items()          # drop now-dangling roles
                           if not (isinstance(v, str) and v.lower() == a)}
            self._save()
            return before - len(self._conns)

    def resolve(self, arg: str):
        """A connection by exact label/model_name, else a UNIQUE case-insensitive substring."""
        a = (arg or "").strip()
        if not a:
            return None
        for p in self._conns:
            if p.get("label") == a or p.get("model_name") == a:
                return dict(p)
        al = a.lower()
        for p in self._conns:
            if p.get("label", "").lower() == al or p.get("model_name", "").lower() == al:
                return dict(p)
        matches = [p for p in self._conns if al in p.get("model_name", "").lower()
                   or al in p.get("label", "").lower()]
        return dict(matches[0]) if len(matches) == 1 else None

    # ---- roles ----
    def roles(self) -> dict:
        return dict(self._roles)

    def get_role(self, capability: str):
        return self._roles.get((capability or "").strip().lower())

    def set_role(self, capability: str, label) -> None:
        cap = (capability or "").strip().lower()
        with self._lock:
            if label is None:
                self._roles.pop(cap, None)
            else:
                self._roles[cap] = label
            self._save()
