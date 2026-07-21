# Model-Capability Benchmark

A single-arm, difficulty-graded benchmark for tracking how well *Argus-on-model-X* performs agentic tasks **across model sizes** — the instrument for locating the small-model capability shelf/cliff. Unlike the skill-eval A/B, this runs the frozen `battery.json` once per model under the **standard config** (skills on), scores each task (deterministic chain + Opus quality judge), and accumulates a committed, labeled dataset.

## Run it (one model at a time)

```
# label each run with the model's param count (billions) — that's the x-axis:
python -m engine.eval.benchmark run --model main --params 35 --mode native
python -m engine.eval.benchmark run --model 'fast=http://192.168.0.93:8001/v1|fast' --params 3 --mode manual

# after you have >=1 result, regenerate the curve + report:
python -m engine.eval.benchmark report
```

Results accumulate in `results/` (committed JSON), so runs from different sessions/models compose into one dataset. The curve is `curve.png`; the read is `report.md`.

## Battery

`battery.json` (versioned — bump `battery_version` on any change). Tasks are graded into 4 difficulty tiers; each has a `rubric` for the 0–3 judge and an optional `expect` chain-predicate. `requires`-gated tasks (internet/pdf/searxng) are skipped when the dependency is absent. Fixtures live in `fixtures/`.
