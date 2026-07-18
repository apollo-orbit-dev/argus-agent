"""make_pdf — create a PDF document from HTML the agent writes (WeasyPrint).

The agent already writes HTML well (build_web_page + the frontend_design skill), so a PDF is just
that HTML rendered to paper: headings, tables, lists, and images all work, and a chart embeds by
referencing its workspace file (inlined as base64, same as build_web_page). Saved to the workspace →
downloadable and emailable via notify/attachments. Rendering is network-blocked (only inlined data:
images load — no external fetches during render), so a PDF is fully self-contained and SSRF-safe.
"""
from __future__ import annotations

import asyncio
import io
import os
from html import escape

from pydantic import BaseModel, Field

from engine.tools.artifacts import ensure_document, inline_workspace_images, slugify
from engine.tools.base import Tool
from engine.tools.files import FileWorkspace, safe_name

_DOC_CSS = (
    "body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;line-height:1.55;color:#1a1a1a;"
    "font-size:12pt}h1,h2,h3{line-height:1.25}pre{white-space:pre-wrap;word-wrap:break-word;"
    "background:#f6f8fa;padding:12px;border-radius:6px;font-family:ui-monospace,monospace;font-size:.9em}"
    "code{background:#f6f8fa;padding:2px 4px;border-radius:3px;font-family:ui-monospace,monospace}"
    # code INSIDE a pre block must not re-apply inline padding/background — on a multi-line inline
    # <code> the left padding lands only on the first line and shifts it right (misaligns charts/tables).
    "pre code{background:none;padding:0;border-radius:0;font-size:inherit}"
    "table{border-collapse:collapse;width:100%;margin:12px 0}th,td{border:1px solid #ccc;padding:6px 10px;"
    "text-align:left}th{background:#f0f0f0}img{max-width:100%}blockquote{border-left:3px solid #ddd;"
    "margin:0;padding-left:14px;color:#555}")


def _wrap(title: str, body_html: str) -> str:
    return (f"<!doctype html><html><head><meta charset=\"utf-8\"><title>{escape(title)}</title>"
            f"<style>{_DOC_CSS}</style></head><body>{body_html}</body></html>")


def _csv_to_table(text: str, sep: str) -> str:
    import csv
    import io as _io
    rows = list(csv.reader(_io.StringIO(text), delimiter=sep))
    if not rows:
        return "<p>(empty)</p>"
    head = "".join(f"<th>{escape(c)}</th>" for c in rows[0])
    body = "".join("<tr>" + "".join(f"<td>{escape(c)}</td>" for c in r) + "</tr>" for r in rows[1:])
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def file_to_html(name: str, text: str, ws) -> str:
    """Turn a file's contents into a full HTML document for PDF rendering, by extension."""
    base = os.path.splitext(os.path.basename(name))[0]
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    if ext in ("md", "markdown"):
        import markdown
        return _wrap(base, markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"]))
    if ext in ("html", "htm"):
        return ensure_document(text, base)                      # respect the file's own styling
    if ext in ("csv", "tsv"):
        return _wrap(base, _csv_to_table(text, "\t" if ext == "tsv" else ","))
    if ext in ("txt", "text", "log", "json", "yaml", "yml", ""):
        return _wrap(base, f"<pre>{escape(text)}</pre>")
    raise ValueError(f"can't convert '.{ext}' to PDF")


def _no_external_fetch(url: str):
    """WeasyPrint url_fetcher: allow inlined data: URIs only; block http/file/relative so PDF
    rendering can't fetch external resources or read local files."""
    if url.startswith("data:"):
        from weasyprint.urls import default_url_fetcher
        return default_url_fetcher(url)
    raise ValueError("external resources are not allowed in PDF rendering — inline them")


class MakePdfTool(Tool):
    name = "make_pdf"
    description = (
        "Create a PDF document. Write the content as HTML in `html` — headings, paragraphs, tables, "
        "lists, and images all render; embed a chart or image by referencing its workspace file name "
        "in an <img src=\"chart.png\">. Give a `title`. The PDF is saved to your workspace, where the "
        "user can download it and you can email it as an attachment (notify). Use for a report, "
        "invoice, summary, letter — any document the user wants as a PDF."
    )

    class Params(BaseModel):
        title: str = Field(..., description="document title")
        html: str = Field(..., description="the document content as HTML")
        name: str = Field("", description="file name to save as (optional)")

    def __init__(self, ws: FileWorkspace):
        self.ws = ws

    async def run(self, args: "MakePdfTool.Params") -> str:
        if not (args.html or "").strip():
            return "make_pdf error: html is empty — write the document content."
        try:
            return await asyncio.to_thread(self._render, args)
        except Exception as e:
            return f"make_pdf error: could not render the PDF ({type(e).__name__}: {e})."

    def _render(self, args: "MakePdfTool.Params") -> str:
        from weasyprint import HTML
        doc = ensure_document(args.html, args.title.strip() or "document")
        doc = inline_workspace_images(doc, self.ws)          # embed referenced charts/images
        base = slugify(safe_name(args.name) or args.title) or "document"
        buf = io.BytesIO()
        HTML(string=doc, url_fetcher=_no_external_fetch).write_pdf(buf)
        saved = self.ws.save_bytes(base + ".pdf", buf.getvalue())
        return (f"make_pdf: created '{saved}' ({len(buf.getvalue())} bytes) in your workspace — the "
                "user can download it, and you can email it as an attachment with notify.")


class ConvertToPdfTool(Tool):
    name = "convert_to_pdf"
    description = (
        "Convert an EXISTING file in your workspace to a PDF. Markdown (.md), plain text "
        "(.txt/.log/.json), HTML, and CSV are rendered with formatting; Word/Excel (.docx/.xlsx) are "
        "converted as their extracted text. Use when the user wants a file turned into a PDF. "
        "Arg: name (the file in your workspace)."
    )

    class Params(BaseModel):
        name: str = Field(..., description="the workspace file to convert to PDF")

    def __init__(self, ws: FileWorkspace):
        self.ws = ws

    async def run(self, args: "ConvertToPdfTool.Params") -> str:
        path = self.ws.path_if_exists(args.name)
        if not path:
            names = ", ".join(f["name"] for f in self.ws.list()) or "(empty)"
            return f"convert_to_pdf: no file '{args.name}' in the workspace. Files: {names}."
        try:
            return await asyncio.to_thread(self._render, args.name, path)
        except ValueError as e:
            return f"convert_to_pdf: {e}."
        except Exception as e:
            return f"convert_to_pdf error: could not convert ({type(e).__name__}: {e})."

    def _render(self, name: str, path: str) -> str:
        from weasyprint import HTML
        ext = os.path.splitext(name)[1].lower().lstrip(".")
        base = slugify(os.path.splitext(os.path.basename(name))[0]) or "document"
        if ext == "pdf":
            return f"'{name}' is already a PDF"
        if ext in ("docx", "xlsx", "xlsm"):
            from engine.tools.documents import extract_document
            doc = _wrap(base, f"<pre>{escape(extract_document(path))}</pre>")
        else:
            with open(path, encoding="utf-8", errors="replace") as fh:
                doc = file_to_html(name, fh.read(), self.ws)
        doc = inline_workspace_images(doc, self.ws)
        buf = io.BytesIO()
        HTML(string=doc, url_fetcher=_no_external_fetch).write_pdf(buf)
        saved = self.ws.save_bytes(base + ".pdf", buf.getvalue())
        return (f"convert_to_pdf: converted '{name}' → {saved} ({len(buf.getvalue())} bytes) in your "
                "workspace — downloadable and emailable with notify.")
