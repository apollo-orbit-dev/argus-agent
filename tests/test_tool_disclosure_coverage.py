"""Pre-flight for progressive tool disclosure: does the view still CONTAIN the tools the work needs?

This is the highest-value test in the feature and it costs zero model calls. Disclosure has exactly
one failure mode today's system cannot produce — the turn needs a tool that was never advertised —
and the benchmark would surface it only as a mysterious per-task regression after an expensive run.
Here, every cap-2 task's required tool chain is checked against the tool set `select_visible` would
actually hand the model for that task's prompt.

K and CORE are read from `Config`'s FIELD DEFAULTS (`Config.model_fields[...].default`), not from a
`Config()` instance — an instance resolves .env -> OS env -> default, so it reads whatever THIS
machine happens to have configured (e.g. an operator's uncommented `TOOL_DISCLOSURE_K` in their own
.env), not what actually ships. Reading the field default is what makes this test immune to drift:
a future default change in config.py is measured here automatically, and a local/deployed override
can never turn this suite red.

HISTORY: at K=12 (the original shipped default, pre enriched-descriptions), 30 of the 56 cap-2 tasks
lost a tool their chain needed — read_file in all 30. That was a strict xfail here (see argus-89t):
raising K alone needed ~60 of ~70 tools before it went green (no real cut), so the fix was enriching
the tool descriptions the keyword ranker scores (desc-exp, merged to main) and, on that improved
signal, moving the default to K=40 — the smallest K on the measured sweep with zero coverage misses
(52/52 chain tasks, 43% of the catalog hidden). The gap is now closed and the assertion is unrelaxed:
this test PASSES for real at the shipped default, no xfail.

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
K = Config.model_fields["tool_disclosure_k"].default
CORE = Config.model_fields["tool_disclosure_core"].default.split(",")

# The measured state of the world at the shipped default (K=40, enriched descriptions): zero cap-2
# tasks lose a required tool. Recorded as a set (not just a count) so a regression that swaps WHICH
# task fails — same count, different task — cannot hide behind a `<= N` bound.
BASELINE_FAILING_TASKS = frozenset()
assert len(BASELINE_FAILING_TASKS) == 0


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


def _miss_ids(registry: ToolRegistry, **kw) -> set[str]:
    """Just the task ids from `_misses`, for set comparison against BASELINE_FAILING_TASKS."""
    return {line.split(" (tier")[0] for line in _misses(registry, **kw)}


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
    """The gap above is a known, recorded set of task ids — not just a count — so this test catches
    BOTH growth (more failing tasks than recorded) AND a silent partial regression (a fix closes some
    of the 30 while a DIFFERENT, previously-passing task starts failing; same or smaller count, but a
    task outside BASELINE_FAILING_TASKS now misses a tool it needs). A `<= 30` count bound would let
    that swap hide. A fixer that closes part of the gap must edit BASELINE_FAILING_TASKS down to the
    tasks they actually fixed — this test does not go green on its own."""
    registry = await _full_run_registry(tmp_path)
    miss_ids = _miss_ids(registry)
    new = miss_ids - BASELINE_FAILING_TASKS
    assert not new, (
        f"tool-disclosure coverage REGRESSED: {len(new)} task(s) now lose a required tool that are "
        f"NOT in the recorded baseline (possibly masked by other tasks improving): {sorted(new)}")


async def test_pinning_closes_the_coverage_gap(tmp_path):
    """Pinning is the mechanism a skill relies on, and it must be absolute: a name in `pinned` is
    never truncated by K, whatever the ranker thinks. This does NOT remedy the measured gap above —
    nothing pins read_file for a cap-2 task like "how many rows are in sales.csv", and none of the
    56 cap-2 tasks are skill-driven — it only proves the mechanism itself has no leak, for the
    caller (e.g. a skill's declared tools) that actually uses it."""
    registry = await _full_run_registry(tmp_path)
    assert _misses(registry, pin_chain=True) == []


async def test_view_is_actually_a_cut(tmp_path):
    """The coverage numbers above only mean something if the view is genuinely smaller than the
    registry — a 'view' that hides nothing would pass trivially."""
    registry = await _full_run_registry(tmp_path)
    total = len(registry.names())
    # At the shipped default (K=40 of ~70), the cut is real (~43% hidden) but no longer a 2x margin
    # the way K=12 was (2*K=80 > 70 tools is unsatisfiable) — assert the view hides a MEANINGFUL
    # fraction of the registry instead. `total > K` alone only guarantees one tool is hidden: it
    # would happily pass if someone raised K to 69 specifically to make a future coverage miss
    # disappear, and the suite would report "zero coverage misses" for a view one tool narrower
    # than the full registry, proving nothing. Requiring >=25% hidden keeps that regression caught
    # while still being satisfiable at the shipped default (30/70 = 43%).
    hidden = total - K
    assert hidden >= 0.25 * total, (
        f"K={K} of {total} hides only {hidden} tools ({100*hidden/total:.0f}%) — "
        f"coverage numbers measured against a view this close to the full registry prove nothing")
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
