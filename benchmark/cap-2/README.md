# cap-2 Battery

The second-generation model-capability battery for Argus: 56 tasks, graded across 4 difficulty
tiers and 6 capability families, run once per model under the standard Argus config (skills on)
to locate the small-model capability shelf/cliff with more precision than cap-1.

## The four-tier ladder

Both axes — tool selection difficulty and reasoning-chain depth — rise together as the tier
increases:

| Tier | Tool selection | Chain depth |
|------|-----------------|-------------|
| T1 | One obvious tool | Single call |
| T2 | Right-tool selection among plausible distractors | Single call, but picking correctly matters |
| T3 | — | 2–3 step ordered chain |
| T4 | — | 3+ step chain with a judgment call folded in |

**T3 and T4 always pair a real tool chain with a demanding rubric** — every T3/T4 task has both
`expect.tools_in_order` (a deterministic chain-correctness check) and a `rubric` (for the 0–3
quality judge). There are no judge-only tasks at T3/T4: by the time the chain gets this deep, chain
correctness alone doesn't prove much, so it always ships with a quality bar attached. Judge-only
tasks (no `expect.tools_in_order`, rubric only) are reserved for the `restraint` family at T1/T2,
where the correct behavior is often *not* calling a tool, and there is no chain to check.

## The six families

- **compute** — arithmetic, unit conversion, date/time math: tasks that live entirely inside the
  calculator/time tools.
- **tool-selection** — several plausible tools are available; picking the right one (and not the
  tempting wrong one) is the point.
- **retrieve** — pulling a fact from an external source (web search, a page fetch, geocoding).
- **data-transform** — reading, reshaping, filtering, or writing structured data (the table store,
  extraction, schema work).
- **synthesis** — combining multiple pieces of retrieved/computed information into one coherent
  answer.
- **restraint** — knowing when *not* to act: refusing an unsafe/ambiguous request, asking for
  clarification, or correctly doing nothing.

Every family has at least 4 tasks in the battery; the actual counts are validated in
`tests/test_benchmark.py::test_cap2_battery_is_complete_and_balanced` and are not expected to be
even (some families naturally support more distinct scenarios than others).

## The `solved` metric

The headline metric is **`solved`**: a task counts as solved on a given run when
`chain-correct AND judge >= 2`, collapsed across the `k` repeats the same way `chain_pass` is
(i.e. a task is `solved` if it clears the threshold on enough of its `k` runs). For judge-only
tasks (no chain to check — restraint at T1/T2), `solved` falls back to the judge score alone.

`chain_pass` (deterministic tool-order correctness) and `judge_mean` (0–3 quality score, averaged
over `k`) remain in the report as diagnostics — they tell you *why* a task failed to solve (bad
chain vs. bad answer quality) — but `solved` is the number to read off the capability curve.

## Dependency policy

- **At least 80% of tasks run with no external dependency** (no `requires` key) — the battery must
  be runnable offline/air-gapped for the common case.
- **Self-hosted Firecrawl** (`requires: "firecrawl"`) is used freely — it's ours, it's not metered,
  and it's the standard way to fetch a real page.
- **`web_search` / `requires: "searxng"` is capped at <= 2 tasks** — SearXNG is metered
  infrastructure and its use is intentionally minimal, present only to exercise the retrieve
  family's most realistic case.
- **No paid third-party APIs.** Anything requiring one is out of scope for this battery.

Tasks whose declared dependency isn't available in the current environment are skipped at run
time, not failed.

## Running it

```
python -m engine.eval.benchmark run \
  --battery benchmark/cap-2/battery.json \
  --model '<label>=<url>|<served>' \
  --params <n> \
  [--mode native|manual] \
  [--baseline]
```

- `--model` — either the bare label `main` (the app's configured model) or
  `label=base_url|served_model_name` for an OpenAI-compatible endpoint.
- `--params` — the model's parameter count in billions; this is the x-axis on the capability
  curve.
- `--mode` — `native` (tool-calling API) or `manual` (prompted tool JSON); omit to use the model's
  default.
- `--baseline` — run with Argus's scaffolding (skills, memory, structured tools) switched off, to
  measure the lift Argus provides over a plain agent loop on the same model.

Results accumulate under `benchmark/cap-2/results/` (once runs begin), one committed JSON per run,
so runs from different sessions and models compose into a single dataset. Regenerate the report
with `python -m engine.eval.benchmark report --battery benchmark/cap-2/battery.json`.

## Status

cap-2 is **frozen** once validated (`test_cap2_battery_validates` +
`test_cap2_battery_is_complete_and_balanced` both green), the same way cap-1's `battery.json` is
frozen — no further task additions or edits to this file. Further growth of the battery happens
in a future `cap-3`.
