# cap-1 Battery

The **founding** model-capability battery for Argus: 27 tasks across 4 difficulty tiers, run once per
model under the standard config (skills on) to locate the small-model capability shelf/cliff. This is
the battery behind the founding cross-size run in the top-level [README](../README.md).

**Frozen** — `battery.json` is not edited (changes would break the committed, version-keyed results).
Battery growth happens in [`cap-2`](../cap-2/) (56 tasks, 6 families, the stricter `solved` metric) and
future batteries, not here.

## Contents

- `battery.json` — 27 tasks, 4 tiers; each has a `rubric` for the 0–3 judge and an optional `expect`
  tool-chain predicate.
- `fixtures/` — input files a task's `source` references (copied in beside the runner at run time).
- `report.md`, `curve.png`, `stackup[_solved].png`, `model_tiers[_solved].png` — generated from the
  shared `benchmark/results/` by `python -m engine.eval.benchmark report --battery-version cap-1`.

Run it (cap-1 is the default battery, so `--battery` can be omitted):

```
python -m engine.eval.benchmark run --model main --params 35 --mode native
```

See the top-level [benchmark README](../README.md) for the full runner reference and the shared-results
layout.
