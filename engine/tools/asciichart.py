"""asciichart — dependency-free text charts (Unicode block art, ASCII fallback).

Pure library: every chart function takes data + options and returns a multi-line string.
No matplotlib, no I/O, no third-party deps — the charts render inline in Telegram, the
dashboard, and any terminal, so the agent can *show* a number series without attaching an image.

Chart types:
    hbar(data)        horizontal bar chart      (labels left, bars right, sub-cell precision)
    vbar(data)        vertical bar chart        (columns of blocks, short labels below)
    composition(data) one proportion bar        ("pie as a single bar" + % legend)
    sparkline(values) inline single-line trend  (▁▂▃▄▅▆▇█)
    line(values)      y-axis line plot          (classic box-drawing line chart)

Data shapes accepted by the labelled charts (hbar/vbar/composition):
    - list of {"label": str, "value": num}     (also accepts name/x and value/y/count keys)
    - list of (label, value) pairs
    - dict {label: value}
sparkline/line accept the above OR a bare list of numbers.

Set ascii_only=True to emit pure 7-bit ASCII (for fonts/terminals without block glyphs).
The AsciiChartTool at the bottom is the thin Argus wrapper around these functions.
"""
from __future__ import annotations

import math

from pydantic import BaseModel, Field

from engine.tools.base import Tool

# ── glyph ramps ────────────────────────────────────────────────────────────
_EIGHTHS = "▏▎▍▌▋▊▉█"        # 1/8 .. 8/8, left-anchored (horizontal bar tips)
_LOWER = "▁▂▃▄▅▆▇█"          # 1/8 .. 8/8, bottom-anchored (vbar tops, sparkline)
_ASCII_RAMP = "._-=+*#@"      # 8-level ascii density ramp (sparkline fallback)
_COMP_UNICODE = "█▓▒░"        # composition fills (distinct for up to 4 adjacent segments)
_COMP_ASCII = "#=*+o.x@"
_LINE_ASCII = {"┤": "+", "┼": "+", "─": "-", "│": "|",
               "╰": "\\", "╭": "/", "╮": "\\", "╯": "/"}


def _fmt(v: float) -> str:
    """Compact number format: ints without a decimal, floats trimmed to <=2 places."""
    if v == int(v):
        return str(int(v))
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return s


def _axis_decimals(interval: float) -> int:
    """Decimal places for line-chart y-axis tick labels, so they read cleanly and uniformly.
    Wide ranges → integer ticks; narrow ranges → more precision (0..3 places)."""
    if interval <= 0:
        return 0
    return min(max(1 - math.floor(math.log10(interval)), 0), 3)


def _normalize(data) -> list[tuple[str, float]]:
    """Coerce the accepted data shapes into [(label, value)]."""
    items: list[tuple] = []
    if isinstance(data, dict):
        items = list(data.items())
    else:
        for d in data or []:
            if isinstance(d, dict):
                label = d.get("label", d.get("name", d.get("x", "")))
                value = d.get("value", d.get("y", d.get("count", 0)))
                items.append((label, value))
            elif isinstance(d, (list, tuple)) and len(d) >= 2:
                items.append((d[0], d[1]))
            else:                                   # bare number
                items.append(("", d))
    out = []
    for label, value in items:
        try:
            out.append((str(label), float(value)))
        except (TypeError, ValueError):
            continue                                # skip non-numeric rows rather than crash
    return out


def _values(data) -> list[float]:
    """Extract a bare numeric series (for sparkline/line)."""
    if isinstance(data, dict):
        return [v for _, v in _normalize(data)]
    out = []
    for d in data or []:
        if isinstance(d, dict):
            out.extend(v for _, v in _normalize([d]))
        elif isinstance(d, (list, tuple)) and len(d) >= 2:
            try:
                out.append(float(d[1]))
            except (TypeError, ValueError):
                pass
        else:
            try:
                out.append(float(d))
            except (TypeError, ValueError):
                pass
    return out


def _center(s: str, width: int) -> str:
    if len(s) >= width:
        return s[:width]
    pad = width - len(s)
    left = pad // 2
    return " " * left + s + " " * (pad - left)


# ── horizontal bar ─────────────────────────────────────────────────────────
def _hblocks(cells: float, ascii_only: bool) -> str:
    if ascii_only:
        return "#" * int(round(cells))
    full = int(cells)
    s = "█" * full
    idx = int(round((cells - full) * 8))
    if idx > 0:
        s += _EIGHTHS[idx - 1]
    return s


def hbar(data, width: int = 40, ascii_only: bool = False, show_values: bool = True,
         vmin=None, vmax=None) -> str:
    items = _normalize(data)
    if not items:
        return "(no data)"
    lw = max(len(l) for l, _ in items)
    lo = 0.0 if vmin is None else float(vmin)        # axis floor (default: zero-baseline)
    hi = (max((v for _, v in items), default=0.0)) if vmax is None else float(vmax)
    span = (hi - lo) or 1.0                           # values scale across [lo, hi] → [0, width]
    sep = "|" if ascii_only else "│"
    vw = max((len(_fmt(v)) for _, v in items), default=0)
    lines = []
    for l, v in items:
        cells = max(0.0, min((v - lo) / span, 1.0)) * width if v > lo else 0.0
        bar = _hblocks(cells, ascii_only)
        row = f"{l.ljust(lw)} {sep} "
        if show_values:                              # pad bar to full width so values align in a column
            row += f"{bar.ljust(width)} {_fmt(v).rjust(vw)}"
        else:
            row += bar
        lines.append(row.rstrip())
    return "\n".join(lines)


# ── vertical bar ───────────────────────────────────────────────────────────
def vbar(data, height: int = 8, ascii_only: bool = False, col_width: int = 0,
         show_values: bool = True, vmin=None, vmax=None) -> str:
    items = _normalize(data)
    if not items:
        return "(no data)"
    lo = 0.0 if vmin is None else float(vmin)        # axis floor (default: zero-baseline)
    hi = (max((v for _, v in items), default=0.0)) if vmax is None else float(vmax)
    span = (hi - lo) or 1.0
    valstrs = [_fmt(v) for _, v in items]
    widest = [len(l) for l, _ in items] + ([len(s) for s in valstrs] if show_values else [])
    bw = col_width or max(2, min(8, max(widest)))   # wide enough for labels AND value tags
    gutter = 1
    fill = "#" if ascii_only else "█"
    rows: list[str] = []
    for r in range(height, 0, -1):                  # top → bottom, r = cell threshold
        row = ""
        for _, v in items:
            cells = max(0.0, min((v - lo) / span, 1.0)) * height
            full = int(cells)
            frac = cells - full
            if r <= full:
                ch = fill
            elif r == full + 1 and frac > 0 and not ascii_only:
                ch = _LOWER[max(0, int(round(frac * 8)) - 1)]
            elif r == full + 1 and frac >= 0.5 and ascii_only:
                ch = fill
            else:
                ch = " "
            row += ch * bw + " " * gutter
        rows.append(row.rstrip())
    out = []
    if show_values:                                 # value tags on top (scale ref + hbar parity)
        out.append("".join(_center(s, bw) + " " * gutter for s in valstrs).rstrip())
    out += rows
    out.append("".join(_center(l[:bw], bw) + " " * gutter for l, _ in items).rstrip())
    return "\n".join(out)


# ── composition (single proportion bar, "pie as a bar") ─────────────────────
def composition(data, width: int = 40, ascii_only: bool = False) -> str:
    items = _normalize(data)
    total = sum(max(v, 0.0) for _, v in items)
    if total <= 0:
        return "(no data)"
    palette = _COMP_ASCII if ascii_only else _COMP_UNICODE
    raw = [max(v, 0.0) / total * width for _, v in items]
    counts = [int(x) for x in raw]
    remainder = width - sum(counts)
    # largest-remainder apportionment so the segments sum exactly to `width`
    order = sorted(range(len(items)), key=lambda i: raw[i] - counts[i], reverse=True)
    for i in order[:remainder]:
        counts[i] += 1
    # Percentages via the same largest-remainder apportionment so the legend sums to exactly 100%.
    praw = [max(v, 0.0) / total * 100 for _, v in items]
    pcts = [int(x) for x in praw]
    prem = 100 - sum(pcts)
    porder = sorted(range(len(items)), key=lambda i: praw[i] - pcts[i], reverse=True)
    for i in porder[:max(prem, 0)]:
        pcts[i] += 1
    dash = "-" if ascii_only else "—"
    bar = ""
    legend = []
    for i, (l, v) in enumerate(items):
        ch = palette[i % len(palette)]
        bar += ch * counts[i]
        legend.append(f"  {ch} {l} {dash} {_fmt(v)} ({pcts[i]}%)")
    left, right = ("[", "]") if ascii_only else ("│", "│")
    return f"{left}{bar}{right}\n" + "\n".join(legend)


# ── sparkline ──────────────────────────────────────────────────────────────
def sparkline(data, ascii_only: bool = False, show_range: bool = False) -> str:
    vals = _values(data)
    if not vals:
        return "(no data)"
    ramp = _ASCII_RAMP if ascii_only else _LOWER
    mn, mx = min(vals), max(vals)
    span = mx - mn
    if span == 0:
        spark = ramp[len(ramp) // 2] * len(vals)
    else:
        spark = "".join(ramp[int(round((v - mn) / span * (len(ramp) - 1)))] for v in vals)
    if show_range:                                   # scale reference: the low–high the ramp spans
        dash = "-" if ascii_only else "–"
        return f"{spark}  ({_fmt(mn)}{dash}{_fmt(mx)})"
    return spark


# ── line plot (classic asciichart box-drawing) ─────────────────────────────
def _resample(series: list[float], width: int) -> list[float]:
    n = len(series)
    if n <= width or width < 2:
        return series
    return [series[int(round(i * (n - 1) / (width - 1)))] for i in range(width)]


def line(data, height: int = 10, width: int = 0, ascii_only: bool = False,
         vmin=None, vmax=None) -> str:
    series = _values(data)
    if not series:
        return "(no data)"
    if len(series) == 1:
        series = series * 2                         # need >=2 points to draw a segment
    if width and len(series) > width:
        series = _resample(series, width)
    mn = min(series) if vmin is None else float(vmin)   # explicit y-range zooms/pins the axis
    mx = max(series) if vmax is None else float(vmax)
    if mx < mn:
        mn, mx = mx, mn
    interval = mx - mn
    ratio = (height - 1) / interval if interval else 1.0
    min2, max2 = round(mn * ratio), round(mx * ratio)
    rows = max(max2 - min2, 1)
    dec = _axis_decimals(interval)                   # consistent, clean tick labels (not 83.45, 76.91…)
    labels = [f"{mx - i * (interval / rows):.{dec}f}" for i in range(rows + 1)]
    lw = max(len(s) for s in labels)
    offset = lw + 1
    n = len(series)
    w = n + offset
    grid = [[" "] * w for _ in range(rows + 1)]
    for i in range(rows + 1):                       # y-axis labels + ticks
        for j, ch in enumerate(labels[i].rjust(lw)):
            grid[i][j] = ch
        grid[i][offset - 1] = "┤"

    def y_of(val: float) -> int:
        return max(0, min(round(val * ratio) - min2, rows))   # clamp out-of-range points to edge

    grid[rows - y_of(series[0])][offset - 1] = "┼"  # start marker on the axis
    for x in range(n - 1):
        y0, y1 = y_of(series[x]), y_of(series[x + 1])
        if y0 == y1:
            grid[rows - y0][x + offset] = "─"
        else:
            if y0 > y1:
                grid[rows - y1][x + offset] = "╰"
                grid[rows - y0][x + offset] = "╮"
            else:
                grid[rows - y1][x + offset] = "╭"
                grid[rows - y0][x + offset] = "╯"
            for y in range(min(y0, y1) + 1, max(y0, y1)):
                grid[rows - y][x + offset] = "│"
    out = "\n".join("".join(r).rstrip() for r in grid)
    if ascii_only:
        out = out.translate(str.maketrans(_LINE_ASCII))
    return out


# ── scatter plot ───────────────────────────────────────────────────────────
def _points(data) -> list[tuple[float, float]]:
    """Extract (x, y) pairs. Accepts {"x":,"y":} dicts (y also from 'value'), [x,y] pairs,
    or a bare list of numbers (x becomes the index)."""
    pts: list[tuple[float, float]] = []
    for i, d in enumerate(data or []):
        try:
            if isinstance(d, dict):
                y = d.get("y", d.get("value"))
                x = d.get("x", i)
                pts.append((float(x), float(y)))
            elif isinstance(d, (list, tuple)) and len(d) >= 2:
                pts.append((float(d[0]), float(d[1])))
            else:
                pts.append((float(i), float(d)))
        except (TypeError, ValueError):
            continue
    return pts


def scatter(data, width: int = 40, height: int = 15, ascii_only: bool = True, marker: str = "*") -> str:
    """A scatter plot on an ASCII grid. Overlapping points render as a count (2–9, then '#')."""
    pts = _points(data)
    if not pts:
        return "(no data)"
    xs = [x for x, _ in pts]
    ys = [y for _, y in pts]
    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    xspan = (xmax - xmin) or 1.0
    yspan = (ymax - ymin) or 1.0
    counts = [[0] * width for _ in range(height)]
    for x, y in pts:
        col = int(round((x - xmin) / xspan * (width - 1)))
        row = int(round((ymax - y) / yspan * (height - 1)))
        counts[row][col] += 1
    ydec = _axis_decimals(ymax - ymin)              # clean, uniform tick labels (not 83.18, 78.36…)
    ylabels = [f"{ymax - i * (ymax - ymin) / (height - 1):.{ydec}f}" for i in range(height)]
    lw = max(len(s) for s in ylabels)
    axis = "|" if ascii_only else "│"
    out = []
    for r in range(height):
        cells = ""
        for c in range(width):
            n = counts[r][c]
            cells += " " if n == 0 else (marker if n == 1 else (str(n) if n < 10 else "#"))
        out.append(f"{ylabels[r].rjust(lw)} {axis}{cells.rstrip()}")
    base = "-" if ascii_only else "─"
    out.append(f"{' ' * lw} +{base * width}".rstrip())      # x-axis
    xlo, xhi = _fmt(xmin), _fmt(xmax)                        # x range: min left, max right
    xline = xlo + xhi.rjust(width - len(xlo)) if width > len(xlo) + len(xhi) else f"{xlo}..{xhi}"
    out.append(f"{' ' * lw}  {xline}")
    return "\n".join(out)


# ── dispatch + Argus tool ──────────────────────────────────────────────────
_ALIASES = {
    "hbar": "hbar", "horizontal_bar": "hbar", "horizontal": "hbar", "bar": "hbar", "barh": "hbar",
    "vbar": "vbar", "vertical_bar": "vbar", "vertical": "vbar", "column": "vbar", "columns": "vbar",
    "composition": "composition", "stacked": "composition", "proportion": "composition",
    "pie": "composition", "pie_bar": "composition", "breakdown": "composition",
    "sparkline": "sparkline", "spark": "sparkline", "trend": "sparkline",
    "line": "line", "lineplot": "line", "plot": "line",
    "scatter": "scatter", "scatterplot": "scatter", "points": "scatter", "xy": "scatter",
}


def render(chart_type: str, data, *, width: int = 40, height: int = 10,
           ascii_only: bool = False, vmin=None, vmax=None) -> str:
    kind = _ALIASES.get((chart_type or "hbar").strip().lower())
    if kind is None:
        raise ValueError(
            f"unknown chart_type {chart_type!r}. Use one of: hbar, vbar, composition, "
            "sparkline, line, scatter (aliases: horizontal_bar, vertical_bar, pie, trend, plot, xy).")
    if kind == "hbar":
        return hbar(data, width=width, ascii_only=ascii_only, vmin=vmin, vmax=vmax)
    if kind == "vbar":
        return vbar(data, height=height, ascii_only=ascii_only, vmin=vmin, vmax=vmax)
    if kind == "composition":
        return composition(data, width=width, ascii_only=ascii_only)
    if kind == "sparkline":
        return sparkline(data, ascii_only=ascii_only, show_range=True)
    if kind == "scatter":
        return scatter(data, width=width, height=height, ascii_only=ascii_only)
    return line(data, height=height, width=width, ascii_only=ascii_only, vmin=vmin, vmax=vmax)


class AsciiChartTool(Tool):
    name = "ascii_chart"
    echo_result = True          # the chart IS the deliverable — the loop guarantees it reaches the user
    description = (
        "Draw a text chart from data — renders inline in chat, no image file. "
        "IMPORTANT: the result is a finished chart already inside a ``` code block — paste it "
        "VERBATIM into your reply so the user can see it; do NOT just describe it, summarize it, or "
        "redraw it. Use this for a quick "
        "visual of numbers (trends, comparisons, breakdowns) when an attached image would be "
        "overkill. chart_type is one of: 'hbar' (horizontal bars — best default for comparing "
        "labelled values), 'vbar' (vertical bars/columns), 'composition' (ONE bar split into "
        "proportional segments with a % legend — a pie chart drawn as a single bar; great for "
        "shares/breakdowns that sum to a whole), 'sparkline' (a compact one-line trend), 'line' "
        "(a y-axis line plot for a series over time), or 'scatter' (an x/y point cloud). "
        "data is a list of {\"label\": <name>, \"value\": <number>}; for 'sparkline'/'line' you may "
        "pass a bare list of numbers; for 'scatter' use {\"x\": n, \"y\": n} points. "
        "Optional: title; width/height (chart size in characters/rows); vmin/vmax (pin the value "
        "axis to zoom in, e.g. vmin=60 so scores of 80–95 spread out instead of hugging the top). "
        "For a saved image chart, use make_chart instead."
    )

    class Params(BaseModel):
        chart_type: str = Field("hbar", description="hbar | vbar | composition | sparkline | line | scatter")
        data: list = Field(..., description='[{"label":"Mon","value":82},...]; numbers for sparkline/line; {"x":,"y":} for scatter')
        title: str = Field("", description="optional heading shown above the chart")
        width: int = Field(40, description="chart width in characters (hbar/composition/line/scatter)")
        height: int = Field(12, description="chart height in rows (vbar/line/scatter)")
        vmin: float | None = Field(None, description="pin the value-axis minimum (zoom); default 0 for bars, data-min for line")
        vmax: float | None = Field(None, description="pin the value-axis maximum; default is the data max")
        ascii_only: bool = Field(True, description="pure ASCII (default; aligns on every device incl. phones). False = prettier Unicode blocks, best on desktop")

    async def run(self, args: "AsciiChartTool.Params") -> str:
        width = max(8, min(int(args.width or 40), 120))
        height = max(3, min(int(args.height or 12), 30))
        data = args.data
        if isinstance(data, list) and len(data) > 200:
            data = data[:200]                       # cap so output can't blow the message limit
        try:
            chart = render(args.chart_type, data, width=width, height=height,
                           ascii_only=args.ascii_only, vmin=args.vmin, vmax=args.vmax)
        except ValueError as e:
            return f"ascii_chart: {e}"
        except Exception as e:                      # never crash the loop
            return f"ascii_chart: could not render ({type(e).__name__}: {e})."
        title = (args.title or "").strip()
        head = f"{title}\n" if title else ""
        return f"{head}```\n{chart}\n```"
