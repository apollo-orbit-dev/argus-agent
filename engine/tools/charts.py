"""make_chart — render a bar/line/pie/scatter chart to PNG (view/Telegram) + SVG (embed in pages).

Vetted built-in: turning data into a correct chart is exactly the thing a small model fumbles when
hand-writing SVG/JS (flat bars, broken math). Matplotlib does it reliably. Saves BOTH a PNG (raster,
so Telegram can send it as a photo and it renders anywhere) and an SVG (scalable, self-contained for
embedding in a build_web_page report) to the file workspace.
"""
from __future__ import annotations

import asyncio
import io
import os

import matplotlib
matplotlib.use("Agg")                      # headless: no display, thread-safe rendering
import matplotlib.pyplot as plt            # noqa: E402
from pydantic import BaseModel, Field      # noqa: E402

from engine.tools.base import Tool         # noqa: E402
from engine.tools.files import FileWorkspace, safe_name  # noqa: E402

_PALETTE = ["#4C78A8", "#F58518", "#54A24B", "#E45756", "#72B7B2", "#B279A2", "#FF9DA6"]


def _slug(s: str) -> str:
    import re
    base = re.sub(r"[^a-z0-9]+", "-", safe_name(s).lower()).strip("-")
    return (os.path.splitext(base)[0] or "chart")[:60]


class MakeChartTool(Tool):
    name = "make_chart"
    description = (
        "Create a chart from data and save it as an image. Provide: title; chart_type "
        "('bar', 'line', 'pie', or 'scatter'); and data as a list of points, each "
        "{\"label\": <name>, \"value\": <number>} (for scatter you may use {\"x\": n, \"y\": n}). "
        "Optional x_label, y_label. It saves a PNG (which the user can view, and which is sent "
        "automatically in Telegram) and an SVG (to embed in a web page). Use this instead of "
        "hand-drawing charts in HTML."
    )

    class Params(BaseModel):
        title: str = Field(..., description="chart title")
        chart_type: str = Field("bar", description="bar | line | pie | scatter")
        data: list[dict] = Field(..., description='[{"label": "Mon", "value": 82}, ...]')
        x_label: str = Field("", description="x-axis label (optional)")
        y_label: str = Field("", description="y-axis label (optional)")
        name: str = Field("", description="file name to save as (optional)")

    def __init__(self, ws: FileWorkspace, session_id: str = None, on_image=None):
        self.ws = ws
        self.session_id = session_id
        self.on_image = on_image

    async def run(self, args: "MakeChartTool.Params") -> str:
        try:
            return await asyncio.to_thread(self._render, args)   # matplotlib is blocking
        except Exception as e:
            return f"make_chart: could not render the chart ({type(e).__name__}: {e})."

    def _render(self, args: "MakeChartTool.Params") -> str:
        if not args.data:
            return "make_chart: no data given — provide a list of {label, value} points."
        labels, values, xs = [], [], []
        _known = ("label", "x", "value", "y")
        for i, d in enumerate(args.data):
            # Positional fallback: rows straight from SQL (query_rows) have arbitrary column names,
            # e.g. {"month": "2026-06", "avg": 420}. When none of the known keys are present, treat
            # the first column as the label and the second as the value.
            if isinstance(d, dict) and not any(k in d for k in _known) and len(d) >= 2:
                vals = list(d.values())
                labels.append(str(vals[0]))
                v = vals[1]
            else:
                labels.append(str(d.get("label", d.get("x", i))))
                v = d.get("value", d.get("y"))
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                values.append(0.0)
            try:
                xs.append(float(d.get("x", i)))
            except (TypeError, ValueError):
                xs.append(float(i))
        ct = (args.chart_type or "bar").lower().strip()
        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=110)
        try:
            if ct == "pie":
                ax.pie(values, labels=labels, autopct="%1.0f%%", colors=_PALETTE)
            elif ct == "line":
                ax.plot(labels, values, marker="o", color=_PALETTE[0])
            elif ct == "scatter":
                ax.scatter(xs, values, color=_PALETTE[0])
            else:                                       # bar (default)
                ax.bar(labels, values, color=_PALETTE[0])
            ax.set_title(args.title)
            if args.x_label:
                ax.set_xlabel(args.x_label)
            if args.y_label:
                ax.set_ylabel(args.y_label)
            if ct in ("bar", "line", "scatter") and len(labels) > 6:
                plt.setp(ax.get_xticklabels(), rotation=45, ha="right")
            fig.tight_layout()
            base = _slug(args.name or args.title)
            png_buf, svg_buf = io.BytesIO(), io.BytesIO()
            fig.savefig(png_buf, format="png")
            fig.savefig(svg_buf, format="svg")
        finally:
            plt.close(fig)
        png_name = self.ws.save_bytes(base + ".png", png_buf.getvalue())
        svg_name = self.ws.save_bytes(base + ".svg", svg_buf.getvalue())
        if self.on_image and self.session_id:
            path = self.ws.path_if_exists(png_name)
            if path:
                self.on_image(self.session_id, path)
        return (f"make_chart: created a {ct} chart '{args.title}'. Saved {png_name} (image — the "
                f"user can view it, and it's sent automatically in Telegram) and {svg_name} "
                "(scalable, for embedding in a web page). Both are in the workspace.")
