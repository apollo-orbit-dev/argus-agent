"""Knowledge base: chunking + add/search (keyword fallback when no embedder)."""
import asyncio
import types

from engine.tools.files import FileWorkspace
from engine.tools.knowledge import (AddToKnowledgeTool, ForgetKnowledgeTool, KnowledgeStore,
                                     ListKnowledgeTool, SearchKnowledgeTool, chunk_text)


def _store(tmp_path):
    # embedder not configured → keyword-overlap search (no network). `configured` is a bool
    # property on the real EmbeddingClient, so the fake exposes a bool attribute, not a method.
    embedder = types.SimpleNamespace(configured=False)
    return KnowledgeStore(str(tmp_path / "kb.db"), embedder)


def test_chunk_text():
    assert chunk_text("") == []
    assert chunk_text("short") == ["short"]
    big = "para one.\n\n" + ("x " * 600) + "\n\npara two."
    chunks = chunk_text(big, size=400, overlap=80)
    assert len(chunks) >= 2
    assert all(len(c) <= 500 for c in chunks)


def test_add_and_search_keyword(tmp_path):
    kb = _store(tmp_path)
    n = asyncio.run(kb.add("device-notes", "Solar panels convert sunlight into electricity. "
                           "Inverters change DC power to AC. Batteries store energy for the night."))
    assert n >= 1
    hits = asyncio.run(kb.search("how do solar panels work", k=3))
    assert hits and any("solar panels" in h["text"].lower() for h in hits)
    assert kb.stats()["chunks"] >= 1
    assert kb.sources()[0]["source"] == "device-notes"


def test_forget(tmp_path):
    kb = _store(tmp_path)
    asyncio.run(kb.add("s1", "alpha beta gamma delta epsilon"))
    assert kb.stats()["chunks"] >= 1
    removed = kb.forget("s1")
    assert removed >= 1 and kb.stats()["chunks"] == 0


def test_add_tool_from_text(tmp_path):
    kb = _store(tmp_path)
    t = AddToKnowledgeTool(kb, workspace=None)
    out = asyncio.run(t.run(t.Params(source="doc", text="The capital of France is Paris.")))
    assert "added" in out.lower()
    st = SearchKnowledgeTool(kb)
    res = asyncio.run(st.run(st.Params(query="france capital", k=2)))
    assert "Paris" in res


def test_list_and_forget_tools(tmp_path):
    kb = _store(tmp_path)
    asyncio.run(kb.add("manual", "chapter one text here"))
    asyncio.run(kb.add("notes", "some other notes entirely"))
    lt = ListKnowledgeTool(kb)
    listing = asyncio.run(lt.run(lt.Params()))
    assert "manual" in listing and "notes" in listing

    ft = ForgetKnowledgeTool(kb)
    out = asyncio.run(ft.run(ft.Params(source="manual")))
    assert "removed" in out.lower()
    assert "manual" not in asyncio.run(lt.run(lt.Params()))       # gone
    assert "notes" in asyncio.run(lt.run(lt.Params()))            # kept
    # forgetting a nonexistent source reports the real sources
    miss = asyncio.run(ft.run(ft.Params(source="ghost")))
    assert "no source" in miss.lower() and "notes" in miss


def test_list_empty(tmp_path):
    kb = _store(tmp_path)
    assert "empty" in asyncio.run(ListKnowledgeTool(kb).run(ListKnowledgeTool.Params())).lower()


def test_add_tool_from_file(tmp_path):
    ws = FileWorkspace(str(tmp_path / "ws"))
    ws.write_text("facts.txt", "The mitochondria is the powerhouse of the cell.")
    kb = _store(tmp_path)
    t = AddToKnowledgeTool(kb, workspace=ws)
    out = asyncio.run(t.run(t.Params(source="bio", file="facts.txt")))
    assert "added" in out.lower()
    res = asyncio.run(SearchKnowledgeTool(kb).run(SearchKnowledgeTool.Params(query="mitochondria", k=1)))
    assert "powerhouse" in res
