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


def _all_column_specs(create_table_args: list) -> list[str]:
    """Flatten every column spec string across all create_table calls in a run."""
    out: list[str] = []
    for a in create_table_args or []:
        for col in (a or {}).get("columns") or []:
            out.append(str(col))
    return out


def score_case(expect: dict, captured: dict) -> dict:
    """expect: any of tools_in_order / min_counts / activates / skill_not / schema_has.
    captured: {"tools": [ordered tool names], "activated_skill": name|None,
               "create_table_args": [{"name":..., "columns":[...]}]}.
    Returns {"chain_correct": bool, "checks": {name: bool}, "reasons": [str]}.

    `schema_has` inspects the create_table ARGUMENTS (not the tool sequence): a list of substrings
    each of which must appear in at least one created column spec (e.g. ["json"] verifies a json/list
    column was actually declared — the design_table instinct a bare "create_table fired" can't detect)."""
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

    if "schema_has" in expect:
        specs = _all_column_specs(captured.get("create_table_args"))
        joined = " ".join(specs).lower()
        ok = all(sub.lower() in joined for sub in expect["schema_has"])
        checks["schema_has"] = ok
        if not ok:
            reasons.append(f"schema {specs} missing {expect['schema_has']}")

    return {"chain_correct": all(checks.values()) if checks else False,
            "checks": checks, "reasons": reasons}
