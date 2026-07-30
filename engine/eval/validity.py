"""Does a benchmark run measure the MODEL, or the INSTRUMENT?

A cap-2 arm on Qwen3.6-35B once ran clean for 42 runs, then produced no valid tool call for
the remaining 125, and recorded all of it as model capability (`solved=0.250`, published).
The cause was a vLLM serving flag; it was found by hand-auditing a transcript, not by the
harness. This module makes the harness refuse to vouch for a run like that.

Three independent signals, deliberately kept apart because they fail differently:

  validity        — is the instrument working? (canary + run shape)
  compliance gap  — is `solved` measuring capability, or path-compliance?
  the shape       — the per-decile no-tool rate, so a step change is visible at a glance

THE TWO CONCEPTS MUST NOT COLLAPSE INTO EACH OTHER. A genuinely weak model and a
shortcut-prone strong model are both VALID runs; only a broken instrument is not. Every
threshold here is tuned so that a weak model stays `ok` and a strong model that skips the
declared tools stays `ok` — see tests 2 and 7 in the spec (argus-2oj).
"""
from __future__ import annotations

import re

# Canary cadence: before the first task, then every N tasks, then after the last.
CANARY_EVERY = 10
# Two CONSECUTIVE canary failures = compromised. One is a warning: models legitimately fumble.
CANARY_ABORT_STREAK = 2

# The degraded signature: a step change that never recovers, not a scatter. Both conditions
# must hold. The floor matters as much as the ratio — the tightest real no-false-positive
# margin in the committed corpus is gemma-4-E2B manual/on at 33% with a 5.7x ratio, which
# passes ONLY because of the floor. Lower the floor and that valid run gets condemned.
DEGRADED_RATIO = 3.0
DEGRADED_FLOOR = 0.50

# `answered` - `solved` at or above this is flagged. NOT a validity failure — the run is real.
COMPLIANCE_GAP_FLAG = 0.15

SHAPE_BUCKETS = 10

# A trivial, dependency-free probe any working model+harness passes: one obvious tool, one
# exact answer, no judge (a canary must not depend on the judge being reachable). It is NOT a
# battery task and never enters any score.
CANARY_PROMPT = "What is 8163 multiplied by 27? Use the calculator tool."
CANARY_TOOL = "calculator"
CANARY_ANSWER = "220401"


def canary_passed(captured: dict) -> bool:
    """True iff the probe called the calculator AND the exact product appears in the answer.
    Deterministic on purpose: no judge, no rubric, no partial credit. Digit separators are
    stripped before matching so "220,401" counts."""
    if CANARY_TOOL not in (captured.get("tools") or []):
        return False
    flat = re.sub(r"[,\s_]", "", captured.get("final") or "")
    return CANARY_ANSWER in flat


def canary_verdict(canaries: list) -> tuple[bool, dict]:
    """(aborted, evidence) from the canary history [{'before_task_index': int, 'passed': bool}].
    Aborted iff CANARY_ABORT_STREAK consecutive failures occurred."""
    streak = worst = 0
    for c in canaries:
        streak = 0 if c.get("passed") else streak + 1
        worst = max(worst, streak)
    ever = any(c.get("passed") for c in canaries)
    return worst >= CANARY_ABORT_STREAK, {
        "canaries_run": len(canaries),
        "canary_failures": sum(1 for c in canaries if not c.get("passed")),
        "worst_canary_streak": worst,
        # Distinguishes an instrument that REGRESSED mid-run (passed, then stopped) from one that
        # never worked at all — or from a model too weak to call one obvious tool. Same abort
        # either way (there is nothing worth harvesting from either), but the diagnosis differs,
        # and "canary_ever_passed: false" on a tiny model is a prompt/model question, not a
        # serving question. Recorded, deliberately, rather than inferred later from the streak.
        "canary_ever_passed": ever,
    }


def tool_required_ids(battery: dict) -> set:
    """Task ids whose `expect` demands at least one tool call. Restraint tasks ("don't invent a
    price") legitimately expect NO tool, so counting them as no-tool runs would read correct
    behaviour as instrument failure — and because the battery is ordered T1..T4, those tasks
    cluster by position and would bias the very thirds comparison the shape rule uses."""
    out = set()
    for t in battery.get("tasks") or []:
        e = t.get("expect") or {}
        if e.get("tools_in_order") or e.get("min_counts"):
            out.add(t["id"])
    return out


def no_tool_series(result: dict, battery: dict | None = None) -> tuple[list, str]:
    """(series, basis) where series is 1-per-run-with-no-tool in RUN ORDER, over tool-required
    tasks only. basis is "tool_required" normally, or "all_runs" when no battery was supplied
    (an older result whose battery file is unavailable) — recorded so a shape computed on the
    looser basis is never mistaken for the strict one."""
    required = tool_required_ids(battery) if battery else None
    basis = "tool_required" if required else "all_runs"
    series = []
    for t in result.get("tasks") or []:
        if t.get("skipped"):
            continue
        if required is not None and t.get("id") not in required:
            continue
        for r in t.get("runs") or []:
            series.append(0 if (r.get("tools") or []) else 1)
    return series, basis


def decile_shape(series: list, buckets: int = SHAPE_BUCKETS) -> list:
    """No-tool rate per equal bucket, in run order. The aggregate concealed a hard step change;
    this shows it. Buckets split by index so every run lands in exactly one."""
    n = len(series)
    if n == 0:
        return []
    b = min(buckets, n)
    out = []
    for i in range(b):
        lo, hi = (i * n) // b, ((i + 1) * n) // b
        chunk = series[lo:hi] or series[lo:lo + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def shape_verdict(series: list) -> tuple[bool, dict]:
    """(degraded, evidence). Degraded iff the last third's no-tool rate is >= DEGRADED_RATIO x
    the first third's AND exceeds DEGRADED_FLOOR. A uniformly high but FLAT rate is a weak
    model, not a broken instrument, and stays ok — that is the whole point of the ratio test."""
    n = len(series)
    if n < 9:                       # too short for thirds to mean anything
        return False, {"shape_n": n, "shape_note": "too few runs to judge shape"}
    third = n // 3
    first, last = series[:third], series[-third:]
    fr, lr = sum(first) / len(first), sum(last) / len(last)
    if fr > 0:
        ratio = lr / fr
    else:
        ratio = float("inf") if lr > 0 else 1.0
    degraded = lr >= DEGRADED_FLOOR and ratio >= DEGRADED_RATIO
    return degraded, {
        "shape_n": n,
        "no_tool_first_third": round(fr, 4),
        "no_tool_last_third": round(lr, 4),
        # inf is not JSON — a run that starts perfect and ends broken is the canonical case,
        # so it must serialize. None reads as "unbounded" and never as a real ratio.
        "no_tool_ratio": None if ratio == float("inf") else round(ratio, 2),
    }


def compliance_stats(result: dict) -> dict:
    """Per-run split of WHY a run didn't count as solved, plus `compliance_gap`.

    `solved` requires the prescribed tool chain; `answered` only requires the judge. A strong
    model routinely reaches the right result by its own path, so `solved` understates it — and
    the bias grows with capability, which makes `solved` unsafe for cross-size comparison.
    Measured on deepseek-v4-flash (284B): answered 0.964, solved 0.763, with only 6 of 156 runs
    actually WRONG. This records that instead of hiding it."""
    from engine.eval.benchmark import JUDGE_SOLVED_MIN

    counts = {"solved": 0, "chain_fail_answer_ok": 0, "answer_wrong": 0, "no_tool_answer_ok": 0}
    per_tier = {}
    for t in result.get("tasks") or []:
        if t.get("skipped"):
            continue
        tier = str(t.get("tier"))
        pt = per_tier.setdefault(tier, dict.fromkeys(counts, 0))
        for r in t.get("runs") or []:
            c, j = r.get("chain_correct"), r.get("judge_score")
            chain_ok = True if c is None else bool(c)
            ans_ok = None if j is None else (j >= JUDGE_SOLVED_MIN)
            if ans_ok is False:
                key = "answer_wrong"
            elif chain_ok:
                key = "solved"
            else:
                key = "chain_fail_answer_ok"
            counts[key] += 1
            pt[key] += 1
            if key == "chain_fail_answer_ok" and not (r.get("tools") or []):
                counts["no_tool_answer_ok"] += 1
                pt["no_tool_answer_ok"] += 1

    ov = result.get("overall") or {}
    solved, answered = ov.get("solved"), ov.get("answered")
    gap = None if (solved is None or answered is None) else round(answered - solved, 4)
    out = {"compliance_gap": gap, "runs": counts, "per_tier": per_tier,
           "high_compliance_gap": bool(gap is not None and gap >= COMPLIANCE_GAP_FLAG)}
    tiers = {}
    for tier, pt in (result.get("per_tier") or {}).items():
        s, a = pt.get("solved"), pt.get("answered")
        tiers[tier] = None if (s is None or a is None) else round(a - s, 4)
    out["gap_per_tier"] = tiers
    return out


def assess(result: dict, battery: dict | None = None) -> dict:
    """The full verdict block for one result: validity + evidence + shape + compliance.

    validity is "aborted" (canary), "degraded" (shape), else "ok". Canary wins: an aborted run
    is incomplete, so its shape is not a measurement of anything."""
    canaries = result.get("canaries") or []
    aborted, cev = canary_verdict(canaries)
    series, basis = no_tool_series(result, battery)
    degraded, sev = shape_verdict(series)
    validity = "aborted" if aborted else ("degraded" if degraded else "ok")
    return {
        "validity": validity,
        "evidence": {**cev, **sev, "shape_basis": basis},
        "no_tool_shape": [round(x, 3) for x in decile_shape(series)],
        "compliance": compliance_stats(result),
    }


_SPARK = "▁▂▃▄▅▆▇█"


def sparkline(shape: list) -> str:
    """Compact per-decile no-tool rate. Empty string for no data — never a misleading flat bar."""
    if not shape:
        return ""
    return "".join(_SPARK[min(len(_SPARK) - 1, int(round(x * (len(_SPARK) - 1))))] for x in shape)
