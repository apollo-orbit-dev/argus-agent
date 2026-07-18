"""Dashboard-driven CRUD over created tools/skills + the /status run_status probe."""
import asyncio
import json
import types

from config import Config
from engine.engine import Engine
from engine.experimental.skill_creation import CreateSkillTool
from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool


def _engine(tmp_path):
    cfg = Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token="")
    e = Engine(cfg)
    e._created_tools_dir = str(tmp_path / "created_tools")
    e._created_skills_dir = str(tmp_path / "created_skills")
    (tmp_path / "created_tools").mkdir()
    (tmp_path / "created_skills").mkdir()
    return e


# ---- created-skill delete ----

def test_delete_created_skill(tmp_path):
    e = _engine(tmp_path)
    treg = ToolRegistry(); treg.register(CalculatorTool())
    ct = CreateSkillTool(e.skill_registry, treg, e._created_skills_dir)
    asyncio.run(ct.run(ct.Params(name="temp", description="x", tools=[], procedure="1. Go.")))
    assert e.skill_registry.get("temp") is not None

    res = e.delete_created_skill("temp")
    assert res["ok"] is True
    assert e.skill_registry.get("temp") is None
    assert not (tmp_path / "created_skills" / "temp.md").exists()


def test_delete_created_skill_missing(tmp_path):
    e = _engine(tmp_path)
    res = e.delete_created_skill("ghost")
    assert res["ok"] is False and "no skill" in res["error"].lower()


def test_delete_created_skill_protects_builtin(tmp_path):
    e = _engine(tmp_path)
    from engine.skills.base import Skill
    e.skill_registry.register(Skill(name="lib_one", description="x", tools=[], procedure="1.",
                                    path="/elsewhere/library/lib_one.md"))
    res = e.delete_created_skill("lib_one")
    assert res["ok"] is False and "built-in" in res["error"].lower()
    assert e.skill_registry.get("lib_one") is not None


# ---- created-tool delete ----

def test_delete_created_tool(tmp_path):
    e = _engine(tmp_path)
    # a persisted json + a live sink entry, mirroring how a created tool exists
    (tmp_path / "created_tools" / "greet.json").write_text(json.dumps({"name": "greet"}))
    e._created_tools.append(types.SimpleNamespace(name="greet"))

    res = e.delete_created_tool("greet")
    assert res["ok"] is True
    assert not any(t.name == "greet" for t in e._created_tools)
    assert not (tmp_path / "created_tools" / "greet.json").exists()


def test_delete_created_tool_protects_builtin(tmp_path):
    e = _engine(tmp_path)
    res = e.delete_created_tool("calculator")     # not in the created sink
    assert res["ok"] is False and "not a created tool" in res["error"].lower()


# ---- run_status ----

def test_run_status_idle(tmp_path):
    e = _engine(tmp_path)
    s = e.run_status("dashboard")
    assert s["running"] is False
    assert s["current_step"] == 0
    assert s["turns"] == 0
    assert s["max_steps"] == e._config.max_steps


# ---- memory controls (list / forget / reviewable autoextract) ----

def _fresh_memory(e, tmp_path):
    from engine.memory.embeddings import EmbeddingClient
    from engine.memory.manager import Memory
    from engine.memory.store import MemoryStore
    e.memory = Memory(MemoryStore(str(tmp_path / "m.db")), EmbeddingClient(), "off")


async def test_memory_list_and_forget(tmp_path):
    e = _engine(tmp_path)
    _fresh_memory(e, tmp_path)
    await e.memory.remember(e._memory_key("s"), "The user likes tea")
    facts = e.memory_list("s")
    assert len(facts) == 1 and "tea" in facts[0]["text"]
    fid = facts[0]["id"]
    assert e.memory_forget("s", fid) is True
    assert e.memory_list("s") == []
    assert e.memory_forget("s", fid) is False           # already gone


async def test_notify_memory_saved_uses_deliver(tmp_path):
    e = _engine(tmp_path)
    sent = []

    async def _deliver(session_id, text):
        sent.append((session_id, text))
    e.scheduler.deliver = _deliver
    await e._notify_memory_saved("123", [{"id": 7, "text": "The user likes tea"}])
    assert sent and sent[0][0] == "123"
    assert "remembered" in sent[0][1].lower() and "/forget 7" in sent[0][1]


async def test_notify_memory_saved_noop_without_deliver(tmp_path):
    e = _engine(tmp_path)
    e.scheduler.deliver = None
    await e._notify_memory_saved("123", [{"id": 1, "text": "x"}])   # must not raise
