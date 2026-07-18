"""Server-side model CONNECTIONS + capability ROLES — the unified model layer.

A CONNECTION is how to reach a model:
    {label, base_url, provider, model_name, api_key, context_window, capabilities}
A ROLE maps a capability (chat, embedding, vision, tts, stt, image_gen, video_gen) to a
connection label, so every subsystem — chat, embeddings, and later vision/audio — resolves its
model through the SAME registry. Add a provider (Ollama, another host, …) once as a connection
and remap; no per-tool connection code to rewrite.

Persisted as one JSON file (shared by dashboard + Telegram), format:
    {"connections": [ ... ], "roles": {"chat": "<label>", "embedding": "<label>", ...}}
A legacy bare-list file (the old presets format) is migrated to `connections` on load.
"""
from __future__ import annotations

import json
import os
import threading

# Capabilities the harness understands, modelled on the aux-tool model slots agents like Hermes
# expose. chat + embedding + utility are wired; the rest are reserved slots so their tools have a
# home the moment they're built (no re-plumbing).
#   utility  = cheap model for background work (compaction, autoextract, routing, captioning)
#   reasoning/coding = dedicated models for those tasks (else the chat model handles them)
ROLES = ("chat", "utility", "reasoning", "coding", "embedding", "vision",
         "tts", "stt", "image_gen", "video_gen")


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
            context_window=None, api_key=None, capabilities=None) -> dict:
        """Add or replace a connection (by label, case-insensitive). Omitted api_key / capabilities
        on an UPDATE are preserved from the existing entry (so correcting the ctx window or model id
        never silently wipes a stored key)."""
        label = (label or model_name).strip()
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
