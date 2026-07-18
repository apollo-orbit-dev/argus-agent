"""Memory manager — store + embeddings + the semantic-recall policy.

SEMANTIC_RECALL:
  off  -> keyword recall only (no embedding model needed)
  on   -> always try semantic (requires a configured embedding endpoint)
  auto -> semantic if an embedding endpoint is configured, else keyword (default)
"""
from __future__ import annotations

from typing import Optional

from engine.memory.embeddings import EmbeddingClient
from engine.memory.store import MemoryStore


class Memory:
    def __init__(self, store: MemoryStore, embedder: EmbeddingClient,
                 semantic_recall: str = "auto"):
        self.store = store
        self.embedder = embedder
        self.semantic_recall = semantic_recall

    @property
    def semantic_enabled(self) -> bool:
        if self.semantic_recall == "off":
            return False
        if self.semantic_recall == "on":
            return True
        return self.embedder.configured  # "auto"

    async def remember(self, user_id: str, text: str, source: str = "user") -> dict:
        emb = await self.embedder.embed_one(text) if self.semantic_enabled else None
        return self.store.add(user_id, text, source=source, embedding=emb)

    async def recall(self, user_id: str, query: str, k: int = 5) -> list[dict]:
        qemb = await self.embedder.embed_one(query) if self.semantic_enabled else None
        return self.store.recall(user_id, query, k=k, query_embedding=qemb)

    def forget(self, user_id: str, fact_id: int) -> bool:
        return self.store.forget(user_id, fact_id)

    async def forget_by_query(self, user_id: str, query: str) -> Optional[dict]:
        """Find the best-matching memory for a description and delete it. Returns the
        deleted fact, or None if nothing matched."""
        hits = await self.recall(user_id, query, k=1)
        if not hits:
            return None
        top = hits[0]
        return top if self.store.forget(user_id, top["id"]) else None

    def list(self, user_id: str) -> list[dict]:
        return self.store.list(user_id)
