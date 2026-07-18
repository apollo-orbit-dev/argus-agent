"""make_chart rendering + build_web_page image inlining."""
import asyncio

from engine.tools.artifacts import inline_workspace_images
from engine.tools.charts import MakeChartTool
from engine.tools.files import FileWorkspace


def _ws(tmp_path):
    return FileWorkspace(str(tmp_path / "ws"))


def test_make_chart_positional_columns(tmp_path):
    # rows straight from query_rows (arbitrary column names, no label/value keys) chart positionally:
    # first column -> label, second -> value.
    ws = _ws(tmp_path)
    t = MakeChartTool(ws, "sess1", lambda s, p: None)
    out = asyncio.run(t.run(t.Params(
        title="Monthly", chart_type="bar",
        data=[{"month": "2026-06", "avg": 420}, {"month": "2026-07", "avg": 430}])))
    assert "created" in out.lower()
    assert "monthly.png" in [f["name"] for f in ws.list()]


def test_make_chart_saves_png_and_svg(tmp_path):
    ws = _ws(tmp_path)
    recorded = []
    t = MakeChartTool(ws, "sess1", lambda s, p: recorded.append((s, p)))
    out = asyncio.run(t.run(t.Params(
        title="Weekly Report", chart_type="bar",
        data=[{"label": "Mon", "value": 82}, {"label": "Tue", "value": 75}])))
    assert "created" in out.lower()
    names = [f["name"] for f in ws.list()]
    assert "weekly-report.png" in names and "weekly-report.svg" in names
    # PNG is a real image (PNG magic bytes) and was recorded for the Telegram layer
    png = ws.read_text  # noqa - use path
    import os
    with open(ws.path_if_exists("weekly-report.png"), "rb") as fh:
        assert fh.read(4) == b"\x89PNG"
    assert recorded and recorded[0][0] == "sess1"
    assert os.path.basename(recorded[0][1]) == "weekly-report.png"


def test_make_chart_types(tmp_path):
    ws = _ws(tmp_path)
    for ct in ("bar", "line", "pie", "scatter"):
        t = MakeChartTool(ws)
        out = asyncio.run(t.run(t.Params(
            title=f"c-{ct}", chart_type=ct,
            data=[{"label": "a", "value": 3}, {"label": "b", "value": 5}])))
        assert "created" in out.lower(), ct
        assert ws.exists(f"c-{ct}.png")


def test_make_chart_empty_data(tmp_path):
    t = MakeChartTool(_ws(tmp_path))
    out = asyncio.run(t.run(t.Params(title="x", chart_type="bar", data=[])))
    assert "no data" in out.lower()


def test_inline_workspace_images(tmp_path):
    ws = _ws(tmp_path)
    ws.save_bytes("chart.png", b"\x89PNG\r\n\x1a\nfakepngbytes")
    html = '<h1>Report</h1><img src="chart.png" alt="c"><img src="https://x.com/a.png">'
    out = inline_workspace_images(html, ws)
    assert 'src="data:image/png;base64,' in out          # workspace image inlined
    assert 'src="https://x.com/a.png"' in out            # external ref untouched
    # a reference to a non-existent workspace file is left as-is
    assert inline_workspace_images('<img src="missing.png">', ws) == '<img src="missing.png">'
