import asyncio

import httpx
import pytest

from engine.memory.embeddings import EmbeddingClient
from engine.memory.manager import Memory
from engine.memory.store import MemoryStore, _cosine
from engine.tools.memory import ForgetTool, RecallTool, RememberTool


# ---- store ----

def test_add_and_keyword_recall(tmp_path):
    s = MemoryStore(str(tmp_path / "m.db"))
    s.add("u", "The user's dog is a golden retriever named Max")
    s.add("u", "The user works as a nurse in Nashville")
    hits = s.recall("u", "what is the user's dog", k=3)
    assert hits and "golden retriever" in hits[0]["text"].lower()
    # scoped by user
    assert s.recall("other", "dog") == []


def test_dedup_reinforces_trust(tmp_path):
    s = MemoryStore(str(tmp_path / "m.db"))
    a = s.add("u", "User likes coffee")
    b = s.add("u", "user likes coffee")   # same (case-insensitive) -> reinforce, no dup
    assert a["id"] == b["id"] and b["trust"] > a["trust"]
    assert len(s.list("u")) == 1


def test_forget(tmp_path):
    s = MemoryStore(str(tmp_path / "m.db"))
    rec = s.add("u", "temporary fact")
    assert s.forget("u", rec["id"]) is True
    assert s.forget("u", rec["id"]) is False
    assert s.list("u") == []


def test_semantic_recall_with_embeddings(tmp_path):
    s = MemoryStore(str(tmp_path / "m.db"))
    # orthogonal-ish vectors: query close to fact1, far from fact2
    s.add("u", "fact one", embedding=[1.0, 0.0, 0.0])
    s.add("u", "fact two", embedding=[0.0, 1.0, 0.0])
    hits = s.recall("u", "unrelated words", k=2, query_embedding=[0.9, 0.1, 0.0])
    assert hits[0]["text"] == "fact one"  # semantic ranking, not keyword


def test_cosine():
    assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)


# ---- embedding client (mocked) ----

async def test_embedding_client_unconfigured_returns_none():
    assert await EmbeddingClient().embed(["x"]) is None            # no base url
    assert EmbeddingClient().configured is False


async def test_embedding_client_parses_response(monkeypatch):
    def handler(req):
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})
    real_init = httpx.AsyncClient.__init__

    def fake_init(self, *a, **k):
        k["transport"] = httpx.MockTransport(handler)
        real_init(self, *a, **k)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", fake_init)
    ec = EmbeddingClient("http://x/v1", "emb")
    assert await ec.embed_one("hi") == [0.1, 0.2]


# ---- manager policy ----

def test_semantic_enabled_policy(tmp_path):
    s = MemoryStore(str(tmp_path / "m.db"))
    assert Memory(s, EmbeddingClient(), "off").semantic_enabled is False
    assert Memory(s, EmbeddingClient(), "on").semantic_enabled is True
    assert Memory(s, EmbeddingClient(), "auto").semantic_enabled is False       # no endpoint
    assert Memory(s, EmbeddingClient("http://x/v1", "emb"), "auto").semantic_enabled is True


async def test_manager_remember_recall_keyword(tmp_path):
    m = Memory(MemoryStore(str(tmp_path / "m.db")), EmbeddingClient(), "off")
    await m.remember("u", "The user's favorite color is teal")
    hits = await m.recall("u", "what color does the user like")
    assert hits and "teal" in hits[0]["text"].lower()


# ---- tools ----

async def test_forget_by_description(tmp_path):
    m = Memory(MemoryStore(str(tmp_path / "m.db")), EmbeddingClient(), "off")
    await m.remember("u", "The user likes coffee in the morning")
    await m.remember("u", "The user drives a red truck")
    deleted = await m.forget_by_query("u", "coffee")
    assert deleted and "coffee" in deleted["text"].lower()
    assert len(m.list("u")) == 1                       # only the coffee fact removed
    assert await m.forget_by_query("u", "nonexistent xyz topic") is None or len(m.list("u")) == 1


async def test_forget_tool_by_description(tmp_path):
    m = Memory(MemoryStore(str(tmp_path / "m.db")), EmbeddingClient(), "off")
    await m.remember("u", "The user is allergic to peanuts")
    f = ForgetTool(m, "u")
    out = await f.run(f.Params(description="peanuts"))
    assert "forgot" in out.lower() and "peanut" in out.lower()
    assert m.list("u") == []


async def test_memory_tools(tmp_path):
    m = Memory(MemoryStore(str(tmp_path / "m.db")), EmbeddingClient(), "off")
    r = RememberTool(m, "u")
    out = await r.run(r.Params(fact="User is allergic to peanuts"))
    assert "remembered" in out.lower()
    rec = RecallTool(m, "u")
    assert "peanuts" in (await rec.run(rec.Params(query="what is the user allergic to"))).lower()
    # forget
    fid = m.list("u")[0]["id"]
    f = ForgetTool(m, "u")
    assert "forgot" in (await f.run(f.Params(memory_id=fid))).lower()
    assert "no relevant" in (await rec.run(rec.Params(query="what is the user allergic to"))).lower()
