#!/usr/bin/env python3
"""Render several chart styles from the sweep matrix so one can be chosen.

All read evidence/sweep-matrix.json. Output: /tmp/plots/*.png
"""
from __future__ import annotations

import json
import os
import sys

import matplotlib
import matplotlib.patches
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.colors import LinearSegmentedColormap  # noqa: E402
import numpy as np  # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else "evidence/sweep-matrix.json"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/plots"
os.makedirs(OUT, exist_ok=True)

rows = json.load(open(SRC))["rows"]
SIZES = sorted({r["chunk_kib"] for r in rows})
LEVELS = sorted({r["clients"] for r in rows})
GRID = {(r["chunk_kib"], r["clients"]): r for r in rows}

plt.rcParams.update({
    "font.size": 12, "axes.titlesize": 15, "axes.labelsize": 13,
    "axes.spines.top": False, "axes.spines.right": False,
    "figure.dpi": 160, "savefig.dpi": 160,
})
COLORS = {6: "#2563eb", 16: "#dc2626", 32: "#16a34a"}


def label(kib: int) -> str:
    return f"{kib}K" if kib < 1024 else "1M"


def get(kib, n, key):
    r = GRID[(kib, n)]
    return r[key]


def incomplete(kib, n) -> bool:
    return get(kib, n, "complete") < n


# ---------------------------------------------------------------- A: two panel
def variant_a():
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(9.5, 7.4), sharex=True,
                                   gridspec_kw={"height_ratios": [1.35, 1]})
    x = range(len(SIZES))
    for n in LEVELS:
        ys = [get(s, n, "median_ok_s") for s in SIZES]
        ax1.plot(x, ys, "o-", color=COLORS[n], lw=2.2, ms=7, label=f"{n} clients")
        for xi, s, y in zip(x, SIZES, ys):
            if incomplete(s, n):
                ax1.plot(xi, y, "o", mfc="white", mec=COLORS[n], mew=2.2, ms=11, zorder=5)
                ax1.annotate(f"{get(s,n,'complete')}/{n}", (xi, y),
                             textcoords="offset points", xytext=(0, 11),
                             ha="center", fontsize=9, color="#b45309", fontweight="bold")
    ax1.set_ylabel("median completion time (s)")
    ax1.set_title("Chunk size vs concurrency — 1 MiB object, DE→US, retry enabled", loc="left")
    ax1.grid(alpha=.25)
    ax1.legend(frameon=False)

    for n in LEVELS:
        pct = [100 * get(s, n, "complete") / n for s in SIZES]
        ax2.plot(x, pct, "s--", color=COLORS[n], lw=2, ms=6)
    ax2.set_ylim(0, 105)
    ax2.set_ylabel("clients completing (%)")
    ax2.set_xticks(list(x))
    ax2.set_xticklabels([label(s) for s in SIZES])
    ax2.set_xlabel("chunk size")
    ax2.grid(alpha=.25)
    ax2.set_title("A fast run that drops clients is not fast", loc="left", fontsize=12)
    fig.tight_layout()
    fig.savefig(f"{OUT}/a_two_panel.png", bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------------ B: heatmap
def variant_b():
    fig, ax = plt.subplots(figsize=(8.6, 6.4))
    times = np.array([[get(s, n, "median_ok_s") for n in LEVELS] for s in SIZES])
    cmap = LinearSegmentedColormap.from_list("g", ["#bbf7d0", "#fde68a", "#fca5a5"])
    im = ax.imshow(times, cmap=cmap, aspect="auto")

    for i, s in enumerate(SIZES):
        for j, n in enumerate(LEVELS):
            r = GRID[(s, n)]
            lost = r["complete"] < n
            txt = f"{r['median_ok_s']:.0f}s\n{r['complete']}/{n}"
            ax.text(j, i, txt, ha="center", va="center", fontsize=11,
                    fontweight="bold" if lost else "normal",
                    color="#7f1d1d" if lost else "#14532d")
            if lost:
                ax.add_patch(matplotlib.patches.Rectangle((j - .5, i - .5), 1, 1, fill=False,
                                           edgecolor="#7f1d1d", lw=2.5))
    ax.set_xticks(range(len(LEVELS)))
    ax.set_xticklabels([f"{n} clients" for n in LEVELS])
    ax.set_yticks(range(len(SIZES)))
    ax.set_yticklabels([label(s) for s in SIZES])
    ax.set_xlabel("client concurrency")
    ax.set_ylabel("chunk size")
    ax.set_title("Time and completion — red outline = did not complete", loc="left")
    fig.colorbar(im, ax=ax, label="median completion (s)")
    fig.tight_layout()
    fig.savefig(f"{OUT}/b_heatmap.png", bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------ C: single panel, one metric
def variant_c():
    fig, ax = plt.subplots(figsize=(9.2, 5.6))
    x = range(len(SIZES))
    for n in LEVELS:
        ys = [get(s, n, "median_ok_s") for s in SIZES]
        ax.plot(x, ys, "-", color=COLORS[n], lw=2.6, label=f"{n} clients")
        for xi, s, y in zip(x, SIZES, ys):
            ok = not incomplete(s, n)
            ax.plot(xi, y, "o" if ok else "X", color=COLORS[n],
                    ms=8 if ok else 12, mfc=COLORS[n] if ok else "white",
                    mew=2.2, zorder=5)
    ax.set_yscale("log")
    ax.set_ylabel("median completion time (s, log)")
    ax.set_xticks(list(x))
    ax.set_xticklabels([label(s) for s in SIZES])
    ax.set_xlabel("chunk size")
    ax.grid(alpha=.25, which="both")
    ax.legend(frameon=False)
    ax.set_title("Bigger amortises the round trip; smaller survives contention", loc="left")
    ax.annotate("X = run did not complete", xy=(0.985, 0.03), xycoords="axes fraction",
                ha="right", fontsize=10, color="#7f1d1d")
    fig.tight_layout()
    fig.savefig(f"{OUT}/c_single_log.png", bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------- D: grouped bars
def variant_d():
    fig, ax = plt.subplots(figsize=(9.6, 5.8))
    w = 0.26
    x = np.arange(len(SIZES))
    for k, n in enumerate(LEVELS):
        ys = [get(s, n, "median_ok_s") for s in SIZES]
        bars = ax.bar(x + (k - 1) * w, ys, w, color=COLORS[n], label=f"{n} clients")
        for b, s in zip(bars, SIZES):
            if incomplete(s, n):
                b.set_hatch("///")
                b.set_edgecolor("#7f1d1d")
                b.set_linewidth(1.6)
    ax.set_xticks(x)
    ax.set_xticklabels([label(s) for s in SIZES])
    ax.set_xlabel("chunk size")
    ax.set_ylabel("median completion time (s)")
    ax.set_title("Median completion by chunk size (hatched = incomplete run)", loc="left")
    ax.legend(frameon=False)
    ax.grid(axis="y", alpha=.25)
    fig.tight_layout()
    fig.savefig(f"{OUT}/d_bars.png", bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------- E: winner per concurrency
def variant_e():
    fig, ax = plt.subplots(figsize=(8.8, 5.0))
    winners = []
    for n in LEVELS:
        full = [(get(s, n, "median_ok_s"), s) for s in SIZES if not incomplete(s, n)]
        winners.append((n, *min(full)))
    labels = [f"{n} clients" for n, _, _ in winners]
    vals = [t for _, _, t in winners]
    sizes = [f"{label(s)}" for _, s, _ in winners]
    colors = [COLORS[n] for n, _, _ in winners]
    bars = ax.barh(labels, vals, color=colors, height=.55)
    for b, v, s in zip(bars, vals, sizes):
        ax.text(v + 1, b.get_y() + b.get_height() / 2, f"{v:.1f}s  ({s} chunks)",
                va="center", fontsize=12, fontweight="bold")
    ax.invert_yaxis()
    ax.set_xlabel("median completion time (s) — best chunk size per concurrency")
    ax.set_title("The optimum moves with concurrency", loc="left")
    ax.set_xlim(0, max(vals) * 1.35)
    ax.grid(axis="x", alpha=.25)
    fig.tight_layout()
    fig.savefig(f"{OUT}/e_winners.png", bbox_inches="tight")
    plt.close(fig)


for fn in (variant_a, variant_b, variant_c, variant_d, variant_e):
    fn()
    print("ok", fn.__name__)
print("wrote to", OUT)
