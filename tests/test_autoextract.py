from config import Config
from engine.engine import Engine
from engine.memory.embeddings import EmbeddingClient
from engine.memory.manager import Memory
from engine.memory.store import MemoryStore
from engine.protocol import ModelResponse


class _FakeExtractor:
    def __init__(self, content):
        self.content = content
    async def chat(self, messages, tools=None, max_tokens=None, temperature=None, think=None, reasoning=None):
        return ModelResponse(content=self.content, finish_reason="stop")


def _engine(tmp_path, extractor_output):
    # session scope so the memory key == session_id (scoping is tested in test_memory_scope)
    cfg = Config(model_base_url="http://x/v1", model_name="main",
                 telegram_bot_token="", memory_scope="session")
    e = Engine(cfg)
    e.memory = Memory(MemoryStore(str(tmp_path / "m.db")), EmbeddingClient(), "off")
    e._model_client = lambda: _FakeExtractor(extractor_output)
    return e


async def test_autoextract_saves_durable_facts(tmp_path):
    e = _engine(tmp_path, "The user's name is John\nThe user loves hiking")
    saved = await e.autoextract("u", "My name is John and I love hiking")
    assert len(saved) == 2
    facts = [f["text"] for f in e.memory.list("u")]
    assert any("John" in f for f in facts) and any("hiking" in f for f in facts)
    # saved with source 'auto'
    import sqlite3
    db = sqlite3.connect(str(tmp_path / "m.db"))
    assert db.execute("SELECT count(*) FROM facts WHERE source='auto'").fetchone()[0] == 2


async def test_autoextract_none_saves_nothing(tmp_path):
    e = _engine(tmp_path, "NONE")
    assert await e.autoextract("u", "what's the weather?") == []
    assert e.memory.list("u") == []


async def test_autoextract_filters_non_facts(tmp_path):
    # only 'The user ...' lines are kept; chatter is dropped
    e = _engine(tmp_path, "Sure, I can help!\nThe user is a nurse\n- The user lives in Nashville")
    saved = await e.autoextract("u", "I'm a nurse in Nashville")
    assert len(saved) == 2
    assert all(f.lower().startswith("the user") for f in saved)


async def test_autoextract_skips_trivial_input(tmp_path):
    e = _engine(tmp_path, "The user said hi")
    assert await e.autoextract("u", "hi") == []  # too short to bother


async def test_autoextract_drops_vague_project_fact(tmp_path):
    # the exact reported failure: a capable model emits "The user is working on a project" — junk.
    e = _engine(tmp_path, "The user is working on a project")
    assert await e.autoextract("u", "this is actually my project!") == []
    assert e.memory.list("u") == []


async def test_autoextract_keeps_specific_project_fact(tmp_path):
    # a NAMED project with real detail is worth keeping
    e = _engine(tmp_path, "The user is building Vessel, an app that turns spreadsheets into apps")
    saved = await e.autoextract("u", "I'm building Vessel, it turns spreadsheets into apps")
    assert len(saved) == 1 and "Vessel" in saved[0]


async def test_autoextract_drops_transient_feedback_fact(tmp_path):
    e = _engine(tmp_path, "The user asked for feedback on their project")
    assert await e.autoextract("u", "what do you think of this?") == []


async def test_low_value_fact_matcher():
    from engine.engine import _low_value_fact
    assert _low_value_fact("The user is working on a project")
    assert _low_value_fact("The user is working on an app.")
    assert _low_value_fact("The user asked for feedback")
    assert _low_value_fact("The user wants help with something")
    # specific / durable facts must pass through
    assert not _low_value_fact("The user is building Vessel, an app for spreadsheets")
    assert not _low_value_fact("The user's name is John")
    assert not _low_value_fact("The user is allergic to penicillin")


class _RecordingExtractor:
    def __init__(self, content):
        self.content = content
        self.last_messages = None
    async def chat(self, messages, tools=None, max_tokens=None, temperature=None,
                   think=None, reasoning=None):
        self.last_messages = messages
        return ModelResponse(content=self.content, finish_reason="stop")


async def test_autoextract_includes_recent_context(tmp_path):
    # with prior turns naming the project, the extractor gets the context AND saves the specific fact
    e = _engine(tmp_path, "unused")
    rec = _RecordingExtractor("The user is building Vessel, an app for spreadsheets")
    e._model_client = lambda: rec
    e.store.extend_messages("u", [
        {"role": "user", "content": "review my Vessel project on github"},
        {"role": "assistant", "content": "Vessel turns spreadsheets into AI-generated apps"},
    ])
    saved = await e.autoextract("u", "this is actually my project!")
    ctx = rec.last_messages[-1]["content"]
    assert "Vessel" in ctx and "this is actually my project" in ctx    # context passed
    assert len(saved) == 1 and "Vessel" in saved[0]                    # specific fact saved


async def test_autoextract_no_context_when_history_empty(tmp_path):
    e = _engine(tmp_path, "unused")
    rec = _RecordingExtractor("NONE")
    e._model_client = lambda: rec
    await e.autoextract("u", "hello there friend")
    assert "(no prior context)" in rec.last_messages[-1]["content"]


def test_low_value_rejects_model_reasoning_blob():
    from engine.engine import _low_value_fact
    # exact junk from the screenshot (id 26): the model's own reasoning, not a fact
    assert _low_value_fact(
        "The user asked for the definition of 'altruism' after it was already provided in the "
        "conversation. This is a request for information, not a statement of a new, durable fact "
        "about the user. No specific, durable, non-obvious information was stated or confirmed.")


def test_low_value_rejects_transient_lookups():
    from engine.engine import _low_value_fact
    assert _low_value_fact("The user is looking for a comparison of general-purpose agents like "
                           "Hermes, OpenClaw, and NanoClaw, not coding-specific agents.")   # id 27
    assert _low_value_fact("The user asked for the definition of altruism")
    assert _low_value_fact("The user wants to know how vLLM works")
    assert _low_value_fact("The user is comparing agent frameworks")
    assert _low_value_fact("The user is exploring options for a new laptop")


def test_low_value_keeps_real_durable_facts():
    from engine.engine import _low_value_fact
    for f in ["The user's name is John",
              "The user is allergic to penicillin",
              "The user is building Vessel, an app that turns spreadsheets into AI-generated software",
              "The user's daughter is named Mia",
              "The user is a licensed PE in Florida",
              "The user needs a wheelchair for mobility",
              "The user wants to lose 20 pounds this year"]:
        assert not _low_value_fact(f), f
