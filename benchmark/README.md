# Model-Capability Benchmark

A difficulty-graded benchmark for tracking how well *Argus-on-model-X* performs agentic tasks **across model sizes and with the scaffolding on vs off** — the instrument for locating the small-model capability shelf/cliff and measuring what the harness actually adds.

## Layout

Each **battery** is a self-contained, versioned test set in its own folder; shared infrastructure lives at the root.

```
benchmark/
├── README.md          ← this file
├── charts.py          ← shared chart generators (battery-version-aware)
├── results/           ← shared — every run's JSON, filenames keyed by battery_version
├── cap-1/             ← battery cap-1 (frozen): 27 tasks, 4 tiers
│   ├── battery.json  fixtures/  report.md
│   └── curve.png  stackup[_solved].png  model_tiers[_solved].png
└── cap-2/             ← battery cap-2: 56 tasks, 4 tiers, `solved` metric, honest ladder
    ├── battery.json  fixtures/  validate_battery.py  README.md  report.md
    └── curve.png  stackup[_solved].png  model_tiers[_solved].png
```

`results/` is shared on purpose: filenames encode the `battery_version`, so runs from different
sessions, models, and batteries compose into one dataset that the report/chart generators filter by
battery.

## Run it (one model at a time)

```
# label each run with the model's param count (billions) — that's the x-axis. --battery defaults to
# cap-1; point it at another battery's battery.json to run that one:
python -m engine.eval.benchmark run --model main --params 35 --mode native
python -m engine.eval.benchmark run --battery benchmark/cap-2/battery.json \
    --model 'small=http://localhost:8001/v1|Qwen2.5-3B-Instruct' --params 3 --mode manual

# add --baseline to disable the toggleable scaffolding and measure Argus's lift over a plain loop.

# regenerate a battery's report + charts from the accumulated results:
python -m engine.eval.benchmark report --battery-version cap-2
```

`report` writes into that battery's folder: `report.md`, `curve.png`, and `stackup`/`model_tiers`
charts in both `chain_pass` (canonical) and `solved` (`*_solved.png`) metrics.

## Batteries

- **cap-1** — the founding battery (27 tasks, 4 tiers). Frozen. Shows the difficulty×size shelf.
- **cap-2** — the expanded battery (56 tasks, 14/tier, 6 task families). Adds the `solved` metric
  (`chain_pass ∧ judge≥2`) and a ladder that's honest on both the chain and judge axes. See
  `cap-2/README.md` for the design and `cap-2/validate_battery.py` for the structural validator.

A task is graded into one of 4 difficulty tiers; each has a `rubric` for the 0–3 Opus judge and an
optional `expect` chain-predicate. `requires`-gated tasks (internet/pdf/searxng) are skipped when the
dependency is absent. Bump `battery_version` on any change to a battery.
