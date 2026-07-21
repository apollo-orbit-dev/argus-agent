"""Benchmark pure helpers: task_verdict, aggregate, build_series, resolve_config."""
from engine.eval.benchmark import aggregate, build_series, resolve_config, task_verdict


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


def test_resolve_config_overrides():
    c = resolve_config("fast=http://host:8001/v1|qwen", "manual")
    assert c.model_base_url == "http://host:8001/v1" and c.model_name == "qwen"
    assert c.tool_calling_mode == "manual"
    d = resolve_config("main", None)                        # no override → configured defaults
    assert d.tool_calling_mode in ("native", "manual", "native_finish")
