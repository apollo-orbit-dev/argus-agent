"""Knowledge base (RAG) — teach Argus a body of documents and retrieve grounded passages.

Different from fact-memory (short durable facts about the user): this is a chunked, embedded
store you add documents/notes to and SEMANTICALLY search. Reuses the embedding endpoint. When
embeddings aren't configured it degrades to keyword overlap so search still works.
"""
from __future__ import annotations

import re
import sqlite3
import time

from pydantic import BaseModel, Field

from engine.memory.store import _blob_to_vec, _cosine, _tokens, _vec_to_blob
from engine.tools.base import Tool


def chunk_text(text: str, size: int = 800, overlap: int = 150) -> list[str]:
    """Split into overlapping windows, preferring paragraph/sentence boundaries."""
    text = re.sub(r"\n{3,}", "\n\n", (text or "").strip())
    if len(text) <= size:
        return [text] if text else []
    chunks, i = [], 0
    while i < len(text):
        end = min(i + size, len(text))
        if end < len(text):                         # back up to a nearby boundary
            window = text[i:end]
            cut = max(window.rfind("\n\n"), window.rfind(". "), window.rfind("\n"))
            if cut > size * 0.5:
                end = i + cut + 1
        chunks.append(text[i:end].strip())
        if end >= len(text):
            break
        i = max(end - overlap, i + 1)
    return [c for c in chunks if c]


class KnowledgeStore:
    def __init__(self, path: str, embedder):
        self.embedder = embedder
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "source TEXT NOT NULL, text TEXT NOT NULL, embedding BLOB, created_at REAL)")
        self._db.commit()

    async def add(self, source: str, text: str) -> int:
        chunks = chunk_text(text)
        if not chunks:
            return 0
        embs = None
        if self.embedder and self.embedder.configured:
            embs = await self.embedder.embed(chunks)
        embs = embs or [None] * len(chunks)
        now = time.time()
        for c, e in zip(chunks, embs):
            self._db.execute(
                "INSERT INTO chunks (source, text, embedding, created_at) VALUES (?,?,?,?)",
                (source, c, _vec_to_blob(e), now))
        self._db.commit()
        return len(chunks)

    async def search(self, query: str, k: int = 5) -> list[dict]:
        rows = list(self._db.execute("SELECT source, text, embedding FROM chunks"))
        if not rows:
            return []
        qv = await self.embedder.embed_one(query) if self.embedder and self.embedder.configured else None
        scored = []
        if qv:
            for r in rows:
                v = _blob_to_vec(r["embedding"])
                scored.append((_cosine(qv, v) if v else 0.0, r))
        else:                                        # keyword-overlap fallback
            qtok = _tokens(query)
            for r in rows:
                overlap = len(qtok & _tokens(r["text"])) / (len(qtok) or 1)
                scored.append((overlap, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [{"source": r["source"], "text": r["text"], "score": round(s, 3)}
                for s, r in scored[:k] if s > 0]

    def sources(self) -> list[dict]:
        rows = self._db.execute(
            "SELECT source, COUNT(*) n FROM chunks GROUP BY source ORDER BY MAX(created_at) DESC")
        return [{"source": r["source"], "chunks": r["n"]} for r in rows]

    def forget(self, source: str) -> int:
        cur = self._db.execute("DELETE FROM chunks WHERE source=?", (source,))
        self._db.commit()
        return cur.rowcount

    def all_chunks(self) -> list[dict]:
        """Every chunk as {id, text} — for re-embedding after an embedding-model change."""
        return [{"id": r["id"], "text": r["text"]}
                for r in self._db.execute("SELECT id, text FROM chunks")]

    def set_embedding(self, chunk_id: int, embedding) -> None:
        self._db.execute("UPDATE chunks SET embedding=? WHERE id=?",
                         (_vec_to_blob(embedding), chunk_id))
        self._db.commit()

    def stats(self) -> dict:
        n = self._db.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"]
        return {"chunks": n, "sources": len(self.sources()),
                "semantic": bool(self.embedder and self.embedder.configured)}


class AddToKnowledgeTool(Tool):
    name = "add_to_knowledge"
    description = (
        "Add a document or notes to your knowledge base so you can semantically search it later. "
        "Provide `source` (a short label, e.g. the file/topic name) and EITHER `text` (the content) "
        "OR `file` (a file name in your workspace — it will be read, incl. PDFs/Word/Excel and "
        "scanned PDFs via OCR). Use for reference material you'll want to look things up in."
    )

    class Params(BaseModel):
        source: str = Field(..., description="short label for where this came from")
        text: str = Field("", description="the content to add (or leave empty and give `file`)")
        file: str = Field("", description="a workspace file to read and add instead of text")

    def __init__(self, store: KnowledgeStore, workspace=None):
        self.store = store
        self.workspace = workspace

    async def run(self, args: "AddToKnowledgeTool.Params") -> str:
        import asyncio
        text = args.text or ""
        if args.file and self.workspace is not None:
            path = self.workspace.path_if_exists(args.file)
            if not path:
                return f"add_to_knowledge: no file '{args.file}' in the workspace."
            from engine.tools.documents import extract_document
            try:
                text = await asyncio.to_thread(extract_document, path)
            except Exception as e:
                return f"add_to_knowledge: could not read '{args.file}' ({e})."
        if not (text or "").strip():
            return "add_to_knowledge: nothing to add — provide `text` or a readable `file`."
        n = await self.store.add(args.source.strip() or "note", text)
        return (f"add_to_knowledge: added {n} chunk(s) from '{args.source}' to your knowledge base. "
                "Use search_knowledge to look things up in it.")


class ListKnowledgeTool(Tool):
    name = "list_knowledge"
    description = ("List the sources currently in your knowledge base (their labels and how many "
                  "chunks each). Use it to see what you can search, or to find a source's exact "
                  "label before forgetting it. No arguments.")

    class Params(BaseModel):
        pass

    def __init__(self, store: KnowledgeStore):
        self.store = store

    async def run(self, args: "ListKnowledgeTool.Params") -> str:
        srcs = self.store.sources()
        if not srcs:
            return "Your knowledge base is empty."
        lines = "\n".join(f"  {s['source']} ({s['chunks']} chunk{'s' if s['chunks'] != 1 else ''})"
                          for s in srcs)
        return "Knowledge base sources:\n" + lines


class ForgetKnowledgeTool(Tool):
    name = "forget_knowledge"
    description = ("Remove an entire SOURCE from your knowledge base by its label — this deletes all "
                  "of that source's chunks (removal is by source, not individual passages). Check "
                  "list_knowledge first for the exact label. Arg: source.")

    class Params(BaseModel):
        source: str = Field(..., description="the source label to remove (see list_knowledge)")

    def __init__(self, store: KnowledgeStore):
        self.store = store

    async def run(self, args: "ForgetKnowledgeTool.Params") -> str:
        n = self.store.forget((args.source or "").strip())
        if n == 0:
            existing = ", ".join(s["source"] for s in self.store.sources()) or "(none)"
            return f"forget_knowledge: no source '{args.source}'. Sources: {existing}."
        return (f"forget_knowledge: removed '{args.source}' from your knowledge base "
                f"({n} chunk{'s' if n != 1 else ''}).")


class SearchKnowledgeTool(Tool):
    name = "search_knowledge"
    description = ("Search your knowledge base for passages relevant to a query and get them back "
                   "with their source. Use before answering questions about material you've added. "
                   "Args: query, optional k (default 5).")

    class Params(BaseModel):
        query: str = Field(..., description="what to look up")
        k: int = Field(5, description="how many passages to return")

    def __init__(self, store: KnowledgeStore):
        self.store = store

    async def run(self, args: "SearchKnowledgeTool.Params") -> str:
        hits = await self.store.search(args.query, k=max(1, min(args.k, 10)))
        if not hits:
            return ("search_knowledge: no relevant passages found "
                    "(the knowledge base may be empty — add material with add_to_knowledge).")
        out = []
        for h in hits:
            snippet = h["text"][:600] + ("…" if len(h["text"]) > 600 else "")
            out.append(f"[{h['source']}] (score {h['score']})\n{snippet}")
        return "\n\n".join(out)
