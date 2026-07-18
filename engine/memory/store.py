"""Persistent fact memory — SQLite storage + trust scoring, hybrid retrieval.

Design goals: dependency-free (stdlib sqlite3 only), local, small-scale (a personal
assistant has hundreds of facts, not millions). Storage is SQLite; retrieval is done
in Python — keyword token-overlap plus, when embeddings are available, semantic
cosine. Trust scoring reinforces facts confirmed repeatedly. (FTS5 / sqlite-vec are
future optimizations for larger scale.)
"""
from __future__ import annotations

import re
import sqlite3
import time
from array import array
from typing import Optional

_STOP = {"the", "a", "an", "and", "or", "is", "are", "was", "were", "to", "of", "in",
         "on", "my", "your", "his", "her", "for", "with", "that", "this", "it",
         "i", "you", "me", "we", "do", "does", "what", "who", "when", "where", "how"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
            if len(w) > 1 and w not in _STOP}


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _vec_to_blob(vec: Optional[list[float]]) -> Optional[bytes]:
    return array("f", vec).tobytes() if vec else None


def _blob_to_vec(blob: Optional[bytes]) -> Optional[list[float]]:
    if not blob:
        return None
    a = array("f")
    a.frombytes(blob)
    return list(a)


class MemoryStore:
    def __init__(self, path: str):
        self.path = path
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("""
            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                text TEXT NOT NULL,
                trust REAL NOT NULL DEFAULT 0.5,
                source TEXT DEFAULT 'user',
                embedding BLOB,
                created_at REAL, updated_at REAL
            )""")
        self._db.commit()

    # ---- write ----
    def add(self, user_id: str, text: str, source: str = "user",
            embedding: Optional[list[float]] = None) -> dict:
        text = (text or "").strip()
        now = time.time()
        # dedup / reinforce: exact (case-insensitive) match -> bump trust instead of duplicating
        row = self._db.execute(
            "SELECT * FROM facts WHERE user_id=? AND lower(text)=lower(?)",
            (user_id, text)).fetchone()
        if row:
            new_trust = min(1.0, row["trust"] + 0.1)
            self._db.execute("UPDATE facts SET trust=?, updated_at=? WHERE id=?",
                             (new_trust, now, row["id"]))
            self._db.commit()
            return self._row(row["id"])
        cur = self._db.execute(
            "INSERT INTO facts (user_id, text, trust, source, embedding, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, text, 0.5, source, _vec_to_blob(embedding), now, now))
        self._db.commit()
        return self._row(cur.lastrowid)

    def forget(self, user_id: str, fact_id: int) -> bool:
        cur = self._db.execute("DELETE FROM facts WHERE id=? AND user_id=?", (fact_id, user_id))
        self._db.commit()
        return cur.rowcount > 0

    def set_embedding(self, fact_id: int, embedding: list[float]) -> None:
        self._db.execute("UPDATE facts SET embedding=? WHERE id=?",
                         (_vec_to_blob(embedding), fact_id))
        self._db.commit()

    # ---- read ----
    def _row(self, fact_id: int) -> dict:
        r = self._db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        return dict(id=r["id"], text=r["text"], trust=r["trust"], source=r["source"],
                    has_embedding=r["embedding"] is not None)

    def list(self, user_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, text, trust, source FROM facts WHERE user_id=? ORDER BY trust DESC, updated_at DESC",
            (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def facts_without_embeddings(self, user_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, text FROM facts WHERE user_id=? AND embedding IS NULL", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def all_facts(self) -> list[dict]:
        """Every fact (all users) as {id, text} — for re-embedding after an embedding-model change."""
        rows = self._db.execute("SELECT id, text FROM facts").fetchall()
        return [{"id": r["id"], "text": r["text"]} for r in rows]

    def recall(self, user_id: str, query: str, k: int = 5,
               query_embedding: Optional[list[float]] = None) -> list[dict]:
        """Hybrid recall. Semantic (cosine) when embeddings are present, else keyword
        token-overlap; both weighted by trust. Returns [{id,text,trust,score}, ...]."""
        rows = self._db.execute(
            "SELECT id, text, trust, embedding FROM facts WHERE user_id=?", (user_id,)).fetchall()
        if not rows:
            return []
        qtok = _tokens(query)
        scored = []
        for r in rows:
            vec = _blob_to_vec(r["embedding"])
            if query_embedding is not None and vec is not None:
                base = _cosine(query_embedding, vec)          # semantic
                if qtok:                                       # small keyword tiebreak
                    ov = len(qtok & _tokens(r["text"])) / max(len(qtok), 1)
                    base = 0.85 * base + 0.15 * ov
            else:
                ftok = _tokens(r["text"])
                base = len(qtok & ftok) / max(len(qtok | ftok), 1) if (qtok or ftok) else 0.0
            score = base * (0.5 + 0.5 * r["trust"])            # trust weighting
            if score > 0:
                scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [dict(id=r["id"], text=r["text"], trust=r["trust"], score=round(s, 4))
                for s, r in scored[:k]]
