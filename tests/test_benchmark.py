"""Benchmark pure helpers: task_verdict, aggregate, build_series, resolve_config."""
from engine.eval.benchmark import (_backfill_solved, aggregate, build_series, render_report,
                                    resolve_config, task_verdict, _write_result)


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


def test_render_report_has_solved_column():
    # render on a battery version present via _load_results is integration-heavy; test the string builder
    # by monkeypatching _load_results through a written file is overkill — assert the header names solved.
    from engine.eval import benchmark as B
    md, ok = B.render_report("cap-1")  # cap-1 results exist in the repo
    assert ok and "solved" in md.splitlines()[4].lower()  # header row includes the column


def test_build_series_metric_solved():
    r = _backfill_solved(_fake_result("m", 3, {1: True}))
    s = build_series([r], "cap-1", metric="solved")
    assert s["1"][0][1] == 1.0   # (params, value, judge) — value is the solved rate for metric=solved
