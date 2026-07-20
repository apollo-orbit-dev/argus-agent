"""Pure, deterministic scoring for the skill-eval harness. No I/O, no model, no engine import.

A case's `expect` predicate is checked against what actually happened (`captured`). Every predicate key
is optional; `chain_correct` is the AND of the checks that are present (a predicate with no recognized
keys is `False` — an empty expectation proves nothing).
"""
from __future__ import annotations


def _is_subsequence(needles: list, hay: list) -> bool:
    """True if `needles` appear in `hay` in order (other items may interleave)."""
    it = iter(hay)
    return all(n in it for n in needles)


def score_case(expect: dict, captured: dict) -> dict:
    """expect: any of tools_in_order / min_counts / activates / skill_not.
    captured: {"tools": [ordered tool names], "activated_skill": name|None}.
    Returns {"chain_correct": bool, "checks": {name: bool}, "reasons": [str]}."""
    tools = list(captured.get("tools") or [])
    skill = captured.get("activated_skill")
    checks: dict[str, bool] = {}
    reasons: list[str] = []

    if "tools_in_order" in expect:
        ok = _is_subsequence(list(expect["tools_in_order"]), tools)
        checks["tools_in_order"] = ok
        if not ok:
            reasons.append(f"tools {tools} lack ordered {expect['tools_in_order']}")

    if "min_counts" in expect:
        ok = all(tools.count(t) >= n for t, n in expect["min_counts"].items())
        checks["min_counts"] = ok
        if not ok:
            reasons.append(f"min_counts {expect['min_counts']} not met by {tools}")

    if "activates" in expect:
        ok = skill == expect["activates"]
        checks["activates"] = ok
        if not ok:
            reasons.append(f"activated {skill!r}, expected {expect['activates']!r}")

    if "skill_not" in expect:
        ok = skill not in set(expect["skill_not"])
        checks["skill_not"] = ok
        if not ok:
            reasons.append(f"over-fired: {skill!r} in {expect['skill_not']}")

    return {"chain_correct": all(checks.values()) if checks else False,
            "checks": checks, "reasons": reasons}
