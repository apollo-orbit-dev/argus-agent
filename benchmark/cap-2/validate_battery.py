"""Structural validator for the cap-2 battery. Empty return = valid. Encodes the plan's Global
Constraints so authoring is gated by `pytest -k cap2_battery` rather than eyeballing."""
import json, os

FAMILIES = {"compute", "tool-selection", "retrieve", "data-transform", "synthesis", "restraint"}
PER_TIER = 14
TIERS = [1, 2, 3, 4]

def validate(path: str) -> list[str]:
    p = []
    b = json.loads(open(path).read())
    if b.get("battery_version") != "cap-2":
        p.append(f"battery_version must be 'cap-2', got {b.get('battery_version')!r}")
    tasks = b.get("tasks", [])
    if len(tasks) != PER_TIER * len(TIERS):
        p.append(f"expected {PER_TIER*len(TIERS)} tasks, got {len(tasks)}")
    ids = [t.get("id") for t in tasks]
    if len(set(ids)) != len(ids):
        p.append("duplicate task ids")
    fixdir = os.path.join(os.path.dirname(path), "fixtures")
    requires_counts = {"": 0, "searxng": 0}
    for t in tasks:
        tid = t.get("id", "?")
        if t.get("tier") not in TIERS:
            p.append(f"{tid}: bad tier {t.get('tier')}")
        if t.get("category") not in FAMILIES:
            p.append(f"{tid}: category {t.get('category')!r} not in {sorted(FAMILIES)}")
        if not t.get("prompt"):
            p.append(f"{tid}: empty prompt")
        # T3/T4 MUST have a real chain AND a rubric
        if t.get("tier") in (3, 4):
            exp = (t.get("expect") or {}).get("tools_in_order")
            if not exp:
                p.append(f"{tid}: T{t.get('tier')} needs expect.tools_in_order")
            if not t.get("rubric"):
                p.append(f"{tid}: T{t.get('tier')} needs a rubric")
        # fixtures referenced by `source` must exist
        src = t.get("source")
        if src and not os.path.exists(os.path.join(fixdir, src)):
            p.append(f"{tid}: fixture {src!r} missing from fixtures/")
        req = t.get("requires", "")
        requires_counts[req] = requires_counts.get(req, 0) + 1
    for tier in TIERS:
        n = sum(1 for t in tasks if t.get("tier") == tier)
        if n != PER_TIER:
            p.append(f"tier {tier}: expected {PER_TIER} tasks, got {n}")
    # dependency budget
    no_dep = requires_counts.get("", 0)
    if no_dep < 45:
        p.append(f"need >=45 dependency-free tasks (>=80%), got {no_dep}")
    if requires_counts.get("searxng", 0) > 2:
        p.append(f"web_search/searxng capped at 2 tasks, got {requires_counts['searxng']}")
    if any(k not in ("", "searxng", "firecrawl", "pdf") for k in requires_counts):
        p.append(f"unexpected requires values: {sorted(requires_counts)}")
    return p

if __name__ == "__main__":
    import sys
    probs = validate(sys.argv[1] if len(sys.argv) > 1 else "benchmark/cap-2/battery.json")
    print("\n".join(probs) if probs else "VALID"); raise SystemExit(1 if probs else 0)
