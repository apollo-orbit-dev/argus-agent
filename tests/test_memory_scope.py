"""Memory scoping: 'global' shares one bank across interfaces; 'session' isolates."""
from config import Config
from engine.engine import Engine


def _engine(tmp_path, **over):
    cfg = Config(model_base_url="http://x/v1", model_name="main",
                 telegram_bot_token="", **over)
    e = Engine(cfg)
    # point the memory store at a temp db (Engine builds its own in __init__)
    from engine.memory.store import MemoryStore
    from engine.memory.embeddings import EmbeddingClient
    from engine.memory.manager import Memory
    e.memory = Memory(MemoryStore(str(tmp_path / "m.db")), EmbeddingClient(), "off")
    return e


def test_global_scope_maps_every_session_to_one_key(tmp_path):
    e = _engine(tmp_path, memory_scope="global", memory_user_id="default")
    assert e._memory_key("dashboard") == "default"
    assert e._memory_key("123456789") == "default"      # a Telegram chat_id


def test_session_scope_keeps_raw_session_id(tmp_path):
    e = _engine(tmp_path, memory_scope="session")
    assert e._memory_key("dashboard") == "dashboard"
    assert e._memory_key("123456789") == "123456789"


async def test_global_scope_shares_facts_across_interfaces(tmp_path):
    """A fact saved under one session_id is visible under another (the real bug)."""
    e = _engine(tmp_path, memory_scope="global", memory_user_id="default")
    # dashboard remembers something
    await e.memory.remember(e._memory_key("dashboard"), "The user's name is John")
    # Telegram (a different session_id) must see it
    assert e.memory_stats("987654321")["count"] == 1
    summary_facts = e.memory.list(e._memory_key("987654321"))
    assert any("John" in f["text"] for f in summary_facts)


async def test_session_scope_isolates_facts(tmp_path):
    e = _engine(tmp_path, memory_scope="session")
    await e.memory.remember(e._memory_key("dashboard"), "The user's name is John")
    assert e.memory_stats("dashboard")["count"] == 1
    assert e.memory_stats("987654321")["count"] == 0   # a different chat sees nothing
