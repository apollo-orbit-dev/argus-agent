import asyncio

from engine.skills.base import Skill, SkillRegistry, get_selector
from engine.skills.selection.explicit import ExplicitSelector
from engine.skills.selection.model_driven import LoadSkillTool, ModelDrivenSelector


def registry_with_research():
    reg = SkillRegistry()
    reg.register(Skill(name="research",
                       description="Answer a factual question by searching the web and reading a source.",
                       tools=["web_search", "fetch_page"],
                       procedure="1. Search. 2. Fetch. 3. Answer."))
    return reg


# ---- explicit ----

def test_explicit_direct_invocation():
    sel = ExplicitSelector(registry_with_research())
    ctx = sel.prepare("s", "anything at all", requested_skill="research")
    assert ctx.active_skill == "research"
    assert "1. Search." in ctx.system_additions
    assert ctx.extra_tools == []


def test_explicit_prematch_on_keywords():
    sel = ExplicitSelector(registry_with_research())
    ctx = sel.prepare("s", "Please research the current world population and give a source.", None)
    assert ctx.active_skill == "research"


def test_explicit_activates_research_on_trigger_topic_question():
    # regression: the canonical failing case "causes of the 2008 crisis" now activates
    from engine.skills.base import SkillRegistry
    from pathlib import Path
    reg = SkillRegistry()
    reg.load_dir(str(Path(__file__).resolve().parents[1] / "engine" / "skills" / "library"))
    sel = ExplicitSelector(reg)
    ctx = sel.prepare("s", "What were the main causes of the 2008 financial crisis?", None)
    assert ctx.active_skill == "research"


def test_explicit_url_activates_summarize():
    from engine.skills.base import SkillRegistry
    from pathlib import Path
    reg = SkillRegistry()
    reg.load_dir(str(Path(__file__).resolve().parents[1] / "engine" / "skills" / "library"))
    sel = ExplicitSelector(reg)
    ctx = sel.prepare("s", "summarize https://example.com/article for me", None)
    assert ctx.active_skill == "summarize_url"


def test_bare_url_does_not_activate_summarize():
    """A URL in a build-a-tool request must NOT hijack it into summarize mode (regression:
    the model research-spiraled because summarize_url fired on the yfinance PyPI link)."""
    from engine.skills.base import SkillRegistry
    from pathlib import Path
    reg = SkillRegistry()
    reg.load_dir(str(Path(__file__).resolve().parents[1] / "engine" / "skills" / "library"))
    sel = ExplicitSelector(reg)
    ctx = sel.prepare(
        "s", "Make a tool using the yfinance library (https://pypi.org/project/yfinance/)",
        None)
    assert ctx.active_skill != "summarize_url"


def test_table_skills_do_not_overfire_on_lookalike_words():
    """Regression: design_table's triggers are substring-matched, so 'table for this' used to fire
    inside 'comfortable for this' / 'presentable for this'. Triggers must be table-anchored and NOT
    hijack ordinary requests that merely contain a word ending in '-table'."""
    from engine.skills.base import SkillRegistry
    from pathlib import Path
    reg = SkillRegistry()
    reg.load_dir(str(Path(__file__).resolve().parents[1] / "engine" / "skills" / "library"))
    sel = ExplicitSelector(reg)
    for text in ("is this comfortable for this weather?",
                 "make this presentable for this meeting",
                 "is that acceptable for this project?"):
        ctx = sel.prepare("s", text, None)
        assert ctx.active_skill not in ("design_table", "extract_to_table"), text


def test_put_x_in_a_table_activates_design_table():
    """'in a table' trigger catches natural 'put/track X in a table' phrasings (coverage gap fix),
    and must not be swallowed by extract_to_table's 'into a table'."""
    from engine.skills.base import SkillRegistry
    from pathlib import Path
    reg = SkillRegistry()
    reg.load_dir(str(Path(__file__).resolve().parents[1] / "engine" / "skills" / "library"))
    sel = ExplicitSelector(reg)
    ctx = sel.prepare("s", "put these expenses in a table for me", None)
    assert ctx.active_skill == "design_table"


def test_explicit_no_match_returns_empty():
    sel = ExplicitSelector(registry_with_research())
    ctx = sel.prepare("s", "hello there", None)
    assert ctx.active_skill is None and ctx.system_additions == ""


def test_explicit_unknown_requested_skill_falls_through():
    sel = ExplicitSelector(registry_with_research())
    ctx = sel.prepare("s", "hello", requested_skill="nonexistent")
    assert ctx.active_skill is None


# ---- model_driven ----

def test_model_driven_lists_skills_and_provides_load_tool():
    sel = ModelDrivenSelector(registry_with_research())
    ctx = sel.prepare("s", "what is the population of France?", None)
    assert "research" in ctx.system_additions
    assert "load_skill" in ctx.system_additions
    assert any(isinstance(t, LoadSkillTool) for t in ctx.extra_tools)
    assert ctx.active_skill is None  # the model decides, not the selector


def test_model_driven_empty_registry_returns_empty():
    ctx = ModelDrivenSelector(SkillRegistry()).prepare("s", "hi", None)
    assert ctx.system_additions == "" and ctx.extra_tools == []


def test_load_skill_tool_returns_procedure():
    tool = LoadSkillTool(registry_with_research())
    out = asyncio.run(tool.run(tool.Params(name="research")))
    assert "1. Search." in out
    err = asyncio.run(tool.run(tool.Params(name="nope")))
    assert "error" in err.lower()


# ---- both selectors share the SkillContext type (loop treats them identically) ----

def test_all_selectors_return_skillcontext():
    reg = registry_with_research()
    for mode in ("explicit", "model_driven", "hybrid"):
        sel = get_selector(mode, reg)
        ctx = sel.prepare("s", "research something", None)
        assert hasattr(ctx, "system_additions") and hasattr(ctx, "extra_tools")


def test_hybrid_uses_explicit_when_trigger_fires():
    from pathlib import Path
    from engine.skills.base import SkillRegistry
    reg = SkillRegistry()
    reg.load_dir(str(Path(__file__).resolve().parents[1] / "engine" / "skills" / "library"))
    sel = get_selector("hybrid", reg)
    ctx = sel.prepare("s", "What were the main causes of the 2008 financial crisis?", None)
    assert ctx.active_skill == "research"          # explicit trigger path
    assert not any(t.name == "load_skill" for t in ctx.extra_tools)


def test_hybrid_falls_back_to_model_driven_when_no_trigger():
    sel = get_selector("hybrid", registry_with_research())
    ctx = sel.prepare("s", "hmm tell me something interesting", None)
    assert ctx.active_skill is None                # no trigger -> model_driven fallback
    from engine.skills.selection.model_driven import LoadSkillTool
    assert any(isinstance(t, LoadSkillTool) for t in ctx.extra_tools)
    assert "load_skill" in ctx.system_additions


# ---- progressive tool disclosure x skills ----
# Skill activation happens BEFORE the per-turn tool view is computed (selector.prepare runs earlier
# in run_task than the registry block), so a trigger-activated skill's declared tools can be PINNED
# and never compete for K. A skill loaded MID-turn via load_skill can't be — hence reveal().

def _tool_registry_with(*names):
    from pydantic import BaseModel
    from engine.tools.base import Tool, ToolRegistry
    reg = ToolRegistry()
    for n in names:
        reg.register(type(n, (Tool,), {
            "name": n, "description": f"the {n} tool",
            "Params": type("P", (BaseModel,), {}),
            "run": lambda self, args: None})())
    return reg


def test_skill_declared_tools_are_pinned_despite_zero_lexical_overlap():
    from engine.tools.disclosure import select_visible
    skill = registry_with_research().get("research")
    # fillers first, so insertion order (the tiebreak) puts the skill's tools LAST — without
    # pinning they lose the cut outright.
    reg = _tool_registry_with(*[f"filler{i}" for i in range(10)], "web_search", "fetch_page")
    prompt = "zzz qqq"                     # nothing whatsoever matches web_search / fetch_page
    assert not (set(skill.tools) & select_visible(reg, prompt, k=4))
    pinned = select_visible(reg, prompt, k=4, pinned=skill.tools)
    assert set(skill.tools) <= pinned


def test_load_skill_reveals_its_declared_tools():
    from engine.tools.disclosure import DisclosedRegistry
    full = _tool_registry_with("web_search", "fetch_page", "other")
    view = DisclosedRegistry(full, ["other"])
    tool = LoadSkillTool(registry_with_research())
    tool.disclosure = view
    out = asyncio.run(tool.run(tool.Params(name="research")))
    assert "1. Search." in out
    assert set(view.visible_names()) == {"web_search", "fetch_page", "other"}


def test_load_skill_without_disclosure_is_unchanged():
    tool = LoadSkillTool(registry_with_research())
    assert tool.disclosure is None
    out = asyncio.run(tool.run(tool.Params(name="research")))
    assert "1. Search." in out


def test_inspect_skill_reveals_its_declared_tools():
    from engine.experimental.skill_creation import InspectSkillTool
    from engine.tools.disclosure import DisclosedRegistry
    full = _tool_registry_with("web_search", "fetch_page", "other")
    view = DisclosedRegistry(full, ["other"])
    tool = InspectSkillTool(registry_with_research())
    tool.disclosure = view
    out = asyncio.run(tool.run(tool.Params(name="research")))
    assert "1. Search." in out
    assert set(view.visible_names()) == {"web_search", "fetch_page", "other"}


def test_reveal_skill_tools_is_a_silent_noop_without_a_view():
    from engine.skills.base import reveal_skill_tools
    skill = registry_with_research().get("research")
    assert reveal_skill_tools(None, skill) == []
    assert reveal_skill_tools(object(), skill) == []


def test_tokenizer_moved_but_stays_bound_in_explicit():
    """The layering fix: _STOP/_tokens now live in engine/textmatch.py so engine/tools/ can share
    them without importing from engine/skills/. Both names stay bound here, unchanged."""
    from engine.skills.selection import explicit
    from engine import textmatch
    assert explicit._tokens is textmatch.tokens
    assert explicit._STOP is textmatch.STOP_WORDS
    assert explicit._tokens("Please research the World population") == {"please", "research", "world", "population"}
