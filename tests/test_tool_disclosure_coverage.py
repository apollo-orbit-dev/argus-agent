"""Pre-flight for progressive tool disclosure: does the view still CONTAIN the tools the work needs?

This is the highest-value test in the feature and it costs zero model calls. Disclosure has exactly
one failure mode today's system cannot produce — the turn needs a tool that was never advertised —
and the benchmark would surface it only as a mysterious per-task regression after an expensive run.
Here, every cap-2 task's required tool chain is checked against the tool set `select_visible` would
actually hand the model for that task's prompt.

WHAT IT FOUND (see the xfail below): at the shipped defaults (mode=keyword, K=12, core =
find_tool/ask_user/calculator/get_current_time/about_argus) only 26 of the 56 cap-2 tasks keep every
tool their chain needs. The assertion is NOT relaxed to accommodate that — the marker is strict, so
the day the ranker, the core set or K is fixed this test XPASSes and CI forces the marker's removal.

Do not weaken the assertion to make it pass. A miss is a finding about the core set, K, or the
ranker, and the fix is one of those three.
"""
import json
from pathlib import Path

import pytest

from config import Config
from engine.engine import Engine
from engine.tools.base import ToolRegistry
from engine.tools.disclosure import select_visible

BATTERY = Path("benchmark/cap-2/battery.json")
K = 12
CORE = "find_tool,ask_user,calculator,get_current_time,about_argus".split(",")

# The measured state of the world at the shipped defaults, recorded so the failure is a NUMBER a
# maintainer can act on rather than a wall of task ids. Keyword ranking has no lexical bridge from
# "how many rows are in sales.csv" to read_file (whose whole doc is 13 content words), so read_file
# alone accounts for 30 of the 30 failing tasks; insert_row (11), create_table (6), query_table (2),
# unit_convert (1) and write_file (1) follow. Raising K with the default core needs K≈60 of ~70
# tools before it goes green — i.e. no cut at all — so K is not the lever. Extending the core set to
# 13 names at K=16 does reach zero, but that is hand-fitting the core set to this battery, which
# would defeat the purpose of the pre-flight; it is a maintainer's call, not a silent default change.
BASELINE_FAILING_TASKS = 30


def _tasks() -> list[dict]:
    return json.loads(BATTERY.read_text())["tasks"]


def _chain_tasks() -> list[dict]:
    return [t for t in _tasks() if (t.get("expect") or {}).get("tools_in_order")]


async def _full_run_registry(tmp_path) -> ToolRegistry:
    """The exact tool set a cap-2 turn sees — the base registry plus run_task's per-run conditional
    block plus find_tool — captured from the REAL run_task rather than reassembled here, so this
    test can never drift from the wiring it guards.

    The web dependencies are configured so the Firecrawl/SearXNG tools exist: cap-2 chains name
    fetch_page, and a benchmark run against a deploy without them skips those tasks anyway.
    """
    captured = {}

    async def fake_run_loop(deps, session_id, run_id, user_text, user_content=None):
        captured["registry"] = deps.registry
        return "ok"

    import engine.engine as eng
    real = eng.run_loop
    eng.run_loop = fake_run_loop
    try:
        e = Engine(Config(searxng_base_url="http://searx.invalid",
                          firecrawl_base_url="http://firecrawl.invalid",
                          tool_disclosure_mode="keyword", tool_disclosure_k=K,
                          tool_disclosure_core=",".join(CORE),
                          enable_action_verify=False, enable_memory_autoextract=False,
                          enable_auto_title_session=False, enable_rules_autodetect=False),
                   data_dir=str(tmp_path))
        await e.run_task("s_probe", "hello")
    finally:
        eng.run_loop = real
    flat = ToolRegistry()
    for t in captured["registry"].list():   # list() is deliberately UNFILTERED — the full set
        flat.register(t)
    return flat


def _misses(registry: ToolRegistry, *, k=K, core=CORE, pin_chain=False) -> list[str]:
    known = set(registry.names())
    out = []
    for task in _chain_tasks():
        needed = [n for n in task["expect"]["tools_in_order"] if n in known]
        visible = select_visible(registry, task["prompt"], mode="keyword", k=k, core=core,
                                 pinned=needed if pin_chain else ())
        missing = [n for n in needed if n not in visible]
        if missing:
            out.append(f"{task['id']} (tier {task['tier']}): missing {sorted(set(missing))}")
    return out


@pytest.mark.xfail(strict=True, reason=(
    "MEASURED FINDING, not a flake: at mode=keyword / K=12 / the shipped core set, 30 of the 56 "
    "cap-2 tasks lose a tool their chain needs — read_file in all 30. Keyword overlap normalised by "
    "query length has no bridge from a prompt's vocabulary ('rows', 'sales.csv') to a terse tool doc, "
    "and it systematically favours tools with LONG descriptions. This is the ship-gate blocker in the "
    "spec's §8: disclosure must not be flipped on for a real deploy until the ranker (or the core "
    "set, or K) closes it. strict=True: fix it and this test XPASSes, which fails CI until the marker "
    "is removed."))
async def test_every_cap2_task_sees_its_required_tools(tmp_path):
    registry = await _full_run_registry(tmp_path)
    assert len(registry.names()) > K, "registry must be larger than K or this proves nothing"
    assert "find_tool" in registry.names()
    misses = _misses(registry)
    assert not misses, (
        f"{len(misses)} of {len(_tasks())} cap-2 tasks would run without a tool their chain needs. "
        f"This is THE invisible-tool failure. Fix the core set, K, or the ranker — not this "
        f"assertion.\n" + "\n".join(misses))


async def test_coverage_gap_has_not_grown(tmp_path):
    """The gap above is a known, recorded number. This test exists so it can only ever get SMALLER:
    a change that makes disclosure hide MORE required tools fails here immediately, instead of
    disappearing into an already-xfailing test."""
    registry = await _full_run_registry(tmp_path)
    misses = _misses(registry)
    assert len(misses) <= BASELINE_FAILING_TASKS, (
        f"tool-disclosure coverage REGRESSED: {len(misses)} cap-2 tasks now lose a required tool, "
        f"up from the recorded {BASELINE_FAILING_TASKS}.\n" + "\n".join(misses))


async def test_pinning_closes_the_coverage_gap(tmp_path):
    """Pinning is the mechanism a skill relies on, and it must be absolute: a skill that declares
    its tools gets ALL of them, whatever the ranker thinks and whatever K is. This is the working
    remedy for the gap above — and the reason a skill-driven turn is unaffected by it."""
    registry = await _full_run_registry(tmp_path)
    assert _misses(registry, pin_chain=True) == []


async def test_view_is_actually_a_cut(tmp_path):
    """The coverage numbers above only mean something if the view is genuinely smaller than the
    registry — a 'view' that hides nothing would pass trivially."""
    registry = await _full_run_registry(tmp_path)
    total = len(registry.names())
    assert total >= 2 * K, f"expected the full registry ({total}) to be well over K={K}"
    for task in _tasks()[:10]:
        visible = select_visible(registry, task["prompt"], mode="keyword", k=K, core=CORE, pinned=())
        assert len(visible) == K


@pytest.mark.skipif(not Config().embedding_base_url,
                    reason="no embedding endpoint configured (embedding_base_url unset)")
async def test_every_cap2_task_sees_its_required_tools_hybrid(tmp_path):
    """The same coverage guarantee on the hybrid signal — the arm most likely to CLOSE the keyword
    gap, since the misses are vocabulary mismatches rather than ranking noise. Skipped unless an
    embedding endpoint is really configured: the point is to exercise the ranker, not the fallback
    (which test_tool_disclosure.py already proves is byte-identical to keyword)."""
    from engine.memory.embeddings import EmbeddingClient
    from engine.tools.disclosure import embed_tool_docs

    c = Config()
    registry = await _full_run_registry(tmp_path)
    embedder = EmbeddingClient(c.embedding_base_url, c.embedding_model, c.embedding_api_key)
    cache: dict = {}
    known = set(registry.names())
    misses = []
    for task in _chain_tasks():
        needed = [n for n in task["expect"]["tools_in_order"] if n in known]
        doc_embs, query_emb = await embed_tool_docs(embedder, registry, task["prompt"], cache)
        visible = select_visible(registry, task["prompt"], mode="hybrid", k=K, core=CORE,
                                 pinned=(), doc_embs=doc_embs, query_emb=query_emb)
        missing = [n for n in needed if n not in visible]
        if missing:
            misses.append(f"{task['id']}: missing {sorted(set(missing))}")
    assert not misses, "hybrid ranking hides required tools:\n" + "\n".join(misses)
