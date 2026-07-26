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
        # abort is the 9th cell: | model | params | mode | scaffold | disclosure | max_tok |
        # solved | answered | abort | ...   (`disclosure` was added when progressive tool disclosure
        # landed; `answered` when the second headline metric landed, argus-3zn)
        abort_cell = cells[9]
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
        assert rows["has-answered"][8] == "50%"
        assert rows["no-answered"][8] == "—"    # absent -> em dash, never a fabricated number
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
