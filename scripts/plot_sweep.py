#!/usr/bin/env python3
"""Render the chunk-size sweep as a dependency-free SVG.

Two panels:
  A. median completion time vs chunk size, one line per client concurrency
  B. completion rate, because a fast run that loses clients is not fast

Hand-rolled SVG so the repo needs no plotting stack and the artifact diffs
cleanly in git.
"""
from __future__ import annotations

import json
import sys

W, H = 960, 620
PAD_L, PAD_R = 78, 28
PANEL_A = (40, 300)
PANEL_B = (360, 600)

SERIES = {6: "#1f77b4", 16: "#d62728", 32: "#2ca02c"}


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def main(path: str, out: str) -> int:
    rows = json.load(open(path))["rows"]
    sizes = sorted({r["chunk_kib"] for r in rows})
    clients = sorted({r["clients"] for r in rows})
    by = {(r["chunk_kib"], r["clients"]): r for r in rows}

    # log2 x axis so 16 KiB .. 1 MiB is evenly spaced
    xs = {s: PAD_L + (W - PAD_L - PAD_R) * (sizes.index(s) / max(1, len(sizes) - 1))
          for s in sizes}

    times = [r["median_ok_s"] for r in rows if r["median_ok_s"]]
    tmax = max(times) if times else 1.0
    tmax = max(1.0, (int(tmax / 10) + 1) * 10)

    a_top, a_bot = PANEL_A
    b_top, b_bot = PANEL_B

    def ya(v):
        return a_bot - (a_bot - a_top) * (v / tmax)

    def yb(v):
        return b_bot - (b_bot - b_top) * (v / 100.0)

    p = []
    p.append(f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
             f'viewBox="0 0 {W} {H}" font-family="ui-sans-serif,-apple-system,'
             f'Segoe UI,Helvetica,Arial,sans-serif">')
    p.append(f'<rect width="{W}" height="{H}" fill="#ffffff"/>')
    p.append('<text x="24" y="26" font-size="17" font-weight="600" fill="#111">'
             'Fleet chunk-size sweep — DE origin → US replica, 1 MiB object</text>')

    # ---- Panel A: time ----
    p.append(f'<text x="24" y="{a_top - 12}" font-size="13" font-weight="600" '
             f'fill="#333">A. median completion time (lower is better)</text>')
    for i in range(5):
        v = tmax * i / 4
        y = ya(v)
        p.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" '
                 f'stroke="#e8e8e8"/>')
        p.append(f'<text x="{PAD_L-10}" y="{y+4:.1f}" font-size="11" fill="#666" '
                 f'text-anchor="end">{v:.0f}s</text>')

    for n in clients:
        pts = []
        for s in sizes:
            r = by.get((s, n))
            if r and r["median_ok_s"]:
                pts.append((xs[s], ya(r["median_ok_s"]), r))
        if len(pts) > 1:
            d = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in pts)
            p.append(f'<polyline points="{d}" fill="none" '
                     f'stroke="{SERIES.get(n, "#555")}" stroke-width="2.2"/>')
        for x, y, r in pts:
            lost = r["complete"] < r["clients"]
            fill = "#ffffff" if lost else SERIES.get(n, "#555")
            p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" '
                     f'fill="{fill}" stroke="{SERIES.get(n, "#555")}" stroke-width="2"/>')
            if lost:
                p.append(f'<text x="{x:.1f}" y="{y-12:.1f}" font-size="10" '
                         f'fill="#b45309" text-anchor="middle">'
                         f'{r["complete"]}/{r["clients"]}</text>')

    for s in sizes:
        p.append(f'<text x="{xs[s]:.1f}" y="{a_bot+18}" font-size="11" fill="#666" '
                 f'text-anchor="middle">{s}</text>')
    p.append(f'<text x="{(PAD_L+W-PAD_R)/2:.0f}" y="{a_bot+38}" font-size="12" '
             f'fill="#333" text-anchor="middle">chunk size (KiB, log scale)</text>')

    # legend
    lx = W - PAD_R - 150
    for i, n in enumerate(clients):
        y = a_top + 6 + i * 18
        p.append(f'<line x1="{lx}" y1="{y}" x2="{lx+22}" y2="{y}" '
                 f'stroke="{SERIES.get(n,"#555")}" stroke-width="2.2"/>')
        p.append(f'<text x="{lx+30}" y="{y+4}" font-size="11" fill="#333">'
                 f'{n} clients</text>')
    p.append(f'<text x="{lx}" y="{a_top+6+len(clients)*18+4}" font-size="10" '
             f'fill="#b45309">open circle = incomplete</text>')

    # ---- Panel B: completion rate ----
    p.append(f'<text x="24" y="{b_top-12}" font-size="13" font-weight="600" '
             f'fill="#333">B. completion rate (a fast run that drops clients is not fast)</text>')
    for v in (0, 25, 50, 75, 100):
        y = yb(v)
        p.append(f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{W-PAD_R}" y2="{y:.1f}" '
                 f'stroke="#e8e8e8"/>')
        p.append(f'<text x="{PAD_L-10}" y="{y+4:.1f}" font-size="11" fill="#666" '
                 f'text-anchor="end">{v}%</text>')

    for n in clients:
        pts = []
        for s in sizes:
            r = by.get((s, n))
            if r:
                pct = 100.0 * r["complete"] / r["clients"]
                pts.append((xs[s], yb(pct)))
        if len(pts) > 1:
            d = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
            p.append(f'<polyline points="{d}" fill="none" '
                     f'stroke="{SERIES.get(n, "#555")}" stroke-width="2.2" '
                     f'stroke-dasharray="5 3"/>')

    for s in sizes:
        p.append(f'<text x="{xs[s]:.1f}" y="{b_bot+18}" font-size="11" fill="#666" '
                 f'text-anchor="middle">{s}</text>')
    p.append(f'<text x="{(PAD_L+W-PAD_R)/2:.0f}" y="{b_bot+38}" font-size="12" '
             f'fill="#333" text-anchor="middle">chunk size (KiB, log scale)</text>')

    p.append('</svg>')
    open(out, "w").write("\n".join(p))
    print(f"wrote {out} ({len(sizes)} sizes x {len(clients)} levels)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/sweep_matrix.json",
                          sys.argv[2] if len(sys.argv) > 2 else "docs/sweep.svg"))
