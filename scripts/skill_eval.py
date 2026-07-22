#!/usr/bin/env python3
"""Skill-eval harness — automated pass^k A/B for skills (#39).

Runs a JSON battery through real, ISOLATED Engines (in-process, temp data_dir — no repo pollution) in
two arms: treatment (skill present) and baseline (skill ablated via skill_registry.unregister). Captures
each run's tool-call chain + created-table args + the deterministically-selected skill, scores against
the case's `expect` predicate (engine.eval.scoring), aggregates pass^k, and writes a treatment-vs-baseline
report. Supports multiple models so the same battery can be compared across model sizes (the thesis
experiment). Deterministic scoring only — no model-graded judge (captures are saved for that later).

Usage:
  python scripts/skill_eval.py --battery docs/ab/batteries/structured-data.json
  python scripts/skill_eval.py --battery <b> --models 'main,small=http://localhost:8001/v1|Qwen2.5-3B-Instruct' --k 3
  python scripts/skill_eval.py --battery <b> --dry-run        # print the run matrix, no model calls
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FIXTURES = ROOT / "docs" / "ab" / "fixtures"
PASS_FRACTION = 0.6          # a case is "correct" for an arm if it passes in >= ceil(k*frac) of k runs
JUDGE_MARGIN = 0.3           # min treatment-baseline judge-mean gap (0-3 scale) to call KEEP/REGRESSION


def parse_models(spec: str) -> list[dict]:
    """'main,fast=http://host/v1|model' -> [{'name':'main'}, {'name':'fast','base_url':...,'model':...}]."""
    out = []
    for entry in [e.strip() for e in (spec or "main").split(",") if e.strip()]:
        # head is 'name' or 'name@mode'; optional 'base_url|model' after '='.
        # @mode overrides tool_calling_mode (native|manual) so the same endpoint can be run both ways
        # to separate a real capability gap from tool-call parse brittleness at small scale.
        head, _, rhs = entry.partition("=")
        name, _, mode = head.partition("@")
        name, mode = name.strip(), mode.strip().lower()
        if mode and mode not in ("native", "manual", "native_finish"):
            raise SystemExit(f"bad tool_calling_mode {mode!r} in {entry!r} "
                             "(expected native|manual|native_finish)")
        m = {"name": name}
        if mode:
            m["mode"] = mode
        if rhs:
            base_url, _, model = rhs.partition("|")
            m["base_url"] = base_url.strip()
            if model.strip():
                m["model"] = model.strip()
        out.append(m)
    return out


def build_config(model: dict):
    from config import Config
    cfg = Config()
    # The eval measures tool SELECTION, not approvals. With interactive approvals on and no human in the
    # loop, an Ask-gated tool (drop_column/rename_column/update_rows/delete_row/…) pauses the turn and
    # never completes, so the tool is issued (chain scored) but the judge sees "pending, not done" and
    # scores it low in BOTH arms. Turn approvals off so gated tools actually execute and the judge grades
    # the real result.
    updates = {"enable_interactive_approvals": False}
    if model.get("base_url"):
        updates["model_base_url"] = model["base_url"]
        updates["model_name"] = model.get("model") or cfg.model_name
    if model.get("mode"):
        updates["tool_calling_mode"] = model["mode"]
    return cfg.model_copy(update=updates)


async def run_one(cfg, battery: dict, case: dict, arm: str, timeout: float) -> dict:
    """One isolated Engine run. Returns the captured chain + score for this (case, arm)."""
    from engine.engine import Engine
    from engine.skills.base import get_selector
    from engine.eval.scoring import score_case

    tmp = tempfile.mkdtemp(prefix="skilleval-")
    try:
        engine = Engine(cfg, data_dir=tmp)
        if arm == "baseline":
            for s in battery.get("under_test", []):
                engine.skill_registry.unregister(s)
        # seed a source fixture into the isolated workspace
        if case.get("source"):
            src = FIXTURES / case["source"]
            dst = Path(engine._workspace_dir)
            dst.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst / case["source"])

        # seed pre-existing TABLES (identically in both arms) so a skill that operates on an EXISTING
        # table (evolve_table) has something real to mutate. setup.tables: [{name, columns, rows}].
        for tbl in (case.get("setup") or {}).get("tables", []):
            engine.tables.create_table(tbl["name"], tbl["columns"])
            for row in tbl.get("rows", []):
                engine.tables.insert(tbl["name"], row)

        session = f"eval-{arm}-{case['id']}"
        activated = get_selector("hybrid", engine.skill_registry).prepare("probe", case["prompt"], None).active_skill

        events: list = []

        async def collect():
            try:
                async for ev in engine.subscribe(session):
                    events.append(ev)
            except asyncio.CancelledError:
                pass

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        final, err = "", None
        try:
            final = await asyncio.wait_for(engine.run_task(session, case["prompt"], origin="api"), timeout=timeout)
        except Exception as e:                       # noqa: BLE001 - one bad run must not kill the sweep
            err = f"{type(e).__name__}: {e}"
        await asyncio.sleep(0.1)
        task.cancel()

        tools, ct_args = [], []
        for ev in events:
            data = getattr(ev, "data", {}) or {}
            if ev.kind == "tool_call" and data.get("tool"):
                tools.append(data["tool"])
                if data["tool"] == "create_table":
                    ct_args.append(data.get("args"))
        captured = {"tools": tools, "activated_skill": activated, "create_table_args": ct_args}
        result = score_case(case["expect"], captured)
        return {"chain_correct": bool(result["chain_correct"]), "reasons": result["reasons"],
                "tools": tools, "activated_skill": activated, "create_table_args": ct_args,
                "final": (final or "")[:2000], "error": err}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _client_judge_fn(client):
    """Judge backend using an OpenAI-compatible ModelClient (e.g. the local main model)."""
    async def fn(messages: list[dict]) -> str:
        resp = await client.chat(messages, max_tokens=200, think=False, temperature=0.0)
        return resp.content or ""
    return fn


def _claude_judge_fn(model: str):
    """Judge backend using the `claude -p` CLI (Opus/Fable via the user's subscription). Run from a
    NEUTRAL cwd (/tmp) so the grader has no repo/branch context — with repo context the agent treats
    the grading string as a possible prompt-injection probe and refuses instead of scoring."""
    async def fn(messages: list[dict]) -> str:
        system = "\n".join(m["content"] for m in messages if m["role"] == "system")
        user = "\n".join(m["content"] for m in messages if m["role"] == "user")
        prompt = (system + "\n\n" + user) if system else user
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", "--model", model, "--output-format", "json",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd="/tmp")
        try:
            out, _ = await asyncio.wait_for(proc.communicate(prompt.encode()), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            raise
        env = json.loads(out.decode() or "{}")
        return env.get("result", "")
    return fn


async def _judge_run(judge_fn, case: dict, r: dict) -> None:
    """Attach judge_score/judge_why to a run record (on-target rubric cases only). Never raises.
    Skips runs that errored/timed out — their capture is empty, and a low judge score on infra noise
    would silently contaminate judge_mean (and flip a KEEP/REGRESSION verdict)."""
    if judge_fn is None or not case.get("skill") or not case.get("rubric") or r.get("error"):
        return
    from engine.eval.judge import build_judge_prompt, parse_judge_reply
    try:
        text = await judge_fn(build_judge_prompt(case, r))
        v = parse_judge_reply(text)
        r["judge_score"], r["judge_why"] = v["score"], v["why"]
    except Exception as e:              # noqa: BLE001 - a judge failure is an unjudged cell, not a crash
        r["judge_score"], r["judge_why"], r["judge_error"] = None, "", f"{type(e).__name__}: {e}"


async def sweep(models: list[dict], battery: dict, k: int, timeout: float, judge_fn=None) -> dict:
    results: dict = {}
    for model in models:
        cfg = build_config(model)
        mres: dict = {}
        for case in battery["cases"]:
            arms = {}
            for arm in ("treatment", "baseline"):
                runs = []
                for i in range(k):
                    try:
                        r = await run_one(cfg, battery, case, arm, timeout)
                    except Exception as e:          # noqa: BLE001 - a bad run (missing fixture, build
                        # error) records an error cell; it must NEVER abort the whole sweep and discard
                        # everything already completed.
                        r = {"chain_correct": False, "reasons": [f"run failed: {e}"], "tools": [],
                             "activated_skill": None, "create_table_args": [], "final": "",
                             "error": f"{type(e).__name__}: {e}"}
                    await _judge_run(judge_fn, case, r)
                    runs.append(r)
                    js = r.get("judge_score")
                    print(f"  [{model['name']}] {case['id']:<18} {arm:<9} run {i+1}/{k}: "
                          f"{'OK ' if r['chain_correct'] else 'x  '}{r['tools']}"
                          + (f"  judge={js}" if js is not None else "")
                          + (f"  ERR {r['error']}" if r["error"] else ""), flush=True)
                passes = sum(1 for r in runs if r["chain_correct"])
                jscores = [r["judge_score"] for r in runs if r.get("judge_score") is not None]
                arms[arm] = {"passes": passes, "runs": runs,
                             "judge_mean": (sum(jscores) / len(jscores)) if jscores else None,
                             "judge_n": len(jscores)}
            mres[case["id"]] = arms
        results[model["name"]] = mres
    return results


def render_report(models, battery, k, results) -> str:
    thr = math.ceil(k * PASS_FRACTION)
    by_id = {c["id"]: c for c in battery["cases"]}
    lines = [f"# Skill-eval report — {battery.get('name', 'battery')}",
             "", f"pass^k: k={k}, a case is 'correct' for an arm if it passes >= {thr}/{k} runs. "
             f"under_test (ablated in baseline): {battery.get('under_test')}.", ""]
    for m in models:
        name = m["name"]
        hdr = f"## model: {name}" + (f"  [tool_calling_mode={m['mode']}]" if m.get("mode") else "")
        def _m(x):
            return f"{x:.1f}" if x is not None else "—"

        lines += [hdr, "",
                  "| case | on/off | chain base | chain treat | Δ | judge base→treat (0-3) |",
                  "|------|--------|-----------|-------------|---|------------------------|"]
        for cid, arms in results[name].items():
            case = by_id[cid]
            tgt = case["skill"] or "(off)"
            b, t = arms["baseline"]["passes"], arms["treatment"]["passes"]
            bc, tc = (b >= thr), (t >= thr)
            delta = ("=" if bc == tc else ("＋" if tc and not bc else "－"))
            bm, tm = arms["baseline"].get("judge_mean"), arms["treatment"].get("judge_mean")
            jcell = "—" if (bm is None and tm is None) else f"{_m(bm)} → {_m(tm)}"
            lines.append(f"| {cid} | {tgt} | {b}/{k} {'✓' if bc else '✗'} | {t}/{k} {'✓' if tc else '✗'} "
                         f"| {delta} | {jcell} |")
        # per-skill roll-up (on-target only): deterministic chain-correctness AND judge quality
        lines += ["", "**Per-skill roll-up (on-target):**"]
        for skill in battery.get("under_test", []):
            cases = [c for c in battery["cases"] if c["skill"] == skill]
            b_ok = sum(1 for c in cases if results[name][c["id"]]["baseline"]["passes"] >= thr)
            t_ok = sum(1 for c in cases if results[name][c["id"]]["treatment"]["passes"] >= thr)
            chain_v = "KEEP" if t_ok > b_ok else ("no-lift" if t_ok == b_ok else "REGRESSION")
            # judge delta = mean over this skill's rubric cases of (treat_mean - base_mean)
            jb = [results[name][c["id"]]["baseline"].get("judge_mean") for c in cases]
            jt = [results[name][c["id"]]["treatment"].get("judge_mean") for c in cases]
            pairs = [(x, y) for x, y in zip(jb, jt) if x is not None and y is not None]
            if pairs:
                jbase = sum(x for x, _ in pairs) / len(pairs)
                jtreat = sum(y for _, y in pairs) / len(pairs)
                d = jtreat - jbase
                jverdict = "KEEP" if d >= JUDGE_MARGIN else ("no-lift" if abs(d) < JUDGE_MARGIN else "REGRESSION")
                jline = f"; judge {jbase:.2f} → {jtreat:.2f} (Δ{d:+.2f}) **{jverdict}**"
            else:
                jline = "; judge (not run)"
            lines.append(f"- `{skill}`: chain baseline {b_ok}/{len(cases)} → treatment {t_ok}/{len(cases)} "
                         f"**{chain_v}**{jline}")
        # over-fire check (off-target must be clean in BOTH arms, all runs)
        overfire = []
        for c in battery["cases"]:
            if c["skill"]:
                continue
            for arm in ("treatment", "baseline"):
                if any(not r["chain_correct"] for r in results[name][c["id"]][arm]["runs"]):
                    overfire.append(f"{c['id']}({arm})")
        lines += ["", f"**Over-fire (off-target):** {'none ✓' if not overfire else 'FIRED: ' + ', '.join(overfire)}", ""]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Skill-eval harness (pass^k A/B across models)")
    ap.add_argument("--battery", required=True)
    ap.add_argument("--models", default="main")
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--out")
    ap.add_argument("--timeout", type=float, default=240.0)
    ap.add_argument("--judge", help="judge for on-target rubric cases (0-3). 'claude:opus' (or "
                                    "claude:fable) uses the claude CLI/subscription; otherwise a model "
                                    "spec like 'main' (same grammar as --models). Omit to skip judging.")
    ap.add_argument("--dry-run", action="store_true", help="print the run matrix, build nothing, call no model")
    args = ap.parse_args()

    battery = json.loads(Path(args.battery).read_text())
    models = parse_models(args.models)
    # judge label: 'claude:<model>' -> the CLI/Opus backend; anything else -> an OpenAI-client model spec
    judge_label = None
    if args.judge:
        judge_label = args.judge if args.judge.startswith("claude:") else parse_models(args.judge)[0]["name"]
    n_cases, n_arms = len(battery["cases"]), 2
    total = len(models) * n_cases * n_arms * args.k
    n_judge = sum(1 for c in battery["cases"] if c.get("skill") and c.get("rubric")) * n_arms * args.k * len(models)
    print(f"battery '{battery.get('name')}' — {len(models)} model(s) × {n_cases} cases × {n_arms} arms × k={args.k} "
          f"= {total} runs" + (f"; judge={judge_label} on {n_judge} on-target cells" if judge_label else ""))
    if args.dry_run:
        for m in models:
            print(f"  model {m['name']}: {m.get('base_url', '(configured default)')}"
                  + (f"  mode={m['mode']}" if m.get("mode") else "  mode=(config default)"))
        for c in battery["cases"]:
            print(f"  case {c['id']:<18} skill={c['skill']!s:<16} rubric={'yes' if c.get('rubric') else 'no'}")
        return 0

    judge_fn = None
    if args.judge and args.judge.startswith("claude:"):
        judge_fn = _claude_judge_fn(args.judge.split(":", 1)[1] or "opus")
    elif args.judge:
        from config import Config
        from engine.model_client import ModelClient
        js = parse_models(args.judge)[0]
        jc = Config()
        judge_fn = _client_judge_fn(ModelClient(js.get("base_url") or jc.model_base_url,
                                                js.get("model") or jc.model_name,
                                                jc.model_api_key, timeout=jc.request_timeout))
    results = asyncio.run(sweep(models, battery, args.k, args.timeout, judge_fn))
    report = render_report(models, battery, args.k, results)
    stem = Path(args.battery).stem
    out = Path(args.out) if args.out else (ROOT / "docs" / "ab" / "reports" / f"{stem}-report.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report)
    out.with_suffix(".json").write_text(json.dumps(results, indent=2, default=str))
    print("\n" + report)
    print(f"\nreport: {out}\ncaptures: {out.with_suffix('.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
