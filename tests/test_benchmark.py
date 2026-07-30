"""Benchmark pure helpers: task_verdict, aggregate, build_series, resolve_config."""
import importlib.util

from engine.eval.benchmark import (_backfill_solved, aggregate, build_series, render_report,
                                    resolve_config, run_aborted, task_verdict, _write_result)


def _load_validator():
    spec = importlib.util.spec_from_file_location("vb", "benchmark/cap-2/validate_battery.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m


def test_cap2_battery_validates():
    vb = _load_validator()
    problems = vb.validate("benchmark/cap-2/battery.json")
    assert problems == [], "cap-2 battery invalid:\n" + "\n".join(problems)


def test_cap2_battery_is_complete_and_balanced():
    import json
    b = json.loads(open("benchmark/cap-2/battery.json").read())
    tasks = b["tasks"]
    assert len(tasks) == 56
    from collections import Counter
    fam = Counter(t["category"] for t in tasks)
    assert set(fam) == {"compute", "tool-selection", "retrieve", "data-transform", "synthesis", "restraint"}
    assert all(v >= 4 for v in fam.values()), f"a family is under-represented: {fam}"
    searxng = sum(1 for t in tasks if t.get("requires") == "searxng")
    assert searxng <= 2
    nodep = sum(1 for t in tasks if not t.get("requires"))
    assert nodep >= 45
    # every T3/T4 task has a real chain AND rubric (the cap-1 fix)
    for t in tasks:
        if t["tier"] in (3, 4):
            assert (t.get("expect") or {}).get("tools_in_order") and t.get("rubric")


def test_task_verdict_chain_threshold_and_judge_mean():
    # k=3, need >=ceil(1.8)=2 chain-correct to pass
    runs = [{"chain_correct": True, "judge_score": 3}, {"chain_correct": True, "judge_score": 2},
            {"chain_correct": False, "judge_score": 1}]
    v = task_verdict(runs, 3)
    assert v["chain_pass"] is True and v["judge_mean"] == 2.0


def test_task_verdict_below_threshold_fails():
    runs = [{"chain_correct": True}, {"chain_correct": False}, {"chain_correct": False}]
    assert task_verdict(runs, 3)["chain_pass"] is False


def test_task_verdict_judge_only_has_no_chain():
    runs = [{"judge_score": 3}, {"judge_score": 3}]
    v = task_verdict(runs, 2)
    assert v["chain_pass"] is None and v["judge_mean"] == 3.0


def test_aggregate_per_tier_and_skipped():
    tasks = [
        {"tier": 1, "chain_pass": True, "judge_mean": 3.0, "skipped": False},
        {"tier": 1, "chain_pass": False, "judge_mean": 2.0, "skipped": False},
        {"tier": 1, "chain_pass": None, "judge_mean": 1.0, "skipped": False},   # judge-only
        {"tier": 2, "chain_pass": None, "judge_mean": None, "skipped": True},    # skipped
    ]
    a = aggregate(tasks)
    t1 = a["per_tier"]["1"]
    assert t1["chain_pass"] == 0.5          # 1 of 2 chain-scored tasks passed
    assert t1["judge_mean"] == 2.0          # mean of 3,2,1
    assert t1["n"] == 3 and t1["skipped"] == 0
    t2 = a["per_tier"]["2"]
    assert t2["chain_pass"] is None and t2["n"] == 0 and t2["skipped"] == 1


def test_build_series_groups_by_params_sorted():
    results = [
        {"battery_version": "cap-1", "params": 35, "per_tier": {"1": {"chain_pass": 1.0, "judge_mean": 3.0},
                                                                 "3": {"chain_pass": 0.8, "judge_mean": 2.5}}},
        {"battery_version": "cap-1", "params": 3, "per_tier": {"1": {"chain_pass": 0.9, "judge_mean": 2.8},
                                                               "3": {"chain_pass": 0.1, "judge_mean": 0.5}}},
        {"battery_version": "old", "params": 7, "per_tier": {"1": {"chain_pass": 0.5, "judge_mean": 1.0}}},
    ]
    s = build_series(results, "cap-1")
    assert s["1"] == [(3, 0.9, 2.8), (35, 1.0, 3.0)]        # sorted by params, old version excluded
    assert s["3"] == [(3, 0.1, 0.5), (35, 0.8, 2.5)]


async def test_run_task_crashed_build_counts_as_chain_failure(monkeypatch):
    # a crashed run cell of a task WITH an `expect` must count as a chain failure, not silently drop
    from engine.eval import benchmark as B

    class _Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("engine build failed")

    monkeypatch.setattr("engine.engine.Engine", _Boom)
    task = {"id": "x", "tier": 1, "expect": {"tools_in_order": ["calculator"]}, "rubric": ["r"]}
    r = await B._run_task(cfg=None, judge_fn=None, task=task, k=2, timeout=1)
    assert r["skipped"] is False and r["chain_pass"] is False   # not None (silent drop)


def test_resolve_config_overrides():
    c = resolve_config("fast=http://host:8001/v1|qwen", "manual")
    assert c.model_base_url == "http://host:8001/v1" and c.model_name == "qwen"
    assert c.tool_calling_mode == "manual"
    d = resolve_config("main", None)                        # no override → configured defaults
    assert d.tool_calling_mode in ("native", "manual", "native_finish")


def test_solved_requires_chain_and_judge_ge_2():
    # k=3, threshold = ceil(3*0.6)=2. All 3 runs chain AND judge>=2 -> solved.
    runs = [{"chain_correct": True, "judge_score": 3}] * 3
    assert task_verdict(runs, 3)["solved"] is True


def test_solved_false_when_judged_below_2_despite_chain():
    # chains every time but judge is 1 -> not solved (good tools, bad answer)
    runs = [{"chain_correct": True, "judge_score": 1}] * 3
    v = task_verdict(runs, 3)
    assert v["chain_pass"] is True and v["solved"] is False


def test_solved_false_when_judge_ok_but_chain_wrong():
    runs = [{"chain_correct": False, "judge_score": 3}] * 3
    assert task_verdict(runs, 3)["solved"] is False


def test_solved_threshold_2_of_3():
    runs = [{"chain_correct": True, "judge_score": 2},
            {"chain_correct": True, "judge_score": 2},
            {"chain_correct": False, "judge_score": 0}]
    assert task_verdict(runs, 3)["solved"] is True   # 2 of 3 solved >= ceil(1.8)=2


def test_solved_judge_only_task_uses_judge_alone():
    # no chain (judge-only, chain_correct None): solved == judge>=2, chain vacuous
    runs = [{"chain_correct": None, "judge_score": 3}] * 3
    v = task_verdict(runs, 3)
    assert v["chain_pass"] is None and v["solved"] is True


def test_aggregate_rolls_up_solved():
    tasks = [{"tier": 1, "chain_pass": True, "judge_mean": 3.0, "solved": True, "skipped": False},
             {"tier": 1, "chain_pass": True, "judge_mean": 1.0, "solved": False, "skipped": False}]
    agg = aggregate(tasks)
    assert agg["per_tier"]["1"]["solved"] == 0.5 and agg["overall"]["solved"] == 0.5


def test_run_aborted_only_on_stuck_repeating():
    assert run_aborted(["repeat_nudge", "stuck_repeating"]) is True
    assert run_aborted(["repeat_nudge"]) is False
    assert run_aborted(["fuzzy_repeat_nudge", "create_without_verify"]) is False
    assert run_aborted([]) is False
    assert run_aborted(None) is False


def test_task_verdict_abort_rate_is_fraction_of_k():
    runs = [{"chain_correct": True, "judge_score": 3, "observer": ["stuck_repeating"], "aborted": True},
            {"chain_correct": True, "judge_score": 3, "observer": [], "aborted": False},
            {"chain_correct": True, "judge_score": 3, "observer": [], "aborted": False}]
    assert task_verdict(runs, 3)["abort_rate"] == 1 / 3

    clean_runs = [{"chain_correct": True, "judge_score": 3, "observer": ["repeat_nudge"], "aborted": False}] * 3
    v = task_verdict(clean_runs, 3)
    assert v["abort_rate"] == 0.0    # measured clean, distinct from None (no data)


def test_task_verdict_abort_rate_observer_only_fallback():
    # no "aborted" key at all -- must fall back to deriving it from "observer" (benchmark.py:66-67)
    runs = [{"chain_correct": True, "judge_score": 3, "observer": ["stuck_repeating"]},
            {"chain_correct": True, "judge_score": 3, "observer": []},
            {"chain_correct": True, "judge_score": 3, "observer": []}]
    assert task_verdict(runs, 3)["abort_rate"] == 1 / 3


def test_abort_rate_none_when_no_observer_data():
    # legacy-shaped runs: no "observer" and no "aborted" key at all
    runs = [{"chain_correct": True, "judge_score": 3}] * 3
    assert task_verdict(runs, 3)["abort_rate"] is None


def test_abort_does_not_affect_solved():
    runs = [{"chain_correct": True, "judge_score": 3, "observer": ["stuck_repeating"], "aborted": True}] * 3
    v = task_verdict(runs, 3)
    assert v["solved"] is True and v["chain_pass"] is True
    assert v["abort_rate"] == 1.0


def test_aggregate_rolls_up_abort_rate():
    tasks = [{"tier": 1, "chain_pass": True, "judge_mean": 3.0, "solved": True, "abort_rate": 1.0, "skipped": False},
             {"tier": 1, "chain_pass": True, "judge_mean": 3.0, "solved": True, "abort_rate": 0.0, "skipped": False}]
    agg = aggregate(tasks)
    assert agg["per_tier"]["1"]["abort_rate"] == 0.5

    all_none = [{"tier": 2, "chain_pass": True, "judge_mean": 3.0, "solved": True, "abort_rate": None, "skipped": False}]
    agg2 = aggregate(all_none)
    assert agg2["per_tier"]["2"]["abort_rate"] is None

    with_skipped = tasks + [{"tier": 1, "chain_pass": None, "judge_mean": None, "solved": None,
                              "abort_rate": None, "skipped": True}]
    agg3 = aggregate(with_skipped)
    assert agg3["per_tier"]["1"]["abort_rate"] == 0.5   # skipped task excluded, not counted as 0


def _fake_result(model, params, per_tier_solved):
    # a minimal result with runs so solved is derivable; one task per tier
    tasks = []
    for tier, solved in per_tier_solved.items():
        runs = [{"chain_correct": True, "judge_score": 3 if solved else 1}] * 3
        tasks.append({"id": f"x{tier}", "tier": tier, "category": "compute",
                      "skipped": False, "chain_pass": True, "judge_mean": 3.0 if solved else 1.0, "runs": runs})
    return {"model": model, "params": params, "mode": "native", "scaffold": "on",
            "max_tokens": 2048, "battery_version": "cap-1", "k": 3,
            "date": "2026-01-01T00:00:00+00:00",
            "per_tier": {}, "overall": {}, "tasks": tasks}


def test_backfill_solved_derives_from_runs_when_missing():
    r = _fake_result("m", 3, {1: True, 2: False})
    out = _backfill_solved(r)
    assert out["per_tier"]["1"]["solved"] == 1.0
    assert out["per_tier"]["2"]["solved"] == 0.0
    assert out["overall"]["solved"] == 0.5


def test_backfill_solved_leaves_abort_rate_none():
    # _fake_result's runs are legacy-shaped (no observer/aborted key) -> nothing to derive abort_rate from
    r = _fake_result("m", 3, {1: True, 2: False})
    out = _backfill_solved(r)
    assert out["overall"]["abort_rate"] is None


def test_render_report_has_solved_column():
    # render on a battery version present via _load_results is integration-heavy; test the string builder
    # by monkeypatching _load_results through a written file is overkill — assert the header names solved.
    from engine.eval import benchmark as B
    md, ok = B.render_report("cap-1")  # cap-1 results exist in the repo
    assert ok and "solved" in md.splitlines()[4].lower()  # header row includes the column


def _report_cols(md):
    """Header name -> cell index. Tests must NOT hardcode a column position: the report gains
    columns over time (disclosure, answered, then valid/gap/shape for argus-2oj), and a positional
    index either breaks or — worse — silently starts asserting on a different column."""
    return {c.strip().lower(): i for i, c in enumerate(md.splitlines()[4].split("|"))}


def test_render_report_has_abort_column():
    from engine.eval import benchmark as B
    md, ok = B.render_report("cap-1")  # cap-1 results exist in the repo (all pre-argus-92a: legacy)
    assert ok
    header = md.splitlines()[4].lower()
    assert "abort" in header
    body_lines = [l for l in md.splitlines()[6:] if l.startswith("|")]
    assert body_lines, "expected at least one result row"
    # render_report emits one row per result, in the same params-sorted order _load_results/render_report
    # use internally -- zip them up so we can tell which rows are legacy (no observer data ever
    # recorded) vs. a future post-argus-92a result, and only apply the strict —/n/a assertion to the
    # legacy ones. Asserting it for EVERY row is a time bomb: it breaks the moment a real abort-rate
    # result (a genuine percentage, not — or n/a) is committed to benchmark/results/.
    results = [r for r in B._load_results() if r.get("battery_version") == "cap-1"]
    results.sort(key=lambda r: r.get("params", 0))
    assert len(body_lines) == len(results)
    for line, r in zip(body_lines, results):
        cells = [c.strip() for c in line.split("|")]
        abort_cell = cells[_report_cols(md)["abort"]]
        has_observer_data = r.get("scaffold") != "off" and any(
            "aborted" in run or "observer" in run
            for t in r.get("tasks", []) for run in t.get("runs", []))
        assert abort_cell != "0%" or has_observer_data, \
            f"legacy row must not show a fabricated 0%: {line}"
        if not has_observer_data:
            assert abort_cell in ("—", "n/a"), f"legacy row should render — or n/a, got {abort_cell!r}: {line}"


def test_build_series_metric_solved():
    r = _backfill_solved(_fake_result("m", 3, {1: True}))
    s = build_series([r], "cap-1", metric="solved")
    assert s["1"][0][1] == 1.0   # (params, value, judge) — value is the solved rate for metric=solved


def test_fixtures_resolve_beside_the_battery():
    """Regression (cap-2 pilot): the runner copies a task's `source` fixture from a `fixtures/` dir
    BESIDE the battery file, so every battery in its own subdir uses its own fixtures. Each battery
    now lives in its own sibling folder (benchmark/cap-1/, benchmark/cap-2/), so cap-1's battery at
    benchmark/cap-1/battery.json maps to benchmark/cap-1/fixtures/ (== the default FIXTURES), and
    cap-2's to benchmark/cap-2/fixtures/ beside it — NOT the cap-1 location."""
    from pathlib import Path
    from engine.eval.benchmark import FIXTURES
    assert (Path("benchmark/cap-1/battery.json").parent / "fixtures").resolve() == FIXTURES.resolve()
    assert (Path("benchmark/cap-2/battery.json").parent / "fixtures") == Path("benchmark/cap-2/fixtures")
    assert Path("benchmark/cap-2/battery.json").parent / "fixtures" != FIXTURES


# ---- progressive tool disclosure arm ----

def test_resolve_config_sets_the_disclosure_mode():
    assert resolve_config("main", None, disclosure="hybrid").tool_disclosure_mode == "hybrid"
    assert resolve_config("main", None, disclosure="off").tool_disclosure_mode == "off"


def test_resolve_config_leaves_disclosure_alone_by_default():
    from config import Config
    assert resolve_config("main", None).tool_disclosure_mode == Config().tool_disclosure_mode


def test_disclosure_is_its_own_axis_not_scaffolding():
    """--baseline must not move the disclosure arm: they are independent axes, so a baseline run
    and a disclosure run stay comparable."""
    from engine.eval.benchmark import BASELINE_OVERRIDES
    assert "tool_disclosure_mode" not in BASELINE_OVERRIDES
    assert resolve_config("main", None, baseline=True, disclosure="keyword").tool_disclosure_mode == "keyword"


# ---- `answered`: the second headline metric (argus-3zn) ----

def test_answered_is_true_when_judge_approves_but_the_chain_failed():
    """THE 3zn CASE IN ONE ASSERTION: a reasoning model answers a trivial task inside its reasoning
    pass, so judge=3 with an EMPTY tool list. solved stays False (strict, unchanged); answered is
    True — that gap is the quantity this metric exists to expose."""
    runs = [{"chain_correct": False, "judge_score": 3}] * 3
    v = task_verdict(runs, 3)
    assert v["solved"] is False and v["chain_pass"] is False
    assert v["answered"] is True


def test_answered_is_none_not_true_when_the_task_has_no_judge_verdict():
    """DELIBERATE ASYMMETRY with solved, which treats a missing judge score as vacuously true.
    answered IS the judge axis: no judge means no verdict, and vacuous-true would inflate it."""
    runs = [{"chain_correct": True}] * 3
    v = task_verdict(runs, 3)
    assert v["solved"] is True        # unchanged: judge vacuously ok
    assert v["answered"] is None      # NOT True


def test_answered_honours_the_collapse_threshold():
    # k=3, threshold = ceil(3*0.6) = 2 -- 1 of 3 judged-good is not answered
    one_of_three = [{"chain_correct": False, "judge_score": 3},
                    {"chain_correct": False, "judge_score": 1},
                    {"chain_correct": False, "judge_score": 0}]
    assert task_verdict(one_of_three, 3)["answered"] is False
    two_of_three = [{"chain_correct": False, "judge_score": 3},
                    {"chain_correct": False, "judge_score": 2},
                    {"chain_correct": False, "judge_score": 0}]
    assert task_verdict(two_of_three, 3)["answered"] is True


def test_aggregate_rolls_up_answered_skipping_none():
    tasks = [{"tier": 1, "chain_pass": False, "judge_mean": 3.0, "solved": False, "answered": True,
              "skipped": False},
             {"tier": 1, "chain_pass": True, "judge_mean": 1.0, "solved": False, "answered": False,
              "skipped": False},
             {"tier": 1, "chain_pass": True, "judge_mean": None, "solved": True, "answered": None,
              "skipped": False},                                   # unjudged: excluded, not counted
             {"tier": 2, "chain_pass": None, "judge_mean": None, "solved": None, "answered": None,
              "skipped": True}]
    agg = aggregate(tasks)
    assert agg["per_tier"]["1"]["answered"] == 0.5     # 1 of the 2 tasks WITH a verdict
    assert agg["per_tier"]["2"]["answered"] is None
    assert agg["overall"]["answered"] == 0.5


def test_backfill_adds_answered_to_a_result_that_already_has_solved():
    """THE TRAP: every committed result already carries a non-None overall.solved, so a single
    `if solved is not None: return` guard would backfill answered for nothing and the new column
    would render — on every published row. Each metric must be guarded independently."""
    r = {"model": "m", "params": 27, "mode": "native", "scaffold": "off", "max_tokens": 8192,
         "battery_version": "cap-1", "k": 3, "date": "2026-01-01T00:00:00+00:00",
         "tasks": [
             {"id": "a", "tier": 1, "skipped": False, "chain_pass": False, "judge_mean": 3.0,
              "solved": False,                                     # right answer, no tool call
              "runs": [{"chain_correct": False, "judge_score": 3}] * 3},
             {"id": "b", "tier": 1, "skipped": False, "chain_pass": True, "judge_mean": 1.0,
              "solved": False,                                     # tool used, bad answer
              "runs": [{"chain_correct": True, "judge_score": 1}] * 3}],
         "per_tier": {"1": {"chain_pass": 0.5, "judge_mean": 2.0, "solved": 0.0, "abort_rate": None,
                            "n": 2, "skipped": 0}},
         "overall": {"chain_pass": 0.5, "judge_mean": 2.0, "solved": 0.0, "abort_rate": None,
                     "n": 2, "skipped": 0}}
    out = _backfill_solved(r)
    assert out["overall"]["answered"] == 0.5          # derived from the stored judge_scores
    assert out["per_tier"]["1"]["answered"] == 0.5
    # and the published numbers are untouched (answered != solved here, so it can't be a copy)
    assert out["overall"]["solved"] == 0.0 and out["per_tier"]["1"]["solved"] == 0.0
    assert out["overall"]["chain_pass"] == 0.5


def test_build_series_metric_answered():
    r = _backfill_solved(_fake_result("m", 3, {1: True}))
    s = build_series([r], "cap-1", metric="answered")
    assert s["1"][0][1] == 1.0


def test_report_renders_the_answered_column():
    base = {"mode": "native", "scaffold": "on", "disclosure": "off", "max_tokens": 2048,
            "battery_version": "cap-3zn-test", "date": "2026-01-01T00:00:00+00:00",
            "per_tier": {}, "tasks": []}
    with_answered = {**base, "model": "has-answered", "params": 1,
                     "overall": {"solved": 1.0, "answered": 0.5}}
    without = {**base, "model": "no-answered", "params": 2, "overall": {"solved": 1.0}}
    outs = [_write_result(with_answered), _write_result(without)]
    try:
        md, ok = render_report("cap-3zn-test")
        assert ok
        header = md.splitlines()[4].lower()
        assert "answered" in header
        # answered sits immediately after solved
        cols = [c.strip() for c in header.split("|")]
        assert cols[cols.index("solved") + 1] == "answered"
        rows = {l.split("|")[1].strip(): [c.strip() for c in l.split("|")]
                for l in md.splitlines()[6:] if l.startswith("|")}
        ai = _report_cols(md)["answered"]
        assert rows["has-answered"][ai] == "50%"
        assert rows["no-answered"][ai] == "—"   # absent -> em dash, never a fabricated number
        footnotes = [l for l in md.splitlines() if l.startswith("`answered`")]
        assert footnotes and "GAP" in footnotes[0]   # the footnote names the gap
    finally:
        import json as _json
        idx = outs[0].parent / "index.json"
        names = {o.name for o in outs}
        for o in outs:
            o.unlink(missing_ok=True)
        if idx.exists():
            keep = [e for e in _json.loads(idx.read_text()) if e.get("file") not in names]
            idx.write_text(_json.dumps(keep, indent=2))


def test_report_renders_the_disclosure_column():
    row = {"model": "m", "params": 3, "mode": "native", "scaffold": "on", "disclosure": "hybrid",
           "battery_version": "cap-2-test", "date": "2026-01-01T00:00:00+00:00",
           "per_tier": {}, "overall": {"solved": 1.0}, "tasks": []}
    out = _write_result(row)
    try:
        md, _ = render_report("cap-2-test")
        assert "disclosure" in md and "hybrid" in md
    finally:
        out.unlink(missing_ok=True)
        idx = out.parent / "index.json"
        if idx.exists():
            import json as _json
            keep = [e for e in _json.loads(idx.read_text()) if e.get("file") != out.name]
            idx.write_text(_json.dumps(keep, indent=2))


# --------------------------------------------------------------------------------------
# argus-as5: the judge axis must not double-count tool use.
#
# `chain_pass` already measures whether the declared tools were called. When a RUBRIC also
# demands the tool, the Opus judge scores tool use a second time, so `answered` (judge >= 2,
# chain ignored) stops being the clean "was the answer right, tools aside" axis it is
# documented to be — and the judge flips 3-vs-0 on the same string because "computes 51 via
# the calculator tool" can be read either way.
#
# So: cap-2 rubrics assert ANSWER QUALITY ONLY, and every tool requirement they used to carry
# now lives in `expect`, where the deterministic scorer owns it.
# --------------------------------------------------------------------------------------

# task id -> (tools that MUST be called, tools that must NOT be called)
# This is the provenance record for the rubric split: every tool named by a pre-split cap-2
# rubric appears here, and the test below proves `expect` still enforces it. Two restraint
# tasks are listed with nothing to move: their rubric said "(no such tool)" — an aside about
# the reply's content, naming no tool — so the edit only deleted the phrase.
#
# A required entry is a bare tool name (>= 1 call) or `(name, n)` for a requirement of n calls.
# The count matters: a `tools_in_order` clause listing a tool ONCE only proves one call, so it
# cannot discharge a two-call requirement. The three *_verify tasks' prompts demand a second,
# verifying query ("Verify the figure against the table before answering"), which is why they
# carry ("query_table", 2) — see `_expect_shortfalls`.
CAP2_MOVED_TOOL_REQUIREMENTS = {
    "t1_calc_area":           (["calculator"], []),
    "t1_calc_percent":        (["calculator"], []),
    "t1_calc_arith":          (["calculator"], []),
    "t1_calc_sqrt":           (["calculator"], []),
    "t1_pick_unit":           (["unit_convert"], ["calculator"]),
    "t1_pick_currency":       (["currency_convert"], ["calculator", "unit_convert"]),
    "t1_pick_crypto":         (["crypto_price"], ["calculator"]),
    "t1_define":              (["dictionary"], []),
    "t1_wiki_summary":        (["wikipedia"], []),
    "t1_fetch_example":       (["fetch_page"], []),
    "t1_text_count":          (["text_tools"], []),
    "t1_text_b64":            (["text_tools"], []),
    "t2_convert_currency":    (["currency_convert"], ["unit_convert", "calculator"]),
    "t2_define_not_wiki":     (["dictionary"], ["wikipedia"]),
    "t2_kb_add":              (["add_to_knowledge"], ["write_file"]),
    "t2_geocode_coords":      (["geocode"], ["weather", "wikipedia"]),
    "t2_fetch_single_page":   (["fetch_page"], ["crawl_site", "map_site"]),
    "t2_config_port":         (["read_file"], ["ask_data", "query_table"]),
    "t2_calc_discount":       (["calculator"], ["currency_convert"]),
    "t2_calc_scale_recipe":   (["calculator"], ["unit_convert"]),
    "t2_no_guess_email_count": ([], []),
    "t2_no_guess_traffic":    ([], []),
    "t2_no_guess_setting":    (["read_file"], []),
    "t3_budget_remaining":    (["calculator"], []),
    "t3_receipt_total":       (["calculator"], []),
    "t3_headcount_cost":      (["calculator"], []),
    "t3_specs_to_kb":         (["add_to_knowledge"], []),
    "t3_fetch_to_kb":         (["add_to_knowledge"], []),
    "t3_fetch_summary":       (["write_file"], []),
    "t4_amount_total_verify": ([("query_table", 2)], []),
    "t4_billable_verify":     ([("query_table", 2)], []),
    "t4_open_high_verify":    ([("query_table", 2)], []),
    "t4_no_oversearch_sleep": ([], ["web_search"]),
    "t4_no_oversearch_notes": ([], ["web_search"]),
    "t4_fetch_flag":          (["fetch_page", "write_file", "add_to_knowledge"], []),
}


def _known_tool_names():
    """Every registered tool name, read off engine/tools/*.py so this can't go stale as tools
    are added. Union'd with whatever cap-2's own `expect` blocks name."""
    import glob
    import json
    import re
    names = set()
    for path in glob.glob("engine/tools/*.py"):
        with open(path) as fh:
            names.update(re.findall(r'name\s*=\s*"([a-z_]+)"', fh.read()))
    b = json.loads(open("benchmark/cap-2/battery.json").read())
    for t in b["tasks"]:
        for key in ("tools_in_order", "min_counts", "max_counts"):
            v = (t.get("expect") or {}).get(key)
            if v:
                names.update(v if isinstance(v, list) else v.keys())
    return names


def test_cap2_rubrics_never_name_a_tool():
    """The judge axis is contaminated the moment a rubric criterion names a tool: `chain_pass`
    already scores tool use, so the judge scores it twice and `answered` stops meaning
    'the answer was right'. Scan, so it cannot regress."""
    import json
    import re
    names = _known_tool_names()
    battery = json.loads(open("benchmark/cap-2/battery.json").read())
    offenders = []
    for t in battery["tasks"]:
        for c in t.get("rubric") or []:
            low = c.lower()
            hits = sorted(n for n in names if re.search(r"\b" + re.escape(n) + r"\b", low))
            if hits or "tool" in low:
                # Hoisted out of the f-string: a backslash inside an f-string EXPRESSION is a
                # SyntaxError before 3.12 (PEP 701), and pyproject requires-python is >=3.11.
                why = hits or ['the word "tool"']
                offenders.append(f"{t['id']}: {c!r} -> {why}")
    assert not offenders, "cap-2 rubrics still name tools:\n" + "\n".join(offenders)


# Tasks whose ONLY honest rubric is "a concrete figure came back": the reply cannot show where
# the number came from, so any criterion about its provenance ("rather than a guessed figure",
# "a live price", "the real rate") is unanswerable from the final text — the judge's only
# evidence would be `_outcome`'s tool list, i.e. exactly the channel the rubric split exists to
# close (argus-z8e). Provenance is `expect`'s job: `tools_in_order` already requires the tool.
CAP2_FIGURE_TASKS = ("t1_pick_crypto", "t1_pick_currency", "t2_convert_currency")

# Wording that asserts something a reader of the final reply alone cannot check.
PROVENANCE_WORDS = ("guess", "fabricat", "invent", "made up", "actual", "real ", "genuine",
                    "live ", "current", "up-to-date", "up to date", "fetched", "looked up")


def test_cap2_figure_rubrics_are_checkable_from_the_reply():
    """These rubrics may assert only what a reader of the reply can verify: that a specific
    figure is reported rather than a refusal or a placeholder. They may NOT ask whether the
    figure is real, current or guessed — nothing in the text answers that, so the judge falls
    back on the tool list and the judge axis is contaminated again (t1_pick_crypto's mean moved
    2.96 -> 2.62 on exactly this wording, with all 11 changes downward)."""
    import json
    battery = {t["id"]: t for t in json.loads(open("benchmark/cap-2/battery.json").read())["tasks"]}
    offenders = []
    for tid in CAP2_FIGURE_TASKS:
        for c in battery[tid].get("rubric") or []:
            low = c.lower()
            hits = [w for w in PROVENANCE_WORDS if w in low]
            if hits:
                offenders.append(f"{tid}: {c!r} -> unanswerable from the reply: {hits}")
            assert "specific" in low, f"{tid}: {c!r} should still demand a specific figure"
    assert not offenders, "cap-2 rubrics ask the judge an unanswerable question:\n" + \
        "\n".join(offenders)


def _expect_shortfalls(tid, exp, required, forbidden):
    """Ways `exp` fails to enforce the (required, forbidden) provenance entry, as strings.

    COUNTS ARE CHECKED. `tools_in_order` is a subsequence match, so a tool listed once in it
    guarantees exactly one call — it cannot discharge a requirement of two. Only repeats in
    `tools_in_order` or a `min_counts` entry can. (This is the masking that let the *_verify
    tasks' second, verifying query be dropped from the rubrics without any test noticing.)
    """
    order = list(exp.get("tools_in_order") or [])
    mins = exp.get("min_counts") or {}
    maxes = exp.get("max_counts") or {}
    out = []
    for item in required:
        tool, n = item if isinstance(item, tuple) else (item, 1)
        guaranteed = max(order.count(tool), int(mins.get(tool) or 0))
        if guaranteed < n:
            out.append(f"{tid}: requires {n}x {tool} but expect guarantees only "
                       f"{guaranteed} ({exp})")
    for tool in forbidden:
        if maxes.get(tool) != 0:
            out.append(f"{tid}: forbids {tool} but expect.max_counts does not ({exp})")
    return out


def test_expect_shortfalls_counts_calls_not_just_names():
    """A one-call `tools_in_order` clause must NOT be accepted as satisfying a two-call
    requirement — the masking that hid the dropped verification step on the *_verify tasks."""
    one_call = {"tools_in_order": ["read_file", "query_table"]}
    assert _expect_shortfalls("t", one_call, ["query_table"], []) == []          # 1 of 1: ok
    assert _expect_shortfalls("t", one_call, [("query_table", 2)], [])           # 1 of 2: not ok
    # only min_counts (or a repeat in tools_in_order) can discharge a 2-call requirement
    with_min = {"tools_in_order": ["query_table"], "min_counts": {"query_table": 2}}
    assert _expect_shortfalls("t", with_min, [("query_table", 2)], []) == []
    repeated = {"tools_in_order": ["query_table", "query_table"]}
    assert _expect_shortfalls("t", repeated, [("query_table", 2)], []) == []
    # a min_counts BELOW the requirement is still a shortfall
    assert _expect_shortfalls("t", {"min_counts": {"query_table": 1}}, [("query_table", 2)], [])
    # forbidden tools still need an explicit max_counts 0
    assert _expect_shortfalls("t", one_call, [], ["web_search"])
    assert _expect_shortfalls("t", {"max_counts": {"web_search": 0}}, [], ["web_search"]) == []


def test_cap2_moved_tool_requirements_still_enforced_by_expect():
    """Nothing was silently dropped: every tool a pre-split rubric named is still required (or
    still forbidden) by that task's `expect`, at the call count the prompt demands."""
    import json
    battery = {t["id"]: t for t in json.loads(open("benchmark/cap-2/battery.json").read())["tasks"]}
    missing = []
    for tid, (required, forbidden) in CAP2_MOVED_TOOL_REQUIREMENTS.items():
        assert tid in battery, f"{tid} is not a cap-2 task"
        missing += _expect_shortfalls(tid, battery[tid].get("expect") or {}, required, forbidden)
    assert not missing, "tool requirements lost in the rubric split:\n" + "\n".join(missing)


def test_cap2_verify_tasks_require_a_second_query():
    """The three *_verify tasks are named for a verification step their prompts demand in plain
    English ("Verify the figure against the table before answering" / "Confirm the count with a
    query before you answer"). The rubric split moved that requirement off the judge axis, so
    `expect` is the only place left that can hold it: one query computes the figure, a second
    verifies it. Pinned here as well as in the provenance table so neither can be quietly
    weakened. See the README — this makes those rows non-comparable to published chain_pass."""
    import json
    import re
    battery = {t["id"]: t for t in json.loads(open("benchmark/cap-2/battery.json").read())["tasks"]}
    for tid in ("t4_amount_total_verify", "t4_billable_verify", "t4_open_high_verify"):
        task = battery[tid]
        assert re.search(r"verify|confirm|double-check", task["prompt"], re.I), \
            f"{tid}'s prompt no longer asks for verification; revisit this requirement"
        mins = (task.get("expect") or {}).get("min_counts") or {}
        assert mins.get("query_table", 0) >= 2, \
            f"{tid} must require >= 2 query_table calls (compute + verify), got {mins}"


# ------------------------------- rejudge -------------------------------

def _mk_result(runs, rubric=("answers correctly",), k=3):
    return {"model": "m", "params": 3, "mode": "native", "battery_version": "cap-x", "k": k,
            "date": "2026-01-01T00:00:00+00:00",
            "per_tier": {"1": {"solved": 0.0, "answered": 0.0, "judge_mean": 0.0}},
            "overall": {"solved": 0.0, "answered": 0.0, "judge_mean": 0.0, "n": 1, "skipped": 0},
            "tasks": [{"id": "t1", "tier": 1, "skipped": False, "chain_pass": True,
                       "judge_mean": 0.0, "solved": False, "runs": list(runs)}]}, \
           {"battery_version": "cap-x",
            "tasks": [{"id": "t1", "tier": 1, "prompt": "p", "rubric": list(rubric)}]}


def _stub_judge(score_by_final):
    calls = []

    async def fn(messages):
        user = "\n".join(m["content"] for m in messages if m["role"] == "user")
        calls.append(user)
        for needle, score in score_by_final.items():
            if needle in user:
                return '{"score": %d, "why": "stub"}' % score
        return '{"score": 0, "why": "stub-default"}'
    fn.calls = calls
    return fn


def test_rejudge_is_identity_when_the_judge_agrees_with_the_stored_score():
    """A re-judge that reproduces the same scores must leave every published number exactly
    where it was — so any movement in the real pass is attributable to the rubric change, not
    to the re-judge machinery."""
    import asyncio
    from engine.eval.benchmark import rejudge_result
    runs = [{"tools": ["calculator"], "final": "AAA", "chain_correct": True, "judge_score": 3},
            {"tools": ["calculator"], "final": "BBB", "chain_correct": True, "judge_score": 2},
            {"tools": [], "final": "CCC", "chain_correct": False, "judge_score": 1}]
    result, battery = _mk_result(runs)
    rep = asyncio.run(rejudge_result(result, battery,
                                     _stub_judge({"AAA": 3, "BBB": 2, "CCC": 1})))
    assert rep["stats"]["judged"] == 3 and rep["stats"]["changed"] == 0
    assert [r["judge_score"] for r in result["tasks"][0]["runs"]] == [3, 2, 1]
    assert result["overall"]["judge_mean"] == 2.0
    assert result["overall"]["solved"] == 1.0 and result["overall"]["answered"] == 1.0
    # ...and a second identical pass is a no-op, byte for byte
    import copy
    snapshot = copy.deepcopy(result)
    asyncio.run(rejudge_result(result, battery, _stub_judge({"AAA": 3, "BBB": 2, "CCC": 1})))
    assert result == snapshot


def test_rejudge_skips_a_run_with_no_final_instead_of_scoring_it_zero():
    """A run whose output was never recorded is a HARNESS gap, not a model failure. Scoring it 0
    would manufacture evidence."""
    import asyncio
    from engine.eval.benchmark import rejudge_result
    runs = [{"tools": [], "final": "", "chain_correct": True, "judge_score": 3},
            {"tools": [], "final": None, "chain_correct": True},
            {"tools": [], "final": "   ", "chain_correct": True},
            {"tools": [], "final": "X", "error": "Timeout", "chain_correct": False, "judge_score": 2},
            {"tools": [], "final": "OK", "chain_correct": True, "judge_score": 1}]
    result, battery = _mk_result(runs, k=5)
    judge = _stub_judge({"OK": 3})
    rep = asyncio.run(rejudge_result(result, battery, judge))
    assert rep["stats"] == {**rep["stats"], "judged": 1, "no_final": 3, "errored": 1}
    assert len(judge.calls) == 1                       # the skipped runs were never sent
    got = [r.get("judge_score") for r in result["tasks"][0]["runs"]]
    assert got == [3, None, None, 2, 3]                # untouched, not zeroed
    assert 0 not in [g for g in got if g is not None]


def test_rejudge_recomputes_solved_and_answered_from_the_new_scores():
    """`solved`/`answered` are derived, so they must follow the new scores rather than keeping
    the stale collapse stored on disk."""
    import asyncio
    from engine.eval.benchmark import rejudge_result
    runs = [{"tools": [], "final": "AAA", "chain_correct": False, "judge_score": 0},
            {"tools": [], "final": "AAA", "chain_correct": False, "judge_score": 0},
            {"tools": [], "final": "AAA", "chain_correct": False, "judge_score": 0}]
    result, battery = _mk_result(runs)
    assert result["overall"]["answered"] == 0.0        # stale, stored
    rep = asyncio.run(rejudge_result(result, battery, _stub_judge({"AAA": 3})))
    assert rep["stats"]["changed"] == 3
    # judge now says 3 on every run: `answered` (judge alone) flips True...
    assert result["tasks"][0]["answered"] is True
    assert result["overall"]["answered"] == 1.0
    assert result["per_tier"]["1"]["answered"] == 1.0
    # ...but `solved` still requires the chain, which still fails. The axes stay independent.
    assert result["tasks"][0]["solved"] is False
    assert result["overall"]["solved"] == 0.0
    assert result["overall"]["judge_mean"] == 3.0


def test_rejudge_refuses_a_frozen_battery():
    """cap-1 is published and closed; re-judging it would rewrite settled numbers."""
    import json
    import tempfile
    from pathlib import Path
    from engine.eval.benchmark import main
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "battery.json"
        p.write_text(json.dumps({"battery_version": "cap-1", "tasks": []}))
        assert main(["rejudge", "--battery", str(p)]) == 2


def test_captured_from_run_carries_the_schemas_the_judge_prompt_renders():
    from engine.eval.benchmark import captured_from_run
    from engine.eval.judge import build_judge_prompt
    cap = captured_from_run({"tools": ["create_table", "insert_row"], "final": "done",
                             "create_table_args": [{"name": "t", "columns": ["a", "b"]}]})
    user = build_judge_prompt({"prompt": "p", "rubric": ["r"]}, cap)[1]["content"]
    assert "t(a, b)" in user and "rows inserted: 1" in user


# ------------------------------- argus-2oj: validity, shape, compliance -------------------------------

def _mkresult(no_tool_flags, *, judges=None, chains=None, tier=1, ids=None, canaries=None):
    """A result dict with one run per flag, in order. no_tool_flags[i]=1 -> that run called no tool."""
    n = len(no_tool_flags)
    judges = judges if judges is not None else [3] * n
    chains = chains if chains is not None else [True] * n
    ids = ids or [f"t{i}" for i in range(n)]
    tasks = [{"id": ids[i], "tier": tier, "skipped": False,
              "runs": [{"tools": ([] if no_tool_flags[i] else ["calculator"]),
                        "judge_score": judges[i], "chain_correct": chains[i]}]}
             for i in range(n)]
    r = {"battery_version": "cap-2", "k": 1, "tasks": tasks,
         "overall": {"solved": 0.5, "answered": 0.5}, "per_tier": {}}
    if canaries is not None:
        r["canaries"] = canaries
    return r


def _battery(ids):
    return {"tasks": [{"id": i, "expect": {"tools_in_order": ["calculator"]}} for i in ids]}


def test_validity_step_change_is_degraded():
    """Test 1: the canonical instrument failure — clean, then 100% no-tool, and never recovers."""
    from engine.eval import validity as V
    flags = [0] * 30 + [1] * 30
    r = _mkresult(flags)
    a = V.assess(r, _battery([t["id"] for t in r["tasks"]]))
    assert a["validity"] == "degraded", a["evidence"]
    assert a["evidence"]["no_tool_last_third"] == 1.0
    assert a["no_tool_shape"][0] == 0.0 and a["no_tool_shape"][-1] == 1.0


def test_validity_flat_weak_model_is_ok():
    """Test 2: THE test that keeps the rule honest. A uniformly bad model is a MEASUREMENT, not a
    broken instrument. If this ever fails, the rule has started punishing weak models."""
    from engine.eval import validity as V
    flags = [1, 1, 0] * 20               # a flat 67% no-tool rate — high, but not a step change
    r = _mkresult(flags)
    a = V.assess(r, _battery([t["id"] for t in r["tasks"]]))
    assert a["validity"] == "ok", a["evidence"]


def test_validity_every_committed_result_is_ok():
    """The no-false-positive property, locked against the real corpus. The battery is ordered
    T1..T4, so 'first third vs last third' is also 'easy vs hard' — this proves the rule does not
    condemn a model for merely having a difficulty gradient."""
    import glob, json as _json
    from engine.eval.benchmark import _backfill_validity
    files = [f for f in glob.glob("benchmark/results/*cap-2*.json")]
    if not files:
        import pytest; pytest.skip("no committed cap-2 results in this checkout")
    bad = []
    for f in files:
        d = _backfill_validity(_json.loads(open(f).read()))
        if (d.get("overall") or {}).get("solved") is None:
            continue
        if d.get("validity") != "ok":
            bad.append((f.split("/")[-1], d.get("validity"), d.get("evidence")))
    assert not bad, "the rule condemned a known-good run:\n" + "\n".join(map(str, bad))


def test_canary_two_consecutive_failures_aborts():
    """Test 3/4: two in a row aborts; a single fumble does not."""
    from engine.eval import validity as V
    assert V.canary_verdict([{"passed": True}, {"passed": False}, {"passed": False}])[0] is True
    assert V.canary_verdict([{"passed": False}, {"passed": True}, {"passed": False}])[0] is False
    assert V.canary_verdict([])[0] is False


def test_canary_pass_requires_tool_and_exact_answer():
    from engine.eval import validity as V
    assert V.canary_passed({"tools": ["calculator"], "final": "It is 220401."})
    assert V.canary_passed({"tools": ["calculator"], "final": "220,401"})     # separators stripped
    assert not V.canary_passed({"tools": [], "final": "220401"})              # no tool used
    assert not V.canary_passed({"tools": ["calculator"], "final": "220400"})  # wrong product


def test_aborted_beats_degraded():
    """An aborted run is incomplete, so its shape is not a measurement of anything."""
    from engine.eval import validity as V
    r = _mkresult([0] * 30 + [1] * 30, canaries=[{"passed": False}, {"passed": False}])
    a = V.assess(r, _battery([t["id"] for t in r["tasks"]]))
    assert a["validity"] == "aborted"


def test_restraint_tasks_excluded_from_shape():
    """A task whose `expect` requires no tool must not count as a no-tool run: for it, calling
    nothing is CORRECT. Those tasks cluster in T1/T2, so including them would bias the thirds."""
    from engine.eval import validity as V
    r = _mkresult([1] * 12)                       # every run called no tool...
    b = {"tasks": [{"id": t["id"], "expect": {}} for t in r["tasks"]]}   # ...but none required one
    series, basis = V.no_tool_series(r, b)
    assert series == [] and basis == "all_runs"    # nothing tool-required -> falls back, records it


def test_compliance_gap_flags_shortcutting_model_but_stays_valid():
    """Test 7: a strong model that reaches the right answer its own way is flagged, NOT condemned.
    validity and compliance must not collapse into each other."""
    from engine.eval import validity as V
    n = 30
    r = _mkresult([0] * n, chains=[False] * n, judges=[3] * n)   # chain fails, judge accepts
    r["overall"] = {"solved": 0.70, "answered": 0.95}
    a = V.assess(r, _battery([t["id"] for t in r["tasks"]]))
    assert a["validity"] == "ok", a["evidence"]
    c = a["compliance"]
    assert c["compliance_gap"] == 0.25 and c["high_compliance_gap"] is True
    assert c["runs"]["chain_fail_answer_ok"] == n and c["runs"]["answer_wrong"] == 0


def test_compliance_gap_zero_when_no_chain_failures():
    """Test 8."""
    from engine.eval import validity as V
    r = _mkresult([0] * 12)
    r["overall"] = {"solved": 0.8, "answered": 0.8}
    c = V.assess(r, _battery([t["id"] for t in r["tasks"]]))["compliance"]
    assert c["compliance_gap"] == 0.0 and c["high_compliance_gap"] is False
    assert c["runs"]["chain_fail_answer_ok"] == 0


def test_compliance_counts_no_tool_answer_ok_separately():
    """The T1/T2 signature: right answer, no tool at all (did the arithmetic in its head)."""
    from engine.eval import validity as V
    r = _mkresult([1, 1, 0], chains=[False, False, False], judges=[3, 3, 3])
    c = V.compliance_stats(r)
    assert c["runs"]["chain_fail_answer_ok"] == 3
    assert c["runs"]["no_tool_answer_ok"] == 2


def test_shape_ratio_is_json_serializable():
    """0% -> nonzero is the canonical case; an inf ratio must not break serialization."""
    import json as _json
    from engine.eval import validity as V
    _, ev = V.shape_verdict([0] * 30 + [1] * 30)
    assert ev["no_tool_ratio"] is None
    _json.dumps(ev)


def test_validity_short_run_not_judged():
    from engine.eval import validity as V
    ok, ev = V.shape_verdict([1, 1, 1])
    assert ok is False and "too few runs" in ev["shape_note"]


def test_sparkline_shows_the_step():
    from engine.eval import validity as V
    s = V.sparkline(V.decile_shape([0] * 30 + [1] * 30))
    assert s[0] == "▁" and s[-1] == "█" and len(s) == 10
    assert V.sparkline([]) == ""


def test_report_marks_a_degraded_row(tmp_path, monkeypatch):
    """Test 6: a non-ok row is visibly marked, and the gap column renders."""
    import engine.eval.benchmark as bm
    monkeypatch.setattr(bm, "RESULTS", tmp_path)
    r = _mkresult([0] * 30 + [1] * 30)
    # the synthetic task ids are not in the real cap-2 battery, so the tool-required set would
    # exclude every run and the shape would be empty -- feed the matching battery instead
    monkeypatch.setattr(bm, "_battery_for", lambda bv: _battery([t["id"] for t in r["tasks"]]))
    r.update({"model": "brokenmodel", "params": 7, "mode": "native", "scaffold": "on",
              "date": "2026-07-30T00:00:00+00:00",
              "per_tier": {"1": {"chain_pass": 0.5, "judge_mean": 2.0, "solved": 0.5, "answered": 0.7}},
              "overall": {"chain_pass": 0.5, "judge_mean": 2.0, "solved": 0.5, "answered": 0.7}})
    (tmp_path / "brokenmodel-cap-2-20260730.json").write_text(_json_dumps(r))
    md, ok = bm.render_report("cap-2")
    assert ok
    assert "**DEGRADED**" in md and "brokenmodel ⚠" in md
    assert "no-tool shape" in md and "`valid`" in md


def _json_dumps(o):
    import json as _j
    return _j.dumps(o)
