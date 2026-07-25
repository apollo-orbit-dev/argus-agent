"""Progressive tool disclosure — the pure ranker and the registry VIEW.

The invariant everything else rests on: disclosure narrows PRESENTATION ONLY. A hidden tool is
still gettable, still validatable, still executable — it is merely not advertised this turn. Half
the tests below exist to pin that down, because filtering dispatch instead would be a different and
far more dangerous feature that would still pass a naive "the schema got smaller" test.
"""
import pytest
from pydantic import BaseModel, Field

from engine.tools.base import Tool, ToolRegistry
from engine.tools.disclosure import (DisclosedRegistry, FindToolTool, cosine, embed_tool_docs,
                                     rank_tools, score_tool, select_visible, tool_doc)
from engine.textmatch import tokens


class Alpha(Tool):
    name = "alpha_tool"
    description = "Does alpha things with widgets and sprockets."

    class Params(BaseModel):
        x: str = Field("", description="a widget name")

    async def run(self, args):
        return "alpha"


def _tool(nm, desc, param=None, pdesc=""):
    ns = {"name": nm, "description": desc,
          "Params": type("P", (BaseModel,), {"__annotations__": {}}),
          "run": lambda self, args: None}
    if param:
        ns["Params"] = type("P", (BaseModel,), {
            "__annotations__": {param: str},
            param: Field("", description=pdesc)})
    return type(nm.title().replace("_", ""), (Tool,), ns)()


def reg_of(*tools) -> ToolRegistry:
    r = ToolRegistry()
    for t in tools:
        r.register(t)
    return r


# --------------------------------------------------------------------------- score_tool


def test_tool_doc_includes_name_description_and_params():
    doc = tool_doc(Alpha())
    assert "alpha_tool" in doc and "sprockets" in doc
    assert "x" in doc.split() and "widget name" in doc


def test_name_hit_outranks_pure_overlap():
    """A verbatim tool name is the strong, precise signal (+5). No amount of descriptive overlap
    (capped at 2.0 by construction) may outrank it — same shape as the skill selector's triggers."""
    named = _tool("currency_convert", "convert money")
    overlapping = _tool("other_tool", "convert money between amounts today please")
    q = "please use currency_convert on this"
    qtok = tokens(q)
    assert score_tool(named, qtok, q.lower()) > score_tool(overlapping, qtok, q.lower())
    assert score_tool(named, qtok, q.lower()) >= 5.0


def test_name_hit_matches_underscores_as_spaces():
    t = _tool("unit_convert", "converts units")
    q = "can you unit convert 12 miles"
    assert score_tool(t, tokens(q), q.lower()) >= 5.0


def test_overlap_is_normalised_by_query_length_and_weighted_two():
    t = _tool("thing", "sprockets")
    q = "sprockets widgets"          # 2 content tokens, 1 matches
    assert score_tool(t, tokens(q), q.lower()) == pytest.approx(2.0 * 0.5)


def test_embedding_term_is_clamped_and_weighted_three():
    t = _tool("thing", "nothing in common")
    q = "zzz"
    assert score_tool(t, set(), q, emb=1.0) == pytest.approx(3.0)
    assert score_tool(t, set(), q, emb=99.0) == pytest.approx(3.0)     # clamped high
    assert score_tool(t, set(), q, emb=-4.0) == pytest.approx(0.0)     # clamped low


def test_score_tool_is_pure_and_repeatable():
    t = Alpha()
    q = "widgets"
    assert score_tool(t, tokens(q), q) == score_tool(t, tokens(q), q)


def test_ties_break_on_registry_insertion_order():
    """Every score equal -> the order the tools were registered in decides, so the same inputs
    always produce the same view. (A set-ordering or dict-hash tiebreak would make the A/B
    unreproducible.)"""
    a, b, c = _tool("aaa", "zzz"), _tool("bbb", "zzz"), _tool("ccc", "zzz")
    fwd = rank_tools(reg_of(a, b, c), "nothing matches here")
    rev = rank_tools(reg_of(c, b, a), "nothing matches here")
    assert [n for n, _ in fwd] == ["aaa", "bbb", "ccc"]
    assert [n for n, _ in rev] == ["ccc", "bbb", "aaa"]


def test_calculator_is_reachable_from_an_explicit_mention():
    from engine.tools.calculator import CalculatorTool
    from engine.tools.weather import WeatherTool
    r = reg_of(WeatherTool(), CalculatorTool())
    assert rank_tools(r, "use the calculator for 47 * 89")[0][0] == "calculator"


def test_arithmetic_prompt_reaches_calculator_only_via_the_core_set():
    """'What is 47 * 89?' carries NO usable lexical signal at all — every content token is a stop
    word or shorter than the tokenizer's floor. That is precisely why calculator is in the core
    set (spec §0) rather than something the ranker is trusted to retrieve."""
    from engine.tools.calculator import CalculatorTool
    from engine.tools.weather import WeatherTool
    assert tokens("What is 47 * 89?") == set()
    r = reg_of(WeatherTool(), CalculatorTool())
    assert "calculator" in select_visible(r, "What is 47 * 89?", k=1, core=["calculator"])


# --------------------------------------------------------------------------- select_visible


def _many(n=20):
    return reg_of(*[_tool(f"t{i:02d}", f"tool number {i}") for i in range(n)])


def test_select_visible_respects_k_exactly():
    r = _many(20)
    assert len(select_visible(r, "anything", k=12)) == 12
    assert len(select_visible(r, "anything", k=3)) == 3


def test_select_visible_returns_everything_when_registry_is_smaller_than_k():
    r = _many(4)
    assert select_visible(r, "anything", k=12) == set(r.names())


def test_core_names_are_always_present():
    r = _many(20)
    core = ["t19", "t18"]
    v = select_visible(r, "tool number 0", k=5, core=core)
    assert set(core) <= v and len(v) == 5


def test_pinned_names_survive_regardless_of_score():
    r = _many(20)
    v = select_visible(r, "tool number 0", k=5, pinned=["t17"])
    assert "t17" in v


def test_pins_win_over_k_when_they_exceed_the_budget():
    """K is a SOFT budget: a skill naming tools the model can't see is worse than a bigger schema
    block, so pins are never dropped to fit."""
    r = _many(20)
    pinned = ["t01", "t02", "t03", "t04", "t05", "t06"]
    v = select_visible(r, "anything", k=3, pinned=pinned)
    assert v == set(pinned)


def test_core_plus_pins_exactly_at_k_admits_nothing_else():
    r = _many(20)
    v = select_visible(r, "anything", k=3, core=["t01"], pinned=["t02", "t03"])
    assert v == {"t01", "t02", "t03"}


def test_unknown_core_and_pin_names_are_ignored_silently():
    """A gated-off dependency or a typo in TOOL_DISCLOSURE_CORE must not shrink the view or raise."""
    r = _many(20)
    v = select_visible(r, "anything", k=5, core=["nope", "t00"], pinned=["also_nope"])
    assert "t00" in v and len(v) == 5


def test_hybrid_without_an_embedder_is_byte_identical_to_keyword():
    """The degradation policy: hybrid falls back to keyword FOR THAT TURN when embeddings are
    unavailable — mirroring memory's semantic_recall=auto. Identical inputs, identical view."""
    r = _many(20)
    q = "tool number 7 please"
    for k in (3, 5, 12):
        assert select_visible(r, q, mode="hybrid", k=k) == select_visible(r, q, mode="keyword", k=k)
    assert rank_tools(r, q, mode="hybrid") == rank_tools(r, q, mode="keyword")


def test_embedding_mode_with_no_embeddings_also_degrades_to_keyword():
    r = _many(20)
    q = "tool number 7 please"
    assert rank_tools(r, q, mode="embedding") == rank_tools(r, q, mode="keyword")


def test_embedding_signal_can_outrank_lexical_overlap():
    a, b = _tool("aaa", "completely unrelated"), _tool("bbb", "tool number 7")
    r = reg_of(a, b)
    embs = {"aaa": [1.0, 0.0], "bbb": [0.0, 1.0]}
    ranked = rank_tools(r, "tool number 7", mode="hybrid", doc_embs=embs, query_emb=[1.0, 0.0])
    assert ranked[0][0] == "aaa"          # cosine 1.0 x3 beats the lexical hit on bbb


def test_embedding_mode_ignores_lexical_overlap_entirely():
    a, b = _tool("aaa", "completely unrelated"), _tool("bbb", "tool number 7")
    r = reg_of(a, b)
    embs = {"aaa": [1.0, 0.0], "bbb": [1.0, 0.0]}
    ranked = dict(rank_tools(r, "tool number 7", mode="embedding",
                             doc_embs=embs, query_emb=[1.0, 0.0]))
    assert ranked["aaa"] == pytest.approx(ranked["bbb"])   # equal cosine, overlap term suppressed


def test_cosine_handles_degenerate_vectors():
    assert cosine(None, [1.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 1.0]) == 0.0
    assert cosine([1.0, 2.0], [1.0]) == 0.0                # length mismatch, not a crash


# --------------------------------------------------------------------------- embed_tool_docs


class _Embedder:
    configured = True

    def __init__(self, fail=False):
        self.fail = fail
        self.batches = 0

    async def embed(self, texts):
        self.batches += 1
        if self.fail:
            raise RuntimeError("endpoint down")
        return [[float(len(t)), 1.0] for t in texts]

    async def embed_one(self, text):
        out = await self.embed([text])
        return out[0]


async def test_embed_tool_docs_returns_none_when_unconfigured():
    class Off:
        configured = False
    assert await embed_tool_docs(Off(), _many(3), "q", {}) == (None, None)
    assert await embed_tool_docs(None, _many(3), "q", {}) == (None, None)


async def test_embed_tool_docs_never_raises_on_failure():
    doc_embs, q = await embed_tool_docs(_Embedder(fail=True), _many(3), "q", {})
    assert doc_embs is None and q is None


async def test_tool_docs_are_batch_embedded_once_and_cached():
    """Only the QUERY should cost a request per turn; the docs are content-addressed by
    sha1(tool_doc), so a stable registry is embedded once per process."""
    r, cache, e = _many(4), {}, _Embedder()
    await embed_tool_docs(e, r, "first", cache)
    assert len(cache) == 4
    after_first = e.batches
    await embed_tool_docs(e, r, "second", cache)
    assert e.batches == after_first + 1          # the query only — no re-embedding of docs


# --------------------------------------------------------------------------- DisclosedRegistry


def _view(n=6, visible=("t00", "t01")):
    full = _many(n)
    return full, DisclosedRegistry(full, visible)


def test_schemas_show_only_visible_tools():
    full, view = _view()
    assert {f["function"]["name"] for f in view.openai_schema()} == {"t00", "t01"}
    assert "t05" not in view.text_schema()
    assert "t00" in view.text_schema()


def test_schemas_keep_registry_insertion_order():
    full = _many(6)
    view = DisclosedRegistry(full, ["t04", "t01", "t00"])
    assert [f["function"]["name"] for f in view.openai_schema()] == ["t00", "t01", "t04"]


def test_dispatch_surface_is_not_narrowed():
    """THE load-bearing invariant. names/list/get/validate must see EVERY tool, so a hidden tool
    the model names still runs and the unknown-tool error still tells the truth."""
    full, view = _view()
    assert set(view.names()) == set(full.names())
    assert len(view.list()) == len(full.list())
    assert view.get("t05") is not None
    assert view.validate("t05", {}).ok is True


def test_tools_dict_is_shared_by_reference_never_copied():
    full, view = _view()
    assert view._tools is full._tools


def test_unknown_tool_error_still_lists_every_tool():
    full, view = _view()
    err = view.validate("frobnicate", {}).error
    assert "unknown tool" in err
    assert "t05" in err and "t00" in err


def test_reveal_grows_the_schema_and_reports_what_changed():
    full, view = _view()
    assert view.reveal(["t03"]) == ["t03"]
    assert "t03" in {f["function"]["name"] for f in view.openai_schema()}
    assert view.reveal(["t03"]) == []                 # already visible -> nothing added
    assert view.reveal(["not_a_tool"]) == []          # unregistered -> nothing added


def test_visible_and_hidden_names_partition_the_registry():
    full, view = _view()
    assert set(view.visible_names()) | set(view.hidden_names()) == set(full.names())
    assert not set(view.visible_names()) & set(view.hidden_names())
    assert view.visible_names() == ["t00", "t01"]     # insertion order


def test_register_auto_reveals():
    """create_tool building a tool mid-turn must never produce something the model cannot see."""
    full, view = _view()
    view.register(Alpha())
    assert "alpha_tool" in {f["function"]["name"] for f in view.openai_schema()}
    assert full.get("alpha_tool") is not None         # and it lands in the shared dict


def test_view_is_a_tool_registry_so_no_signature_changes_are_needed():
    _full, view = _view()
    assert isinstance(view, ToolRegistry)


# --------------------------------------------------------------------------- find_tool


async def test_find_tool_reveals_matching_tools_and_returns_their_schemas():
    full = reg_of(Alpha(), _tool("currency_convert", "Convert an amount between currencies",
                                 "amount", "how much to convert"))
    view = DisclosedRegistry(full, [])
    ft = FindToolTool(view)
    out = await ft.run(FindToolTool.Params(query="convert currency"))
    assert "currency_convert" in out
    assert "amount" in out                                # argument schema is included
    assert "currency_convert" in view.visible_names()     # and it is now advertised


async def test_find_tool_empty_query_returns_the_full_catalog():
    full = _many(30)
    view = DisclosedRegistry(full, [])
    ft = FindToolTool(view)
    for q in ("", "all"):
        out = await ft.run(FindToolTool.Params(query=q))
        assert all(n in out for n in full.names())


async def test_find_tool_without_a_registry_explains_itself_instead_of_crashing():
    out = await FindToolTool().run(FindToolTool.Params(query="anything"))
    assert "error" in out.lower()


def test_find_tool_is_a_reserved_builtin_name():
    """Registered only when disclosure is on, so with disclosure off the name would otherwise be
    free for create_tool to squat — and would then be mistaken for the real escape hatch."""
    from engine.engine import GATED_BUILTIN_NAMES
    assert "find_tool" in GATED_BUILTIN_NAMES


# --------------------------------------------------------------------------- run_task wiring


async def _capture_registry(tmp_path, **cfg):
    """Run one turn with the loop stubbed out and hand back the registry LoopDeps actually got."""
    from config import Config
    from engine.engine import Engine
    import engine.engine as eng

    captured = {}

    async def fake_run_loop(deps, session_id, run_id, user_text, user_content=None):
        captured["deps"] = deps
        return "ok"

    real = eng.run_loop
    eng.run_loop = fake_run_loop
    try:
        e = Engine(Config(enable_action_verify=False, enable_memory_autoextract=False,
                          enable_auto_title_session=False, enable_rules_autodetect=False,
                          **cfg), data_dir=str(tmp_path))
        await e.run_task("s", cfg.pop("_prompt", "what is the weather in Paris?"))
    finally:
        eng.run_loop = real
    return e, captured["deps"]


async def test_off_parity_no_view_object_is_constructed(tmp_path):
    """THE default-safety test. With tool_disclosure_mode='off' the loop must receive a plain
    ToolRegistry — not a view configured to show everything — and the advertised schema must be
    byte-identical to the full registry's. Nothing about an existing deploy changes."""
    _e, deps = await _capture_registry(tmp_path, tool_disclosure_mode="off")
    assert not isinstance(deps.registry, DisclosedRegistry)
    assert type(deps.registry) is ToolRegistry
    assert "find_tool" not in deps.registry.names()
    full = ToolRegistry()
    for t in deps.registry.list():
        full.register(t)
    assert deps.registry.openai_schema() == full.openai_schema()


async def test_disclosure_on_wraps_the_registry_in_a_view(tmp_path):
    _e, deps = await _capture_registry(tmp_path, tool_disclosure_mode="keyword",
                                       tool_disclosure_k=8)
    assert isinstance(deps.registry, DisclosedRegistry)
    assert len(deps.registry.openai_schema()) == 8            # advertised
    assert len(deps.registry.names()) > 8                     # but everything still dispatchable


async def test_find_tool_is_registered_and_never_hidden(tmp_path):
    _e, deps = await _capture_registry(tmp_path, tool_disclosure_mode="keyword",
                                       tool_disclosure_k=6)
    assert "find_tool" in deps.registry.visible_names()
    assert isinstance(deps.registry.get("find_tool"), FindToolTool)
    # …and it was bound to the live view, so reveal() actually reaches this turn's schema
    assert deps.registry.get("find_tool").disclosure is deps.registry


async def test_configured_core_set_is_always_visible(tmp_path):
    _e, deps = await _capture_registry(tmp_path, tool_disclosure_mode="keyword",
                                       tool_disclosure_k=6)
    visible = set(deps.registry.visible_names())
    assert {"find_tool", "ask_user", "calculator", "get_current_time", "about_argus"} <= visible


async def test_disclosure_event_is_emitted_with_the_view(tmp_path):
    e, deps = await _capture_registry(tmp_path, tool_disclosure_mode="keyword",
                                      tool_disclosure_k=9)
    ev = [x for x in e.events.recent("s") if x.kind == "disclosure"]
    assert len(ev) == 1
    d = ev[0].data
    assert d["mode"] == "keyword" and d["k"] == 9
    assert sorted(deps.registry.visible_names()) == d["visible"]
    assert d["hidden"] == len(deps.registry.names()) - len(d["visible"])
    assert "find_tool" in d["pinned"]


async def test_no_disclosure_event_when_off(tmp_path):
    e, _deps = await _capture_registry(tmp_path, tool_disclosure_mode="off")
    assert not [x for x in e.events.recent("s") if x.kind == "disclosure"]


async def test_view_does_not_leak_into_the_engine_wide_registry(tmp_path):
    """The view shares `_tools` by REFERENCE, so it must wrap a per-run CLONE. If it wrapped the
    base registry, find_tool would leak into every later turn and into tools_overview()."""
    e, deps = await _capture_registry(tmp_path, tool_disclosure_mode="keyword")
    assert deps.registry._tools is not e.registry._tools
    assert "find_tool" not in e.registry.names()


async def test_load_skill_gets_the_view_bound_to_it(tmp_path):
    """model_driven selection puts load_skill in ctx.extra_tools; the rebinding pass must give it
    the view so a procedure it loads can reveal the tools it names."""
    _e, deps = await _capture_registry(tmp_path, tool_disclosure_mode="keyword",
                                       skill_selection_mode="model_driven")
    load = deps.registry.get("load_skill")
    assert load is not None
    assert load.disclosure is deps.registry
    # …and its SkillRegistry was NOT clobbered by the tool-registry rebinding pass
    from engine.skills.base import SkillRegistry
    assert isinstance(load.registry, SkillRegistry)


async def test_extra_tools_are_pinned(tmp_path):
    _e, deps = await _capture_registry(tmp_path, tool_disclosure_mode="keyword",
                                       tool_disclosure_k=6, skill_selection_mode="model_driven")
    assert "load_skill" in deps.registry.visible_names()
