"""make_pdf: renders HTML → a real PDF, embeds workspace images, blocks external fetches."""
import asyncio

import pytest

# WeasyPrint is an OPTIONAL dependency (the `pdf` extra) — it needs native GTK/Pango/cairo libs, so
# it isn't part of the base install and isn't present in CI. Skip this whole module when it's absent
# rather than failing (mirrors the tesseract skip in test_documents.py).
pytest.importorskip("weasyprint")

from engine.tools.files import FileWorkspace
from engine.tools.pdf import ConvertToPdfTool, MakePdfTool, _no_external_fetch, file_to_html


def _ws(tmp_path):
    return FileWorkspace(str(tmp_path / "ws"))


def test_make_pdf_produces_valid_pdf(tmp_path):
    ws = _ws(tmp_path)
    t = MakePdfTool(ws)
    out = asyncio.run(t.run(t.Params(
        title="Report", html="<h1>Q3 Report</h1><p>Revenue up 12%.</p>"
        "<table><tr><td>A</td><td>1</td></tr></table>")))
    assert "created" in out.lower()
    assert ws.exists("report.pdf")
    with open(ws.path_if_exists("report.pdf"), "rb") as fh:
        head = fh.read(5)
    assert head == b"%PDF-"                       # a real PDF


def test_make_pdf_embeds_workspace_image(tmp_path):
    ws = _ws(tmp_path)
    # a tiny valid PNG (1x1) so WeasyPrint can decode it
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d494844520000000100000001080600000"
        "01f15c4890000000d49444154789c6360000002000154a24f1e0000000049454e44ae426082")
    ws.save_bytes("chart.png", png)
    t = MakePdfTool(ws)
    out = asyncio.run(t.run(t.Params(title="With Chart", html='<h1>Chart</h1><img src="chart.png">')))
    assert "created" in out.lower() and ws.exists("with-chart.pdf")


def test_make_pdf_empty_html(tmp_path):
    t = MakePdfTool(_ws(tmp_path))
    out = asyncio.run(t.run(t.Params(title="x", html="  ")))
    assert "error" in out.lower()


def test_url_fetcher_blocks_external():
    with pytest.raises(ValueError):
        _no_external_fetch("https://example.com/x.png")
    with pytest.raises(ValueError):
        _no_external_fetch("file:///etc/passwd")
    # data: URIs are allowed (returns a fetch result, not an exception)
    res = _no_external_fetch("data:text/plain;base64,aGk=")
    assert res is not None


# ---- convert_to_pdf (existing files) ----

def test_file_to_html_markdown():
    html = file_to_html("notes.md", "# Title\n\n- a\n- b\n\n| x | y |\n|---|---|\n| 1 | 2 |", None)
    assert "<h1>Title</h1>" in html and "<li>a</li>" in html and "<table>" in html


def test_file_to_html_text_escapes():
    html = file_to_html("log.txt", "error <b>x</b> & y", None)
    assert "<pre>" in html and "&lt;b&gt;" in html          # escaped, not interpreted


def test_file_to_html_unsupported():
    with pytest.raises(ValueError):
        file_to_html("thing.zip", "x", None)


def test_convert_markdown_to_pdf(tmp_path):
    ws = _ws(tmp_path)
    ws.write_text("report.md", "# Q3 Report\n\nRevenue up **12%**.\n\n- item one\n- item two")
    out = asyncio.run(ConvertToPdfTool(ws).run(ConvertToPdfTool.Params(name="report.md")))
    assert "converted" in out.lower() and ws.exists("report.pdf")
    with open(ws.path_if_exists("report.pdf"), "rb") as fh:
        assert fh.read(5) == b"%PDF-"


def test_convert_csv_to_pdf(tmp_path):
    ws = _ws(tmp_path)
    ws.write_text("data.csv", "name,score\nAlice,91\nBob,88")
    out = asyncio.run(ConvertToPdfTool(ws).run(ConvertToPdfTool.Params(name="data.csv")))
    assert "converted" in out.lower() and ws.exists("data.pdf")


def test_convert_missing_file(tmp_path):
    out = asyncio.run(ConvertToPdfTool(_ws(tmp_path)).run(ConvertToPdfTool.Params(name="nope.md")))
    assert "no file" in out.lower()


def test_convert_already_pdf(tmp_path):
    ws = _ws(tmp_path)
    ws.save_bytes("x.pdf", b"%PDF-1.4 fake")
    out = asyncio.run(ConvertToPdfTool(ws).run(ConvertToPdfTool.Params(name="x.pdf")))
    assert "already a pdf" in out.lower()
