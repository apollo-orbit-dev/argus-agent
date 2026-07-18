"""build_web_page — a vetted tool that persists a self-contained HTML artifact.

The MODEL writes the HTML/CSS/JS (its frontend-design skill); this tool just saves it to a
fixed artifacts directory and returns a link. Artifacts are viewable/served from the dashboard.
Same title -> same slug -> updates in place (so the user can iterate on one dashboard). This is
a first-class tool, not model-authored sandboxed code, so no import/AST restrictions apply.
"""
from __future__ import annotations

import os
import re
import time
from html import escape

from pydantic import BaseModel, Field

from engine.tools.base import Tool


def slugify(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return s or "artifact"


def ensure_document(html: str, title: str) -> str:
    """Guarantee a complete, self-contained HTML document with a <title> (so the artifact list
    can show a name). If the model already returned a full document, trust it; otherwise wrap
    the fragment in a minimal responsive skeleton."""
    low = (html or "").lstrip().lower()
    if low.startswith("<!doctype") or low.startswith("<html"):
        if "<title" not in low:  # inject a title so the listing has a name
            html = re.sub(r"(<head[^>]*>)", rf"\1<title>{escape(title)}</title>", html,
                          count=1, flags=re.IGNORECASE)
        return html
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{escape(title)}</title></head>\n<body>\n{html}\n</body></html>\n"
    )


def _title_of(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            head = fh.read(4000)
        m = re.search(r"<title[^>]*>(.*?)</title>", head, re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return os.path.splitext(os.path.basename(path))[0]


def list_artifacts(artifacts_dir: str) -> list[dict]:
    """Every saved artifact (filename, title, url, modified epoch), newest first."""
    out = []
    if not os.path.isdir(artifacts_dir):
        return out
    for fn in os.listdir(artifacts_dir):
        if not fn.endswith(".html"):
            continue
        p = os.path.join(artifacts_dir, fn)
        out.append({"filename": fn, "title": _title_of(p),
                    "url": f"/artifacts/{fn}", "modified": os.path.getmtime(p)})
    out.sort(key=lambda a: a["modified"], reverse=True)
    return out


def delete_artifact(artifacts_dir: str, filename: str) -> bool:
    # guard against path traversal — only a bare *.html basename in the dir
    if "/" in filename or "\\" in filename or not filename.endswith(".html"):
        return False
    p = os.path.join(artifacts_dir, filename)
    if os.path.isfile(p):
        os.remove(p)
        return True
    return False


def validate_page(html: str) -> list[str]:
    """Cheap static checks for the two failures small models hit most on web pages:
    (1) an event handler (onclick=…) that calls a function no <script> defines — the page
    looks fine but its buttons/tabs are DEAD (the real bug from the field); (2) loading an
    external resource, which breaks the offline/self-contained requirement. Returns warnings."""
    warns = []
    # (1) undefined inline-handler functions
    handlers = set(re.findall(r"on\w+\s*=\s*[\"']\s*([A-Za-z_]\w*)\s*\(", html))
    for fn in sorted(handlers):
        defined = re.search(
            rf"function\s+{fn}\b|\b{fn}\s*=\s*(?:function|\(|async)|['\"]?{fn}['\"]?\s*:", html)
        if not defined:
            warns.append(f"an element calls {fn}() (e.g. onclick) but no <script> defines "
                         f"{fn} — add the JavaScript, or the page's buttons/tabs won't work")
    # (2) external resources (script/link/img/iframe src|href) — <a href> navigation is fine
    if re.search(r"<(?:script|link|img|iframe)\b[^>]*\b(?:src|href)\s*=\s*[\"']https?://", html, re.I):
        warns.append("loads an EXTERNAL resource (script/link/img src) — inline it instead; "
                     "the page must be fully self-contained and work offline")
    # (3) truncated / incomplete document — the page ran out of output room (elaborate CSS eating
    # the budget before the body). ensure_document always closes wrapped fragments, so a doc that
    # closes NEITHER </body> nor </html> was cut off mid-markup.
    low = html.lower()
    if "</html>" not in low and "</body>" not in low:
        warns.append("the page looks INCOMPLETE — it never closes (no </body> or </html>), so it "
                     "was cut off (you likely ran out of room writing elaborate CSS). Rebuild a "
                     "COMPLETE, more COMPACT page: trim the CSS, keep it simple, and make sure the "
                     "whole document — through </body></html> — fits in one response")
    return warns


class InspectArtifactTool(Tool):
    name = "inspect_artifact"
    description = (
        "Read the current HTML of a web page you already built, so you can EXTEND or revise it "
        "precisely (then call build_web_page with the SAME title to update it in place). "
        "Argument: title (or filename) of the artifact."
    )

    class Params(BaseModel):
        title: str = Field(..., description="the artifact's title or filename")

    def __init__(self, artifacts_dir: str):
        self.artifacts_dir = artifacts_dir

    async def run(self, args: "InspectArtifactTool.Params") -> str:
        name = args.title.strip()
        fn = name if name.endswith(".html") else f"{slugify(name)}.html"
        path = os.path.join(self.artifacts_dir, fn)
        if not os.path.isfile(path):
            existing = ", ".join(a["title"] for a in list_artifacts(self.artifacts_dir)) or "(none)"
            return f"inspect_artifact: no artifact '{args.title}'. Existing: {existing}."
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        if len(html) > 12000:
            html = html[:12000] + "\n… (truncated — the full page is longer)"
        return f"Current HTML of '{name}' ({fn}):\n{html}"


_IMG_MIME = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
             "svg": "image/svg+xml", "gif": "image/gif", "webp": "image/webp"}


def inline_workspace_images(html: str, workspace) -> str:
    """Replace `src="chart.png"` references to WORKSPACE image files with base64 data URIs, so a
    page can embed a make_chart output (or an uploaded image) and stay fully self-contained."""
    import base64

    def repl(m):
        quote, name = m.group(1), m.group(2)
        if name.startswith(("http://", "https://", "data:", "/", "#")):
            return m.group(0)
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        mime = _IMG_MIME.get(ext)
        path = workspace.path_if_exists(name) if mime else None
        if not path:
            return m.group(0)
        try:
            b64 = base64.b64encode(open(path, "rb").read()).decode()
        except Exception:
            return m.group(0)
        return f"src={quote}data:{mime};base64,{b64}{quote}"

    return re.sub(r"src=([\"'])([^\"']+)\1", repl, html, flags=re.I)


class BuildWebPageTool(Tool):
    name = "build_web_page"
    description = (
        "Create a self-contained web page, dashboard, chart, or visual report as an HTML artifact "
        "the user can open in their browser. Use this whenever a VISUAL output would beat plain "
        "text (a dashboard, a chart, a formatted report, a mock UI). Write COMPLETE, self-contained "
        "HTML in `html` — inline ALL CSS and JavaScript, embed data directly, use NO external files "
        "or CDNs (it must work offline). Give a short descriptive `title`. The page is saved and a "
        "link is returned; reusing the same title UPDATES that page in place so you can iterate on it."
    )

    class Params(BaseModel):
        title: str = Field(..., description="short descriptive title for the page")
        html: str = Field(..., description="complete self-contained HTML (inline CSS/JS, no external files)")

    def __init__(self, artifacts_dir: str, workspace=None):
        self.artifacts_dir = artifacts_dir
        self.workspace = workspace

    async def run(self, args: "BuildWebPageTool.Params") -> str:
        if not (args.html or "").strip():
            return "build_web_page error: html is empty — write the full page markup."
        slug = slugify(args.title)
        filename = f"{slug}.html"
        doc = ensure_document(args.html, args.title.strip() or slug)
        if self.workspace is not None:                # embed referenced workspace images inline
            doc = inline_workspace_images(doc, self.workspace)
        try:
            os.makedirs(self.artifacts_dir, exist_ok=True)
            with open(os.path.join(self.artifacts_dir, filename), "w", encoding="utf-8") as fh:
                fh.write(doc)
        except Exception as e:
            return f"build_web_page error: could not save the page ({e})."
        msg = (f"Saved web page '{args.title.strip() or slug}' → open it at /artifacts/{filename} "
               "(it's listed in the dashboard's Artifacts panel). Reuse the same title to update it.")
        warns = validate_page(doc)
        if warns:
            msg += ("\n⚠️ The page has problems you should FIX (rebuild with the same title):\n"
                    + "\n".join(f"  - {w}" for w in warns))
        return msg
