"""Model-capability benchmark — run a frozen graded task battery once per model under the STANDARD
Argus config (skills on), score chain + Opus judge, accumulate committed results labeled by param
count, and plot a per-tier metric-vs-size curve.

  python -m engine.eval.benchmark run --model main --params 35 --mode native
  python -m engine.eval.benchmark run --model 'fast=http://host/v1|fast' --params 3 --mode manual
  python -m engine.eval.benchmark report

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
FIXTURES = BENCH / "fixtures"
RESULTS = BENCH / "results"
PASS_FRACTION = 0.6
JUDGE_SOLVED_MIN = 2          # a run is "solved" iff it chained correctly AND judge_score >= this

# ------------------------------- pure helpers (unit-tested) -------------------------------


def task_verdict(runs: list, k: int) -> dict:
    """Collapse a task's k runs into {chain_pass, judge_mean, solved}. chain_pass is None when the
    task has no `expect` (judge-only). solved = chained-correctly (vacuous if no chain) AND judge >=
    JUDGE_SOLVED_MIN (vacuous if unjudged), per run, then collapsed like chain_pass (>=ceil(k*frac))."""
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
    return {"chain_pass": chain_pass, "judge_mean": judge_mean, "solved": solved}


def aggregate(tasks: list) -> dict:
    """tasks: [{tier, chain_pass: bool|None, judge_mean: float|None, skipped: bool}]. Returns per-tier
    and overall {chain_pass: rate over tasks-with-a-chain-verdict, judge_mean: mean over judged tasks,
    n, skipped}."""
    def roll(items):
        active = [t for t in items if not t.get("skipped")]
        cp = [t["chain_pass"] for t in active if t.get("chain_pass") is not None]
        jm = [t["judge_mean"] for t in active if t.get("judge_mean") is not None]
        sv = [t["solved"] for t in active if t.get("solved") is not None]
        return {"chain_pass": (sum(1 for x in cp if x) / len(cp)) if cp else None,
                "judge_mean": (sum(jm) / len(jm)) if jm else None,
                "solved": (sum(1 for x in sv if x) / len(sv)) if sv else None,
                "n": len(active), "skipped": sum(1 for t in items if t.get("skipped"))}
    per_tier = {}
    for tier in sorted({t["tier"] for t in tasks}):
        per_tier[str(tier)] = roll([t for t in tasks if t["tier"] == tier])
    return {"per_tier": per_tier, "overall": roll(tasks)}


def build_series(results: list, battery_version: str) -> dict:
    """Group committed result dicts (of one battery_version) into per-tier series sorted by params:
    {tier: [(params, chain_pass, judge_mean), ...]}."""
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
                pts.append((r["params"], pt.get("chain_pass"), pt.get("judge_mean")))
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


def resolve_config(model_spec: str, mode: str | None, baseline: bool = False):
    """Build a Config with the model endpoint + mode overridden. model_spec: 'main' (configured default)
    or 'name=base_url|model'. baseline=True additionally disables the toggleable scaffolding."""
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


async def _run_task(cfg, judge_fn, task: dict, k: int, timeout: float) -> dict:
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
                shutil.copy(FIXTURES / src, dst / src)
            cap = await run_and_capture(engine, f"bench-{task['id']}-{i}", task["prompt"], timeout)
            r = {"tools": cap["tools"], "error": cap["error"], "final": cap["final"]}
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
            cell = {"error": f"{type(e).__name__}: {e}", "tools": []}
            if "expect" in task:            # a crashed cell is a real chain failure, not a silent drop
                cell["chain_correct"] = False
            runs.append(cell)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    v = task_verdict(runs, k)
    return {"id": task["id"], "tier": task["tier"], "category": task.get("category"),
            "skipped": False, **v, "runs": runs}


async def run_model(model_spec: str, params: int, mode: str | None, k: int, judge_spec: str,
                    battery_path: Path, timeout: float, baseline: bool = False) -> dict:
    from engine.eval.judge_runner import make_judge
    battery = json.loads(battery_path.read_text())
    cfg = resolve_config(model_spec, mode, baseline)
    judge_fn = make_judge(judge_spec)
    results = []
    for task in battery["tasks"]:
        results.append(await _run_task(cfg, judge_fn, task, k, timeout))
    agg = aggregate(results)
    name = model_spec.partition("=")[0]
    return {"model": name, "params": params, "mode": mode or cfg.tool_calling_mode,
            "scaffold": "off" if baseline else "on",   # Argus scaffolding on (full config) vs off (baseline arm)
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


# ------------------------------- report / curve -------------------------------


def _load_results() -> list:
    return [json.loads(p.read_text()) for p in sorted(RESULTS.glob("*.json")) if p.name != "index.json"]


def render_report(battery_version: str) -> tuple[str, bool]:
    results = [r for r in _load_results() if r.get("battery_version") == battery_version]
    if not results:
        return f"No results yet for battery {battery_version}.", False
    results.sort(key=lambda r: r.get("params", 0))
    tiers = sorted({t for r in results for t in r.get("per_tier", {})}, key=int)
    lines = [f"# Model-Capability Benchmark — `{battery_version}`", "",
             f"{len(results)} model(s), by param count. Chain = deterministic tool-chain pass-rate; "
             "Judge = Opus quality mean (0–3). A tier's line falling off below some size is the shelf.", "",
             "| model | params (B) | mode | scaffold | max_tok | " + " | ".join(f"T{t} chain / judge" for t in tiers) + " | overall |",
             "|---|---|---|---|---|" + "|".join(["---"] * (len(tiers) + 1)) + "|"]
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
        lines.append(f"| {r['model']} | {r['params']} | {r.get('mode', '?')} | {r.get('scaffold', 'on')} | "
                     f"{mt if mt is not None else '—'} | " + " | ".join(cells) + " |")
    lines += ["",
              "`max_tok` = the completion-token cap for the run. `—` = not recorded (runs predating this "
              "field; the standard-config default is 2048). Runs at different caps are not strictly "
              "comparable — a reasoning model can exhaust a low cap mid-thought, so a higher cap is a "
              "fairer read of its capability but a looser comparison across sizes."]
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


def main(argv=None):
    p = argparse.ArgumentParser(prog="benchmark", description="Argus model-capability benchmark")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run the battery on one model")
    r.add_argument("--model", default="main")
    r.add_argument("--params", type=int, required=True, help="param count in billions (the x-axis)")
    r.add_argument("--mode", default=None, choices=["native", "manual", "native_finish"])
    r.add_argument("--k", type=int, default=3)
    r.add_argument("--judge", default="claude:opus")
    r.add_argument("--battery", default=str(BENCH / "battery.json"))
    r.add_argument("--timeout", type=float, default=180.0)
    r.add_argument("--baseline", action="store_true",
                   help="disable the toggleable scaffolding (observer/verifier/clarify/rules/…) to "
                        "measure Argus's lift over a plain agent loop")
    rep = sub.add_parser("report", help="regenerate report.md + curve.png from the results")
    rep.add_argument("--battery-version", default=None)
    args = p.parse_args(argv)

    if args.cmd == "run":
        result = asyncio.run(run_model(args.model, args.params, args.mode, args.k, args.judge,
                                       Path(args.battery), args.timeout, args.baseline))
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

    md, ok = render_report(bv)
    (BENCH / "report.md").write_text(md)
    curve_ok = render_curve(bv, BENCH / "curve.png")
    print(f"report: {BENCH / 'report.md'}" + (f"\ncurve: {BENCH / 'curve.png'}" if curve_ok else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
