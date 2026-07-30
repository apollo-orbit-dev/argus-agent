"""Model-capability benchmark — run a frozen graded task battery once per model under the STANDARD
Argus config (skills on), score chain + Opus judge, accumulate committed results labeled by param
count, and plot a per-tier metric-vs-size curve.

  python -m engine.eval.benchmark run --model main --params 35 --mode native
  python -m engine.eval.benchmark run --model 'fast=http://host/v1|fast' --params 3 --mode manual
  python -m engine.eval.benchmark report
  python -m engine.eval.benchmark rejudge --battery benchmark/cap-2/battery.json --tasks t1_calc_percent

Single-arm (no skill ablation) — this measures "how good is the deployed system on model X", the
input to the small-model capability curve. Reuses engine.eval.{scoring,judge,capture,judge_runner}.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "benchmark"
FIXTURES = BENCH / "cap-1" / "fixtures"     # the default battery's fixtures (cap-1); others resolve beside their own battery.json
RESULTS = BENCH / "results"                 # shared across batteries — filenames are keyed by battery_version
PASS_FRACTION = 0.6
JUDGE_SOLVED_MIN = 2          # a run is "solved" iff it chained correctly AND judge_score >= this
ABORT_ISSUES = ("stuck_repeating",)   # observer issues that END the turn (v1: exact-repeat only)
FROZEN_BATTERIES = ("cap-1",)         # published + closed: `rejudge` refuses to touch these

# ------------------------------- pure helpers (unit-tested) -------------------------------


def run_aborted(observer) -> bool:
    """True if this run's observer issue list contains an issue that ends the turn (loop.py returns
    instead of continuing). Nudge-only issues (repeat_nudge, fuzzy_repeat_nudge, create_without_verify)
    don't count — the loop kept going after those. Pure so it's unit-testable without an engine."""
    return any(i in ABORT_ISSUES for i in (observer or []))


def task_verdict(runs: list, k: int) -> dict:
    """Collapse a task's k runs into {chain_pass, judge_mean, solved, answered, abort_rate}.
    chain_pass is None when the task has no `expect` (judge-only). solved = chained-correctly
    (vacuous if no chain) AND judge >= JUDGE_SOLVED_MIN (vacuous if unjudged), per run, then
    collapsed like chain_pass (>=ceil(k*frac)).

    answered = the judge scored the run >= JUDGE_SOLVED_MIN, IGNORING chain_correct entirely,
    collapsed the same way. It is the second headline metric (argus-3zn): a capable model can
    answer a trivial task inside its reasoning pass instead of calling the declared tool, scoring
    judge=3 with an empty tool list — right, but `solved` False. The GAP between answered and
    solved is exactly that population.

    DELIBERATE ASYMMETRY WITH solved, do not "harmonise" it: solved treats a missing judge score
    as vacuously true, but answered is **None** — not True — when a task has no judge verdict at
    all. answered IS the judge axis; with no judge there is no verdict, and vacuous-true would
    silently inflate the metric for unjudged tasks.

    abort_rate is the fraction of runs the loop itself gave up on (stuck_repeating); it does NOT
    enter solved/answered/chain_pass — it's diagnostic only. None when no run carries observer
    data (legacy runs), never fabricated as 0.0."""
    thr = math.ceil(k * PASS_FRACTION)
    chained = [r for r in runs if r.get("chain_correct") is not None]
    chain_pass = (sum(1 for r in chained if r["chain_correct"]) >= thr) if chained else None
    js = [r["judge_score"] for r in runs if r.get("judge_score") is not None]
    judge_mean = (sum(js) / len(js)) if js else None

    def _run_solved(r):
        c, j = r.get("chain_correct"), r.get("judge_score")
        chain_ok = True if c is None else c
        judge_ok = True if j is None else (j >= JUDGE_SOLVED_MIN)
        return chain_ok and judge_ok
    solved = (sum(1 for r in runs if _run_solved(r)) >= thr) if runs else None
    answered = (sum(1 for j in js if j >= JUDGE_SOLVED_MIN) >= thr) if js else None

    aborts = []
    for r in runs:
        if "aborted" in r:
            aborts.append(bool(r["aborted"]))
        elif "observer" in r:
            aborts.append(run_aborted(r["observer"]))
    abort_rate = (sum(1 for a in aborts if a) / len(aborts)) if aborts else None
    return {"chain_pass": chain_pass, "judge_mean": judge_mean, "solved": solved,
            "answered": answered, "abort_rate": abort_rate}


def aggregate(tasks: list) -> dict:
    """tasks: [{tier, chain_pass: bool|None, judge_mean: float|None, abort_rate: float|None, skipped:
    bool}]. Returns per-tier and overall {chain_pass: rate over tasks-with-a-chain-verdict, judge_mean:
    mean over judged tasks, solved: rate of chain-AND-judge>=2 over tasks with a solved verdict,
    answered: rate of judge>=2 (chain ignored) over tasks with an answered verdict — i.e. skipping the
    Nones, which are the unjudged tasks, abort_rate: MEAN (not thresholded) of non-None task
    abort_rates — a rate stays a rate, unlike the binary chain_pass/solved collapse, n, skipped}."""
    def roll(items):
        active = [t for t in items if not t.get("skipped")]
        cp = [t["chain_pass"] for t in active if t.get("chain_pass") is not None]
        jm = [t["judge_mean"] for t in active if t.get("judge_mean") is not None]
        sv = [t["solved"] for t in active if t.get("solved") is not None]
        an = [t["answered"] for t in active if t.get("answered") is not None]
        ar = [t["abort_rate"] for t in active if t.get("abort_rate") is not None]
        return {"chain_pass": (sum(1 for x in cp if x) / len(cp)) if cp else None,
                "judge_mean": (sum(jm) / len(jm)) if jm else None,
                "solved": (sum(1 for x in sv if x) / len(sv)) if sv else None,
                "answered": (sum(1 for x in an if x) / len(an)) if an else None,
                "abort_rate": (sum(ar) / len(ar)) if ar else None,
                "n": len(active), "skipped": sum(1 for t in items if t.get("skipped"))}
    per_tier = {}
    for tier in sorted({t["tier"] for t in tasks}):
        per_tier[str(tier)] = roll([t for t in tasks if t["tier"] == tier])
    return {"per_tier": per_tier, "overall": roll(tasks)}


def _backfill_solved(result: dict) -> dict:
    """Ensure per_tier/overall carry both headline rates, `solved` and `answered`, recomputing what
    is missing from each task's stored per-run chain_correct/judge_score. Fresh runs already carry
    both; older results predate one or the other.

    EACH METRIC IS GUARDED INDEPENDENTLY, deliberately: every committed result already has a
    non-None overall.solved, so a single `if solved is None: return` guard (what this used to be)
    would early-return for the whole corpus and backfill `answered` for nothing — the new column
    would render `—` on every published row. Because every stored run carries judge_score, all
    committed results ARE fully back-derivable for answered, with no re-runs.

    abort-rate is NOT backfillable: unlike chain_correct/judge_score, no observer data was ever
    written to disk for pre-argus-92a results (_run_task dropped it before serialization), so there
    is nothing here to recompute from. Legacy files simply keep abort_rate absent -> None at render
    time; the answered-only path below never touches it."""
    overall = result.get("overall") or {}
    need_solved = overall.get("solved") is None
    need_answered = overall.get("answered") is None
    if not (need_solved or need_answered):
        return result

    k = result.get("k", 3)
    tasks = []
    for t in result.get("tasks", []):
        v = task_verdict(t.get("runs", []), k) if t.get("runs") else {}
        tasks.append({"tier": t["tier"], "chain_pass": t.get("chain_pass"),
                      "judge_mean": t.get("judge_mean"), "solved": v.get("solved"),
                      "answered": v.get("answered"), "abort_rate": v.get("abort_rate"),
                      "skipped": t.get("skipped", False)})
    agg = aggregate(tasks)
    if need_solved:      # legacy: nothing trustworthy in per_tier/overall — replace wholesale
        result["per_tier"], result["overall"] = agg["per_tier"], agg["overall"]
    elif need_answered:  # published rows: graft answered on, leave every existing number alone
        result["overall"]["answered"] = agg["overall"].get("answered")
        for tier, pt in (result.get("per_tier") or {}).items():
            pt["answered"] = agg["per_tier"].get(tier, {}).get("answered")
    return result


def build_series(results: list, battery_version: str, metric: str = "chain_pass") -> dict:
    """Group committed result dicts (of one battery_version) into per-tier series sorted by params:
    {tier: [(params, <metric>, judge_mean), ...]} where <metric> is the selected series (default chain_pass)."""
    rows = [r for r in results if r.get("battery_version") == battery_version]
    # One point per model size on the curve: when the same params was run more than once (a re-run
    # at a higher token budget, or the same model in native vs manual mode), plot the best-demonstrated
    # run — highest max_tokens first, then highest overall chain-pass. The report TABLE still lists
    # every run.
    def _rank(r):
        # scaffold-on always wins (the size curve shows the product's real capability; a baseline/
        # ablation run is a separate view), then higher token budget, then higher chain-pass.
        return (1 if r.get("scaffold", "on") == "on" else 0,
                (r.get("max_tokens") or 0), (r.get("overall", {}) or {}).get("chain_pass") or 0)
    best = {}
    for r in rows:
        p = r.get("params", 0)
        cur = best.get(p)
        if cur is None or _rank(r) > _rank(cur):
            best[p] = r
    rows = sorted(best.values(), key=lambda r: r.get("params", 0))
    tiers = sorted({t for r in rows for t in r.get("per_tier", {})}, key=int)
    series = {}
    for tier in tiers:
        pts = []
        for r in rows:
            pt = r.get("per_tier", {}).get(tier)
            if pt:
                pts.append((r["params"], pt.get(metric), pt.get("judge_mean")))
        series[tier] = pts
    return series


# The scaffolding the `--baseline` arm switches OFF, to measure Argus's lift over a plain agent loop.
# These are the config-gated *behavioral* layers (README "Small-model scaffolding"). NOT disabled:
# the tools themselves, the tight tool contracts (structural — validated identifiers, bound params),
# the base loop/prompt, and the tool-calling mode (a separate axis, held constant per run). So a
# baseline number is "same model + same tools, minus the toggleable scaffolding", not a naive harness.
BASELINE_OVERRIDES = {
    "enable_observer": False,          # loop-health watchdog: no nudge/stop on thrash
    "enable_action_verify": False,     # no post-action over-claim verifier
    "enable_clarify": False,           # no clarify tool — the model must guess, not ask
    "enable_rules": False,             # no standing behavioral rules injected each turn
    "enable_rules_autodetect": False,
    "enable_memory": False,            # no memory recall/injection (empty in isolated runs anyway)
    "enable_memory_autoextract": False,
    "adaptive_thinking": False,        # no per-turn reasoning router
    "skill_selection_mode": "model_driven",  # drop explicit-first / deterministic skill selection
}


def resolve_config(model_spec: str, mode: str | None, baseline: bool = False,
                   disclosure: str | None = None):
    """Build a Config with the model endpoint + mode overridden. model_spec: 'main' (configured default)
    or 'name=base_url|model'. baseline=True additionally disables the toggleable scaffolding.

    `disclosure` is progressive tool disclosure's own AXIS, not scaffolding — it is deliberately NOT
    in BASELINE_OVERRIDES, so a --baseline run and a --disclosure run vary independently."""
    from config import Config
    cfg = Config()
    updates = {}
    _, _, rhs = model_spec.partition("=")
    if rhs:
        base_url, _, model = rhs.partition("|")
        updates["model_base_url"] = base_url.strip()
        if model.strip():
            updates["model_name"] = model.strip()
    if mode:
        updates["tool_calling_mode"] = mode
    if baseline:
        updates.update(BASELINE_OVERRIDES)
    if disclosure is not None:
        updates["tool_disclosure_mode"] = disclosure
    return cfg.model_copy(update=updates) if updates else cfg


def _dep_available(requires: str, cfg) -> bool:
    if requires == "pdf":
        if not getattr(cfg, "enable_pdf", True):
            return False
        try:
            import weasyprint  # noqa: F401
            return True
        except Exception:
            return False
    if requires == "searxng":
        return bool(cfg.searxng_base_url)
    if requires in ("firecrawl",):
        return bool(cfg.firecrawl_base_url)
    return True   # 'internet' and anything else: assume available


# ------------------------------- run orchestration -------------------------------


async def _run_task(cfg, judge_fn, task: dict, k: int, timeout: float, fixtures_dir: Path = FIXTURES) -> dict:
    from engine.engine import Engine
    from engine.eval.capture import run_and_capture
    from engine.eval.scoring import score_case
    from engine.eval.judge import build_judge_prompt, parse_judge_reply

    if task.get("requires") and not _dep_available(task["requires"], cfg):
        return {"id": task["id"], "tier": task["tier"], "skipped": True,
                "reason": f"requires {task['requires']}", "chain_pass": None, "judge_mean": None}

    runs = []
    for i in range(k):
        tmp = tempfile.mkdtemp(prefix="bench-")
        try:
            engine = Engine(cfg, data_dir=tmp)
            for src in ([task["source"]] if isinstance(task.get("source"), str) else task.get("source") or []):
                dst = Path(engine._workspace_dir)
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy(fixtures_dir / src, dst / src)
            cap = await run_and_capture(engine, f"bench-{task['id']}-{i}", task["prompt"], timeout)
            # create_table_args is stored (not just used in-flight) because build_judge_prompt's
            # _outcome renders the created schemas: without it on disk, a later `rejudge` would
            # build a DIFFERENT prompt than the original judging saw. Results written before this
            # lack the key and re-judge with "tables created: (none)" — a known fidelity gap.
            r = {"tools": cap["tools"], "create_table_args": cap.get("create_table_args") or [],
                 "error": cap["error"], "final": cap["final"],
                 "observer": cap["observer"], "aborted": run_aborted(cap["observer"])}
            if "expect" in task:
                r["chain_correct"] = score_case(task["expect"], cap)["chain_correct"]
            if task.get("rubric") and not cap["error"] and judge_fn is not None:
                try:
                    text = await judge_fn(build_judge_prompt(task, cap))
                    r["judge_score"] = parse_judge_reply(text)["score"]
                except Exception as e:      # noqa: BLE001 - unjudged cell, not a crash
                    r["judge_error"] = f"{type(e).__name__}: {e}"
            runs.append(r)
            js = r.get("judge_score")
            print(f"  {task['id']:<18} tier {task['tier']} run {i+1}/{k}: "
                  f"{r.get('chain_correct', '-')!s:<5} judge={js if js is not None else '-'} {cap['tools']}"
                  + (f" ERR {cap['error']}" if cap["error"] else ""), flush=True)
        except Exception as e:              # noqa: BLE001 - a bad build must not abort the run
            cell = {"error": f"{type(e).__name__}: {e}", "tools": [], "aborted": False, "observer": []}
            if "expect" in task:            # a crashed cell is a real chain failure, not a silent drop
                cell["chain_correct"] = False
            runs.append(cell)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    v = task_verdict(runs, k)
    return {"id": task["id"], "tier": task["tier"], "category": task.get("category"),
            "skipped": False, **v, "runs": runs}


async def run_model(model_spec: str, params: int, mode: str | None, k: int, judge_spec: str,
                    battery_path: Path, timeout: float, baseline: bool = False,
                    disclosure: str | None = None) -> dict:
    from engine.eval.judge_runner import make_judge
    battery = json.loads(battery_path.read_text())
    cfg = resolve_config(model_spec, mode, baseline, disclosure)
    judge_fn = make_judge(judge_spec)
    # Fixtures live in a `fixtures/` dir beside the battery file, so a battery in its own subdir
    # (e.g. benchmark/cap-2/battery.json) uses benchmark/cap-2/fixtures/. cap-1
    # (benchmark/cap-1/battery.json) resolves to benchmark/cap-1/fixtures/.
    fixtures_dir = battery_path.parent / "fixtures"
    results = []
    for task in battery["tasks"]:
        results.append(await _run_task(cfg, judge_fn, task, k, timeout, fixtures_dir))
    agg = aggregate(results)
    name = model_spec.partition("=")[0]
    return {"model": name, "params": params, "mode": mode or cfg.tool_calling_mode,
            "scaffold": "off" if baseline else "on",   # Argus scaffolding on (full config) vs off (baseline arm)
            "disclosure": cfg.tool_disclosure_mode,    # progressive tool disclosure arm (its own axis)
            "max_tokens": cfg.model_max_tokens,   # completion cap this run used (reasoning models need headroom)
            "battery_version": battery["battery_version"], "k": k,
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "per_tier": agg["per_tier"], "overall": agg["overall"], "tasks": results}


def _write_result(result: dict) -> Path:
    RESULTS.mkdir(parents=True, exist_ok=True)
    stamp = result["date"].replace(":", "").replace("-", "")[:15]   # to the second (avoid overwrites)
    out = RESULTS / f"{result['model']}-{result['battery_version']}-{stamp}.json"
    out.write_text(json.dumps(result, indent=2, default=str))
    # update the index
    idx_path = RESULTS / "index.json"
    idx = json.loads(idx_path.read_text()) if idx_path.exists() else []
    idx.append({k: result[k] for k in ("model", "params", "mode", "battery_version", "date")} | {"file": out.name})
    idx_path.write_text(json.dumps(idx, indent=2))
    return out


# ------------------------------- re-judge (no model re-runs) -------------------------------


def captured_from_run(run: dict) -> dict:
    """Rebuild the `captured` dict build_judge_prompt needs from a STORED run. Judging is cheap and
    generation is not, so a stored run is enough to regenerate a judge score — but only these four
    keys survive to disk, so the reconstruction is explicit rather than passing the run itself."""
    return {"tools": list(run.get("tools") or []),
            "create_table_args": list(run.get("create_table_args") or []),
            "observer": list(run.get("observer") or []),
            "final": run.get("final") or ""}


def rejudgeable(run: dict) -> str | None:
    """None if this stored run can be re-judged, else the reason it can't. A run that cannot be
    re-judged is LEFT ALONE — never scored 0. Inventing a 0 for a run whose output was never
    recorded would manufacture a model failure out of a harness gap."""
    if run.get("error"):
        return "errored"            # the original pass didn't judge these either (`not cap["error"]`)
    if not (run.get("final") or "").strip():
        return "no_final"
    return None


async def rejudge_result(result: dict, battery: dict, judge_fn, task_ids=None,
                         concurrency: int = 1) -> dict:
    """Re-score a stored result IN PLACE from its own `final` text against the CURRENT battery, then
    recompute every task verdict and the aggregate so `solved`/`answered`/`judge_mean` follow the new
    scores instead of the stale stored ones. No model is re-run.

    task_ids: restrict to these battery task ids (None = all). Restricting is the honest default when
    only some rubrics changed — re-judging an UNCHANGED rubric moves published numbers by judge noise
    alone, which is the very thing this whole exercise is trying to stop measuring.

    Returns a report dict; mutates `result`."""
    from engine.eval.judge import build_judge_prompt, parse_judge_reply

    cases = {t["id"]: t for t in battery.get("tasks", [])}
    stats = {"judged": 0, "changed": 0, "errored": 0, "no_final": 0,
             "no_rubric": 0, "unknown_task": 0, "judge_error": 0, "unparsed": 0, "task_skipped": 0}
    changes: list[dict] = []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(case, task, idx, run):
        async with sem:
            try:
                text = await judge_fn(build_judge_prompt(case, captured_from_run(run)))
            except Exception as e:      # noqa: BLE001 - one bad judge call must not lose the pass
                stats["judge_error"] += 1
                run["judge_error"] = f"{type(e).__name__}: {e}"
                return
        score = parse_judge_reply(text)["score"]
        if score is None:               # unparseable reply is not evidence of a 0
            stats["unparsed"] += 1
            return
        old = run.get("judge_score")
        run["judge_score"] = score
        run.pop("judge_error", None)
        stats["judged"] += 1
        if old != score:
            stats["changed"] += 1
            changes.append({"task": task["id"], "run": idx, "old": old, "new": score})

    jobs = []
    for task in result.get("tasks", []):
        case = cases.get(task.get("id"))
        if case is None:
            stats["unknown_task"] += 1
            continue
        if task_ids is not None and task["id"] not in task_ids:
            continue
        if task.get("skipped") or not task.get("runs"):
            stats["task_skipped"] += 1
            continue
        if not case.get("rubric"):
            stats["no_rubric"] += 1
            continue
        for idx, run in enumerate(task["runs"]):
            why = rejudgeable(run)
            if why:
                stats[why] += 1
                continue
            jobs.append(_one(case, task, idx, run))
    if jobs:
        await asyncio.gather(*jobs)

    # Verdicts + aggregate are DERIVED, so recompute them from the runs rather than patching the
    # stored numbers — otherwise `solved`/`answered` would keep reporting the pre-re-judge collapse.
    k = result.get("k", 3)
    for task in result.get("tasks", []):
        if task.get("runs"):
            task.update(task_verdict(task["runs"], k))
    agg = aggregate(result.get("tasks", []))
    result["per_tier"], result["overall"] = agg["per_tier"], agg["overall"]
    return {"stats": stats, "changes": changes}


# ------------------------------- report / curve -------------------------------


def _load_results() -> list:
    return [_backfill_solved(json.loads(p.read_text()))
            for p in sorted(RESULTS.glob("*.json")) if p.name != "index.json"]


def render_report(battery_version: str) -> tuple[str, bool]:
    results = [r for r in _load_results() if r.get("battery_version") == battery_version]
    if not results:
        return f"No results yet for battery {battery_version}.", False
    results.sort(key=lambda r: r.get("params", 0))
    tiers = sorted({t for r in results for t in r.get("per_tier", {})}, key=int)
    lines = [f"# Model-Capability Benchmark — `{battery_version}`", "",
             f"{len(results)} model(s), by param count. Chain = deterministic tool-chain pass-rate; "
             "Judge = Opus quality mean (0–3). A tier's line falling off below some size is the shelf.", "",
             "| model | params (B) | mode | scaffold | disclosure | max_tok | solved | answered | abort | " + " | ".join(f"T{t} chain / judge" for t in tiers) + " | overall |",
             "|---|---|---|---|---|---|---|---|---|" + "|".join(["---"] * (len(tiers) + 1)) + "|"]
    def _pct(x):
        return "—" if x is None else f"{x:.0%}"

    def _q(x):
        return "—" if x is None else f"{x:.1f}"

    for r in results:
        cells = []
        for t in tiers:
            pt = r.get("per_tier", {}).get(t, {})
            cells.append(f"{_pct(pt.get('chain_pass'))} / {_q(pt.get('judge_mean'))}")
        ov = r.get("overall", {})
        cells.append(f"{_pct(ov.get('chain_pass'))} / {_q(ov.get('judge_mean'))}")
        mt = r.get("max_tokens")
        solved = ov.get("solved")
        # baseline arm runs with enable_observer=False (BASELINE_OVERRIDES) -- the observer physically
        # cannot fire, so an abort_rate of 0% there would be a misleading artifact, not a real measurement.
        abort_cell = "n/a" if r.get("scaffold") == "off" else _pct(ov.get("abort_rate"))
        lines.append(f"| {r['model']} | {r['params']} | {r.get('mode', '?')} | {r.get('scaffold', 'on')} | "
                     f"{r.get('disclosure', 'off')} | "
                     f"{mt if mt is not None else '—'} | {_pct(solved)} | {_pct(ov.get('answered'))} | "
                     f"{abort_cell} | " + " | ".join(cells) + " |")
    lines += ["",
              "`max_tok` = the completion-token cap for the run. `—` = not recorded (runs predating this "
              "field; the standard-config default is 2048). Runs at different caps are not strictly "
              "comparable — a reasoning model can exhaust a low cap mid-thought, so a higher cap is a "
              "fairer read of its capability but a looser comparison across sizes.",
              "`answered` = the judge accepted the answer (>= 2), whether or not the declared tools "
              "were used; `solved` additionally requires the tool chain. The GAP between `answered` "
              "and `solved` is the share of tasks the model got RIGHT WITHOUT using the declared "
              "tools — a reasoning model answering inside its reasoning pass. `—` = no judge verdict "
              "(unjudged tasks are not counted as vacuously answered).",
              "`abort` = share of runs the loop itself ended on `stuck_repeating` (an exact-repeat "
              "tool-call thrash); diagnostic only, not part of `solved`. `—` = predates the metric "
              "(no observer data was ever recorded for that result). `n/a` = observer disabled "
              "(baseline arm — `--baseline` turns `enable_observer` off, so it can't fire).",
              # Historical note, kept deliberately: this cost three days and a dozen wrong
              # hypotheses, and the same flag will be enabled again by someone eventually.
              "**A NOTE ON SERVING CONFIGURATION.** Two cells in this table were once badly wrong — `Qwen3.6-35B-A3B native/off` at 112/168 runs producing no usable tool call, and `Qwen3.6-27B native/on` at 95/168 — and both are now re-generated and valid. The cause was vLLM **speculative decoding** (`--speculative-config {\"method\":\"mtp\",...}`): draft tokens accepted that should not have been, corrupting output at unchanged latency. Removing that one flag took the 35B cell from 112/168 broken to 0/168 and its `solved` from 0.250 to 0.571, and the 27B cell from 95/168 to 9/168 and 0.357 to 0.625. It was NOT the chat template and NOT the tool-call parser — the payloads were token-corrupted, not mis-formatted, so no parser could recover them. Flat latency across the onset is the diagnostic tell: a resource problem slows down; speculative decoding does not. Manual-mode answers were degraded too, just less visibly. If a future row looks inexplicably bad in native mode, check the serving flags before anything else."]
    return "\n".join(lines) + "\n", True


def render_curve(battery_version: str, out: Path) -> bool:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    series = build_series(_load_results(), battery_version)
    if not series or not any(series.values()):
        return False
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))
    for tier, pts in series.items():
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ax1.plot(xs, [None if p[1] is None else p[1] * 100 for p in pts], marker="o", label=f"Tier {tier}")
        ax2.plot(xs, [p[2] for p in pts], marker="o", label=f"Tier {tier}")
    ax1.set(title="Chain pass-rate vs model size", xlabel="params (B)", ylabel="pass-rate (%)", ylim=(-5, 105))
    ax2.set(title="Judge quality vs model size", xlabel="params (B)", ylabel="quality (0–3)", ylim=(-0.1, 3.1))
    for ax in (ax1, ax2):
        ax.grid(True, alpha=0.3); ax.legend()
    fig.suptitle(f"Argus model-capability benchmark — {battery_version}")
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return True


# ------------------------------- CLI -------------------------------


def _cmd_rejudge(args) -> int:
    from engine.eval.judge_runner import make_judge
    battery = json.loads(Path(args.battery).read_text())
    bv = battery["battery_version"]
    if bv in FROZEN_BATTERIES:
        print(f"refusing: battery {bv!r} is frozen (published); re-judging it would rewrite closed numbers")
        return 2
    task_ids = set(t.strip() for t in args.tasks.split(",") if t.strip()) if args.tasks else None
    if task_ids:
        unknown = task_ids - {t["id"] for t in battery["tasks"]}
        if unknown:
            print(f"unknown task ids for {bv}: {sorted(unknown)}")
            return 2
    if args.result:
        paths = [Path(x) for x in args.result]
    else:
        paths = [p for p in sorted(RESULTS.glob("*.json")) if p.name != "index.json"
                 and json.loads(p.read_text()).get("battery_version") == bv]
    total = {"judged": 0, "changed": 0}
    for path in paths:
        result = json.loads(path.read_text())
        if result.get("battery_version") != bv:
            print(f"skip {path.name}: battery_version {result.get('battery_version')!r} != {bv!r}")
            continue
        before = dict(result.get("overall") or {})
        if args.dry_run:
            n = sum(1 for t in result.get("tasks", [])
                    if not t.get("skipped") and (task_ids is None or t["id"] in task_ids)
                    for r in t.get("runs", []) if rejudgeable(r) is None)
            print(f"{path.name}: would judge {n} run(s)")
            continue
        rep = asyncio.run(rejudge_result(result, battery, make_judge(args.judge),
                                         task_ids=task_ids, concurrency=args.concurrency))
        result.setdefault("rejudged", []).append({
            "date": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "judge": args.judge, "tasks": sorted(task_ids) if task_ids else "all",
            "stats": rep["stats"]})
        path.write_text(json.dumps(result, indent=2, default=str))
        after = result.get("overall") or {}
        total["judged"] += rep["stats"]["judged"]
        total["changed"] += rep["stats"]["changed"]

        def _d(key):
            a, b = before.get(key), after.get(key)
            return f"{key}: {a if a is None else round(a, 4)} -> {b if b is None else round(b, 4)}"
        print(f"{path.name}: {rep['stats']}\n    " + "  ".join(_d(k) for k in ("judge_mean", "solved", "answered")))
    if not args.dry_run:
        print(f"\ntotal: {total['judged']} run(s) re-judged, {total['changed']} score(s) changed")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="benchmark", description="Argus model-capability benchmark")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the battery on one model")
    r.add_argument("--model", default="main")
    r.add_argument("--params", type=int, required=True, help="param count in billions (the x-axis)")
    r.add_argument("--mode", default=None, choices=["native", "manual", "native_finish"])
    r.add_argument("--k", type=int, default=3)
    r.add_argument("--judge", default="claude:opus")
    r.add_argument("--battery", default=str(BENCH / "cap-1" / "battery.json"))
    r.add_argument("--timeout", type=float, default=180.0)
    r.add_argument("--baseline", action="store_true",
                   help="disable the toggleable scaffolding (observer/verifier/clarify/rules/…) to "
                        "measure Argus's lift over a plain agent loop")
    r.add_argument("--disclosure", default=None,
                   choices=["off", "keyword", "embedding", "hybrid"],
                   help="progressive tool disclosure arm: advertise only the K most relevant tools "
                        "per turn (default: leave the configured value alone)")
    rj = sub.add_parser("rejudge", help="re-score stored results from their own final text against "
                                        "the current battery (no model re-runs)")
    rj.add_argument("--battery", default=str(BENCH / "cap-2" / "battery.json"))
    rj.add_argument("--judge", default="claude:opus")
    rj.add_argument("--tasks", default=None,
                    help="comma-separated battery task ids to re-judge (default: all). Restrict this "
                         "to the tasks whose RUBRIC changed — re-judging an unchanged rubric only "
                         "adds judge noise to published numbers.")
    rj.add_argument("--result", action="append", default=None,
                    help="result file to re-judge (repeatable; default: every stored result of this "
                         "battery_version)")
    rj.add_argument("--concurrency", type=int, default=1)
    rj.add_argument("--dry-run", action="store_true", help="report what would be judged, call nothing")
    rep = sub.add_parser("report", help="regenerate report.md + curve.png from the results")
    rep.add_argument("--battery-version", default=None)
    args = p.parse_args(argv)

    if args.cmd == "rejudge":
        return _cmd_rejudge(args)

    if args.cmd == "run":
        result = asyncio.run(run_model(args.model, args.params, args.mode, args.k, args.judge,
                                       Path(args.battery), args.timeout, args.baseline,
                                       args.disclosure))
        out = _write_result(result)
        print(f"\nresult: {out}")
        bv = result["battery_version"]
    else:
        bv = args.battery_version
        if not bv:
            res = _load_results()
            if not res:
                print("no results yet"); return 1
            bv = sorted(res, key=lambda r: r["date"])[-1]["battery_version"]

    bench_dir = BENCH / bv                       # per-battery folder: benchmark/<battery_version>/
    bench_dir.mkdir(parents=True, exist_ok=True)
    md, ok = render_report(bv)
    (bench_dir / "report.md").write_text(md)
    curve_ok = render_curve(bv, bench_dir / "curve.png")
    print(f"report: {bench_dir / 'report.md'}" + (f"\ncurve: {bench_dir / 'curve.png'}" if curve_ok else ""))
    try:
        from benchmark import charts
        # all three metrics per battery: chain_pass (canonical, matches the curve) + the two
        # headlines, solved (chain AND judge) and answered (judge alone).
        for metric, suffix in (("chain_pass", ""), ("solved", "_solved"), ("answered", "_answered")):
            charts.stackup(str(bench_dir / f"stackup{suffix}.png"), metric=metric, bv=bv)
            charts.model_tiers(str(bench_dir / f"model_tiers{suffix}.png"), metric=metric, bv=bv)
        print(f"charts: {bench_dir}/stackup[_solved|_answered].png, "
              f"model_tiers[_solved|_answered].png")
    except ImportError:
        print("note: matplotlib not available — skipped stackup/model_tiers charts")
    return 0


if __name__ == "__main__":
    sys.exit(main())
