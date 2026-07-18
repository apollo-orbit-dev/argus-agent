"""Embedding client (OpenAI-compatible /v1/embeddings, e.g. a vLLM embedding server).

Optional: if no base URL is configured (or it's unreachable), embed() returns None
and memory recall falls back to keyword matching — so Argus runs fine on a box with
no embedding model.
"""
from __future__ import annotations

from typing import Optional

import httpx


class EmbeddingClient:
    def __init__(self, base_url: str = "", model: str = "", api_key: str = "dummy",
                 timeout: float = 30.0):
        self.base_url = (base_url or "").rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    async def embed(self, texts: list[str]) -> Optional[list[list[float]]]:
        """Return one embedding per input, or None if unconfigured/unreachable."""
        if not self.configured or not texts:
            return None
        url = f"{self.base_url}/embeddings"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as c:
                r = await c.post(url, json={"model": self.model, "input": texts}, headers=headers)
            if r.status_code != 200:
                return None
            data = r.json().get("data") or []
            embs = [d.get("embedding") for d in data]
            return embs if len(embs) == len(texts) and all(embs) else None
        except Exception:
            return None

    async def embed_one(self, text: str) -> Optional[list[float]]:
        out = await self.embed([text])
        return out[0] if out else None
