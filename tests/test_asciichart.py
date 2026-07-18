"""ascii_chart — dependency-free text charts (library funcs + the AsciiChartTool)."""
import asyncio

from config import Config
from engine.engine import build_base_registry
from engine.tools import asciichart as ac

WEEK = [{"label": "Mon", "value": 82}, {"label": "Tue", "value": 75}, {"label": "Wed", "value": 91}]
STAGES = [{"label": "Deep", "value": 95}, {"label": "Light", "value": 240}, {"label": "REM", "value": 110}]


def _mk(**over):
    base = dict(model_base_url="http://x/v1", model_name="main", telegram_bot_token="")
    base.update(over)
    return Config(**base)


# ── library: every chart renders, and every content line is flush-left (no stray indent) ──
def _no_leading_indent(chart: str, *, allow=()):
    """Every line is flush-left except explicitly allowed axis rows (scatter's x-axis)."""
    for ln in chart.splitlines():
        if ln.strip() and not ln.startswith(allow):
            assert not ln.startswith(" "), f"unexpected leading space: {ln!r}"


def test_hbar_aligns_and_shows_values():
    out = ac.hbar(WEEK, width=20)
    _no_leading_indent(out)
    lines = out.splitlines()
    assert len(set(len(l) for l in lines)) == 1                   # equal-length rows → value column aligned
    assert lines[0].endswith("82")


def test_vbar_has_value_and_label_rows():
    out = ac.vbar(WEEK, height=6)
    lines = out.splitlines()
    assert "82" in lines[0] and lines[-1].startswith("Mon")       # values on top, labels below


def test_composition_percentages_sum_to_100():
    out = ac.composition(STAGES, width=30)
    import re
    pcts = [int(x) for x in re.findall(r"\((\d+)%\)", out)]
    assert sum(pcts) == 100


def test_sparkline_and_range():
    out = ac.sparkline([1, 2, 3, 4, 5], show_range=True)
    assert "(1-5)" in out or "(1–5)" in out


def test_line_first_row_flush():
    out = ac.line([20, 35, 55, 72, 80, 40, 18], height=8, width=20, ascii_only=True)
    _no_leading_indent(out)


def test_scatter_density_count():
    # two points land on the same cell → renders a count, not a single marker
    out = ac.scatter([{"x": 1, "y": 1}, {"x": 1, "y": 1}, {"x": 5, "y": 5}], width=10, height=6)
    assert "2" in out


def test_ascii_only_is_pure_7bit():
    for kind, data in [("hbar", WEEK), ("vbar", WEEK), ("composition", STAGES),
                       ("sparkline", [1, 3, 2, 5]), ("line", [1, 5, 2, 8]), ("scatter", [{"x": 1, "y": 2}])]:
        out = ac.render(kind, data, ascii_only=True)
        assert out.isascii(), f"{kind} ascii_only produced non-ASCII: {out!r}"


def test_unicode_uses_block_glyphs():
    assert "█" in ac.hbar(WEEK, ascii_only=False)


def test_vmin_zoom_changes_bars():
    base = ac.hbar(WEEK, width=20, ascii_only=True)
    zoomed = ac.hbar(WEEK, width=20, ascii_only=True, vmin=60)
    assert base != zoomed                                          # pinning the floor rescales


def test_unknown_type_raises():
    try:
        ac.render("piechart3d", WEEK)
        assert False, "expected ValueError"
    except ValueError as e:
        assert "unknown chart_type" in str(e)


# ── tool wrapper ──
def test_tool_wraps_in_code_fence_with_title():
    t = ac.AsciiChartTool()
    out = asyncio.run(t.run(t.Params(chart_type="hbar", data=WEEK, title="Weekly")))
    assert out.startswith("Weekly\n```") and out.rstrip().endswith("```")


def test_tool_bad_type_is_graceful():
    t = ac.AsciiChartTool()
    out = asyncio.run(t.run(t.Params(chart_type="nope", data=WEEK)))
    assert out.startswith("ascii_chart:") and "unknown chart_type" in out


def test_tool_caps_dimensions():
    t = ac.AsciiChartTool()
    out = asyncio.run(t.run(t.Params(chart_type="hbar", data=WEEK, width=9999)))
    assert "```" in out                                           # didn't crash on absurd width


# ── registration ──
def test_registered_when_enabled():
    assert "ascii_chart" in build_base_registry(_mk(enable_ascii_charts=True)).names()


def test_absent_when_disabled():
    assert "ascii_chart" not in build_base_registry(_mk(enable_ascii_charts=False)).names()


# ── loop delivery guarantee (echo_result) ──
from engine.loop import _with_unechoed


def test_unechoed_chart_is_appended():
    chart = "Title\n```\nMon | #### 82\nWed | ######## 91\n```"
    out = _with_unechoed("Wednesday leads at 91.", [chart])
    assert "######## 91" in out                       # the model described it; loop appended the real chart


def test_echoed_chart_not_duplicated():
    chart = "Title\n```\nMon | #### 82\nWed | ######## 91\n```"
    out = _with_unechoed("Here it is:\n" + chart, [chart])
    assert out.count("Mon | #### 82") == 1            # model already pasted it → not doubled


def test_ascii_chart_tool_opts_into_echo():
    assert ac.AsciiChartTool().echo_result is True
