"""Re-embed: rebuild all stored memory + knowledge vectors with the current embedding model
(needed after changing the embedding role — vector spaces aren't compatible across models)."""
from config import Config
from engine.engine import Engine


class _FakeEmbedder:
    configured = True

    async def embed(self, texts):
        return [[0.1, 0.2, 0.3] for _ in texts]


def _engine(tmp_path):
    cfg = Config(model_base_url="http://vllm/v1", model_name="main", telegram_bot_token="",
                 memory_scope="session", embedding_base_url="http://vllm/v1", embedding_model="embed")
    e = Engine(cfg)
    # isolate the vector stores from the repo's memory.db/knowledge.db
    from engine.memory.manager import Memory
    from engine.memory.store import MemoryStore
    from engine.tools.knowledge import KnowledgeStore
    e.memory = Memory(MemoryStore(str(tmp_path / "m.db")), e.memory.embedder, "auto")
    e.knowledge = KnowledgeStore(str(tmp_path / "k.db"), e.knowledge.embedder)
    return e


async def test_reembed_updates_all_vectors(tmp_path):
    e = _engine(tmp_path)
    e.memory.store.add("u", "the user likes hiking")
    await e.knowledge.add("note", "some knowledge text worth remembering")
    fake = _FakeEmbedder()
    e.memory.embedder = fake
    e.knowledge.embedder = fake
    r = await e.reembed()
    assert r["ok"] and r["memory"] == 1 and r["knowledge"] >= 1 and r["failed"] == 0
    assert e.memory.store.facts_without_embeddings("u") == []      # every fact now has a vector


async def test_reembed_reports_when_no_embedder(tmp_path):
    e = _engine(tmp_path)
    e.memory.embedder = None
    r = await e.reembed()
    assert r["ok"] is False and "no embedding" in r["error"]


async def test_reembed_flags_embed_failure(tmp_path):
    class _Broken:
        configured = True
        async def embed(self, texts):
            return None                                            # provider unreachable
    e = _engine(tmp_path)
    e.memory.store.add("u", "a fact")
    e.memory.embedder = _Broken()
    e.knowledge.embedder = _Broken()
    r = await e.reembed()
    assert r["ok"] is False and r["failed"] >= 1 and r["memory"] == 0
