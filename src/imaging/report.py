"""Timeline and comparison reports (CSV, Markdown, SVG) for the load test and the simulation.

The SVG is a column of small multiples sharing one time axis - one measure per panel, one series
per panel (no dual axes), scaling events drawn as thin vertical ticks. Every point carries a
<title> tooltip, and the CSV next to it is the table view.
"""

from __future__ import annotations

import csv
import html
from pathlib import Path

SURFACE, INK, INK2, GRID, SERIES, EVENT = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df", "#2a78d6", "#eb6834"


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    keys = list(rows[0])
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def timeline_svg(samples: list[dict], panels: list[tuple[str, str]], events: list[dict] | None = None,
                 title: str = "Scaling timeline") -> str:
    """panels: [(sample key, panel label)]; events: [{"t": seconds, "layer": ..., "label": ...}]"""
    width, left, right, ph, gap, top = 860, 64, 24, 110, 34, 44
    height = top + len(panels) * (ph + gap) + 24
    tmax = max((s["t"] for s in samples), default=1) or 1
    x = lambda t: left + (width - left - right) * (t / tmax)  # noqa: E731
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
           f'font-family="system-ui, sans-serif" font-size="12" role="img" aria-label="{html.escape(title)}">',
           f'<rect width="100%" height="100%" fill="{SURFACE}"/>',
           f'<text x="{left}" y="22" font-size="15" font-weight="600" fill="{INK}">{html.escape(title)}</text>']
    for i, (key, label) in enumerate(panels):
        y0 = top + i * (ph + gap)
        vals = [float(s.get(key) or 0) for s in samples]
        vmax = max(vals + [1.0])
        y = lambda v, y0=y0, vmax=vmax: y0 + ph - ph * (v / vmax)  # noqa: E731
        out.append(f'<text x="{left}" y="{y0 - 8}" fill="{INK2}">{html.escape(label)} (max {vmax:g})</text>')
        for frac in (0, 0.5, 1):
            gy = y0 + ph - ph * frac
            out.append(f'<line x1="{left}" x2="{width - right}" y1="{gy:.1f}" y2="{gy:.1f}" stroke="{GRID}" stroke-width="1"/>')
            out.append(f'<text x="{left - 6}" y="{gy + 4:.1f}" text-anchor="end" fill="{INK2}">{vmax * frac:g}</text>')
        for ev in events or []:
            ex = x(ev["t"])
            out.append(f'<line x1="{ex:.1f}" x2="{ex:.1f}" y1="{y0}" y2="{y0 + ph}" stroke="{EVENT}" stroke-width="1" '
                       f'stroke-dasharray="2 3"><title>{html.escape(ev.get("label", ev.get("layer", "")))} '
                       f'at {ev["t"]:.0f}s</title></line>')
        if samples:
            pts = " ".join(f"{x(s['t']):.1f},{y(v):.1f}" for s, v in zip(samples, vals, strict=True))
            out.append(f'<polyline points="{pts}" fill="none" stroke="{SERIES}" stroke-width="2" '
                       f'stroke-linejoin="round" stroke-linecap="round"/>')
            for s, v in zip(samples, vals, strict=True):
                out.append(f'<circle cx="{x(s["t"]):.1f}" cy="{y(v):.1f}" r="6" fill="transparent">'
                           f'<title>{html.escape(label)}: {v:g} at {s["t"]:.0f}s</title></circle>')
    yb = top + len(panels) * (ph + gap) - gap + 18
    for frac in (0, 0.25, 0.5, 0.75, 1):
        out.append(f'<text x="{x(tmax * frac):.1f}" y="{yb}" text-anchor="middle" fill="{INK2}">{tmax * frac:.0f}s</text>')
    out.append(f'<text x="{width - right}" y="{yb + 16}" text-anchor="end" fill="{INK2}">dashed ticks: scaling '
               'activities (hover for details)</text></svg>')
    return "\n".join(out)


def relative(a: float | None, b: float | None) -> str:
    """'elastic relative to fixed' as a ratio, without absolute units."""
    if a is None or b is None or b == 0:
        return "n/a"
    return f"{a / b:.2f}x"


def comparison_markdown(fixed: dict, elastic: dict, keys: list[tuple[str, str]]) -> str:
    lines = ["| Measure | Elastic relative to fixed | Reading |", "|---|---|---|"]
    for k, reading in keys:
        lines.append(f"| {k} | {relative(elastic.get(k), fixed.get(k))} | {reading} |")
    return "\n".join(lines)
