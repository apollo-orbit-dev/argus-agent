import asyncio

from engine.experimental.skill_creation import (
    CreateSkillTool, DeleteSkillTool, InspectSkillTool,
    render_skill_markdown, sanitize_skill_name,
)
from engine.skills.base import SkillRegistry
from engine.tools.base import ToolRegistry
from engine.tools.calculator import CalculatorTool


def _tool_reg():
    r = ToolRegistry()
    r.register(CalculatorTool())
    return r


def _make(tmp_path):
    sk = SkillRegistry()
    return CreateSkillTool(sk, _tool_reg(), str(tmp_path)), sk


def test_sanitize_name():
    assert sanitize_skill_name("Plan A Trip!") == "plan_a_trip"
    assert sanitize_skill_name("../evil") == "evil"


def test_render_markdown_roundtrips():
    md = render_skill_markdown("t", "desc", ["calculator"], "1. Do it.")
    from engine.skills.base import parse_frontmatter
    meta, body = parse_frontmatter(md)
    assert meta["name"] == "t" and meta["tools"] == ["calculator"] and body == "1. Do it."


def test_create_skill_registers_and_writes(tmp_path):
    ct, sk = _make(tmp_path)
    out = asyncio.run(ct.run(ct.Params(
        name="mathify", description="do math step by step", tools=["calculator"],
        procedure="1. Parse. 2. calculator. 3. Answer.")))
    assert "created" in out.lower()
    assert sk.get("mathify") is not None
    assert (tmp_path / "mathify.md").exists()
    # the written file re-loads as a valid skill
    sk2 = SkillRegistry(); sk2.load_dir(str(tmp_path))
    assert sk2.get("mathify").tools == ["calculator"]


def test_create_skill_rejects_unknown_tool(tmp_path):
    ct, sk = _make(tmp_path)
    out = asyncio.run(ct.run(ct.Params(
        name="bad", description="x", tools=["nonexistent_tool"], procedure="1. Go.")))
    assert "don't exist" in out.lower() and "nonexistent_tool" in out
    assert sk.get("bad") is None


def test_create_skill_requires_procedure(tmp_path):
    ct, sk = _make(tmp_path)
    out = asyncio.run(ct.run(ct.Params(
        name="empty", description="x", tools=[], procedure="   ")))
    assert "error" in out.lower()
    assert sk.get("empty") is None


def test_create_skill_recreate_replaces(tmp_path):
    """Re-creating a skill with the same name UPDATES it (so the model can fix a skill instead
    of spawning duplicate names)."""
    ct, sk = _make(tmp_path)
    asyncio.run(ct.run(ct.Params(name="dup", description="v1", tools=[], procedure="1. First.")))
    out = asyncio.run(ct.run(ct.Params(name="dup", description="v2", tools=[], procedure="1. Second.")))
    assert "updated" in out.lower() and "already exists" not in out.lower()
    assert sk.get("dup").procedure == "1. Second."          # replaced, not duplicated
    sk2 = SkillRegistry(); sk2.load_dir(str(tmp_path))
    assert "Second" in sk2.get("dup").procedure


def test_create_skill_result_clarifies_not_a_tool(tmp_path):
    ct, sk = _make(tmp_path)
    out = asyncio.run(ct.run(ct.Params(
        name="proc", description="x", tools=["calculator"], procedure="1. Go.")))
    assert "not a tool" in out.lower() and "do not try to call" in out.lower()


def test_inspect_skill_shows_definition(tmp_path):
    ct, sk = _make(tmp_path)
    asyncio.run(ct.run(ct.Params(
        name="mathify", description="do math", tools=["calculator"], procedure="1. Parse. 2. calc.")))
    it = InspectSkillTool(sk)
    out = asyncio.run(it.run(it.Params(name="mathify")))
    assert "mathify" in out and "do math" in out and "calculator" in out and "Parse" in out


def test_inspect_skill_missing(tmp_path):
    ct, sk = _make(tmp_path)
    it = InspectSkillTool(sk)
    out = asyncio.run(it.run(it.Params(name="ghost")))
    assert "no skill" in out.lower() and "ghost" in out


def test_delete_skill_removes_created(tmp_path):
    ct, sk = _make(tmp_path)
    asyncio.run(ct.run(ct.Params(name="temp", description="x", tools=[], procedure="1. Go.")))
    assert sk.get("temp") is not None and (tmp_path / "temp.md").exists()
    dt = DeleteSkillTool(sk, str(tmp_path))
    out = asyncio.run(dt.run(dt.Params(name="temp")))
    assert "deleted" in out.lower()
    assert sk.get("temp") is None
    assert not (tmp_path / "temp.md").exists()


def test_delete_skill_missing(tmp_path):
    ct, sk = _make(tmp_path)
    dt = DeleteSkillTool(sk, str(tmp_path))
    out = asyncio.run(dt.run(dt.Params(name="ghost")))
    assert "no skill" in out.lower()


def test_delete_skill_protects_builtin(tmp_path):
    """A skill whose file lives OUTSIDE the created-skills dir (a built-in) can't be deleted."""
    ct, sk = _make(tmp_path)
    from engine.skills.base import Skill
    sk.register(Skill(name="builtin_one", description="x", tools=[], procedure="1. Go.",
                      path="/some/other/library/builtin_one.md"))
    dt = DeleteSkillTool(sk, str(tmp_path))
    out = asyncio.run(dt.run(dt.Params(name="builtin_one")))
    assert "built-in" in out.lower() and "can't be deleted" in out.lower()
    assert sk.get("builtin_one") is not None          # still there
