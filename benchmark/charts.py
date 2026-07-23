"""Repo-native chart generators for the cap-1 model-capability benchmark: `stackup` (grouped-bar
chain-pass by model x (mode x scaffold)) and `model_tiers` (per-model per-tier grouped bars).

Both read committed results via engine.eval.benchmark._load_results() so the `solved` back-fill
(Task 2) applies uniformly, and both accept `metric` so any per_tier/overall key (chain_pass,
judge_mean, solved) can be plotted. Ported from ad-hoc scratch scripts; kept Agg-backed matplotlib.
"""
from __future__ import annotations

import math

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

from engine.eval.benchmark import _load_results

BV = "cap-1"
NATIVE, MANUAL = "#2a78d6", "#eb6834"
CONFIGS = [  # fixed render order within each model group: baseline first, then scaffolded
    ("native", "off"), ("manual", "off"),
    ("native", "on"), ("manual", "on"),
]
TIERS = [1, 2, 3, 4]


def _sc(r):
    return r.get("scaffold", "on")


def _best_runs():
    """representative run per (model, params, mode, scaffold): highest max_tokens, then latest date"""
    res = [r for r in _load_results() if r.get("battery_version") == BV]
    best = {}
    for r in res:
        key = (r["model"], r["params"], r.get("mode"), _sc(r))
        rank = ((r.get("max_tokens") or 0), r.get("date", ""))
        if key not in best or rank > best[key][0]:
            best[key] = (rank, r)
    return best


def stackup(out, metric: str = "chain_pass") -> bool:
    """Grouped-bar stack-up PNG: `metric` by model x (mode x scaffold). Style matches curve.png."""
    best = _best_runs()
    order = sorted({(p, m) for (m, p, _, _) in best})  # (params, model) sorted by params

    def val(model, mode, scaf):
        for (m, p, md, s), (_, r) in best.items():
            if m == model and md == mode and s == scaf:
                v = r.get("overall", {}).get(metric)
                return None if v is None else round(v * 100)
        return None

    fig, ax = plt.subplots(figsize=(11, 5.6))
    n_slots = len(CONFIGS)
    group_w = 0.82
    bw = group_w / n_slots

    for gi, (params, model) in enumerate(order):
        for ci, (mode, scaf) in enumerate(CONFIGS):
            v = val(model, mode, scaf)
            if v is None:
                continue
            x = gi + (ci - (n_slots - 1) / 2) * bw
            color = NATIVE if mode == "native" else MANUAL
            if scaf == "on":
                ax.bar(x, v, bw * 0.92, color=color, edgecolor="none", zorder=3)
            else:
                ax.bar(x, v, bw * 0.92, facecolor="none", edgecolor=color,
                       hatch="////", linewidth=1.1, zorder=3)
            ax.text(x, v + 1.4, str(v), ha="center", va="bottom", fontsize=8.5,
                    fontfamily="monospace", color="#333", zorder=4)

    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([f"{model}\n{params}B" for params, model in order], fontsize=10)
    ax.set_ylim(0, 105)
    ax.set_ylabel(f"{metric} (%)")
    ax.set_title(f"Argus cap-1 — model stack-up  ·  {metric} by mode & scaffolding", fontsize=12, pad=12)
    ax.yaxis.grid(True, color="#e7e6e1", linewidth=1, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)

    legend = [
        Patch(facecolor=NATIVE, label="native mode"),
        Patch(facecolor=MANUAL, label="manual mode"),
        Patch(facecolor="#888", label="solid = scaffolded"),
        Patch(facecolor="none", edgecolor="#888", hatch="////", label="hatched = baseline (no scaffolding)"),
    ]
    ax.legend(handles=legend, loc="upper left", frameon=False, fontsize=9, ncol=2,
              handlelength=1.4, columnspacing=1.6)
    fig.text(0.5, 0.005, "cap-1 (27 tasks, 4 tiers), k=3, Opus judge · a missing bar means the config wasn't run, not zero",
              ha="center", fontsize=8, color="#8a887f")
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(out, dpi=150)
    plt.close(fig)
    print("wrote", out, "·", len(order), "models")
    return True


def model_tiers(out, metric: str = "chain_pass") -> bool:
    """Per-model grouped-bar charts: one panel per model, x = tier, grouped bars = the four runs
    (baseline/scaffold x native/manual)."""
    best = _best_runs()
    order = sorted({(p, m) for (m, p, _, _) in best})  # (params, model) by size

    def tier_val(model, mode, scaf, tier):
        for (m, p, md, s), (_, r) in best.items():
            if m == model and md == mode and s == scaf:
                v = r.get("per_tier", {}).get(str(tier), {}).get(metric)
                return None if v is None else round(v * 100)
        return None

    n = len(order)
    cols = min(3, n) or 1
    rows = math.ceil(n / cols) if n else 1
    fig, axes = plt.subplots(rows, cols, figsize=(4.7 * cols, 3.3 * rows), sharey=True, squeeze=False)
    flat = [ax for row in axes for ax in row]

    group_w = 0.8
    bw = group_w / len(CONFIGS)
    drawn = 0
    for ax, (params, model) in zip(flat, order):
        for ci, (mode, scaf) in enumerate(CONFIGS):
            color = NATIVE if mode == "native" else MANUAL
            for ti, t in enumerate(TIERS):
                v = tier_val(model, mode, scaf, t)
                if v is None:
                    continue
                x = ti + (ci - (len(CONFIGS) - 1) / 2) * bw
                if scaf == "on":
                    ax.bar(x, v, bw * 0.9, color=color, edgecolor="none", zorder=3)
                else:
                    ax.bar(x, v, bw * 0.9, facecolor="none", edgecolor=color, hatch="////",
                           linewidth=1.0, zorder=3)
                ax.text(x, v + 1.5, str(v), ha="center", va="bottom", fontsize=6.2,
                        fontfamily="monospace", color="#555", zorder=4)
                drawn += 1
        ax.set_title(f"{model}  ·  {params}B", fontsize=10)
        ax.set_xticks(range(len(TIERS)))
        ax.set_xticklabels([f"T{t}" for t in TIERS], fontsize=9)
        ax.set_ylim(0, 108)
        ax.grid(True, axis="y", color="#e7e6e1", linewidth=0.9, zorder=0)
        ax.set_axisbelow(True)
        for sp_ in ("top", "right"):
            ax.spines[sp_].set_visible(False)
        ax.tick_params(length=0)

    for ax in flat[n:]:
        ax.set_visible(False)
    for r_ in range(rows):
        axes[r_][0].set_ylabel(f"{metric} (%)")

    fig.suptitle(f"Argus cap-1 — per-tier {metric} by run, one panel per model", fontsize=13, y=0.99)
    legend = [
        Patch(facecolor=NATIVE, label="native mode"),
        Patch(facecolor=MANUAL, label="manual mode"),
        Patch(facecolor="#888", label="solid = scaffolded"),
        Patch(facecolor="none", edgecolor="#888", hatch="////", label="hatched = baseline"),
    ]
    fig.legend(handles=legend, loc="lower center", ncol=4, frameon=False, fontsize=9.5,
               bbox_to_anchor=(0.5, -0.01))
    fig.text(0.5, 0.035, "bars within each tier: baseline-native, baseline-manual, scaffold-native, scaffold-manual · "
              "a missing bar = that config wasn't run", ha="center", fontsize=8, color="#8a887f")
    fig.tight_layout(rect=(0, 0.06, 1, 0.96))
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print("wrote", out, "·", n, "panels ·", drawn, "bars")
    return True
