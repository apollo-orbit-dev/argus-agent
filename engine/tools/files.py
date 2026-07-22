"""File workspace — a vetted, path-safe files area the agent can read/write/list.

The datastore's sibling for FILES instead of key/values. The sandbox forbids file I/O, so
this is the safe, first-class way to let the agent keep working files: save a generated
report, read an uploaded CSV, hand a file to the document reader or build_web_page. Names may
include subdirectories (e.g. 'reports/july.md') but no path traversal or absolute paths;
everything resolves under one workspace directory (see safe_path).
"""
from __future__ import annotations

import os
import re
import time
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from engine.tools.base import Tool

_MAX_READ = 40000                 # chars returned by read_file (avoid flooding the context)
_MAX_DOWNLOAD = 25 * 1024 * 1024  # 25 MB cap on downloaded files
_CTYPE_EXT = {"application/pdf": ".pdf", "text/csv": ".csv", "text/plain": ".txt",
              "application/json": ".json", "image/png": ".png", "image/jpeg": ".jpg",
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx"}


def safe_name(name: str) -> str:
    """Flatten to a safe basename: strip any path, keep alnum/dot/dash/underscore/space."""
    base = os.path.basename((name or "").strip().replace("\\", "/").rstrip("/"))
    base = re.sub(r"[^A-Za-z0-9._ -]+", "_", base).strip(". ")
    return base[:120] or ""


def _safe_component(part: str) -> str:
    """Sanitise ONE path component with the same character policy as safe_name()."""
    part = re.sub(r"[^A-Za-z0-9._ -]+", "_", part).strip(". ")
    return part[:120]


def safe_path(root: str, name: str) -> str:
    """Resolve `name` under `root`, allowing subdirectories. Returns an absolute path.

    Rejects absolute paths, any '..' component, and anything whose realpath escapes root —
    including via a symlink already inside the workspace, which is why the check is on the
    RESOLVED path rather than the joined one. Raises ValueError on rejection; callers turn that
    into a tool-level error message rather than letting it reach the model as a traceback.
    """
    rel = (name or "").strip().replace("\\", "/")
    if os.path.isabs(rel) or rel.startswith("/"):
        raise ValueError("absolute paths are not allowed")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ValueError("path traversal is not allowed")
    parts = [c for c in (_safe_component(p) for p in parts) if c]
    if not parts:
        raise ValueError("invalid file name")
    root_real = os.path.realpath(root)
    full = os.path.realpath(os.path.join(root_real, *parts))
    if full != root_real and not full.startswith(root_real + os.sep):
        raise ValueError("path escapes the workspace")
    return full


class FileWorkspace:
    def __init__(self, root: str):
        self.root = root

    def _path(self, name: str) -> str:
        return safe_path(self.root, name)

    def rel(self, name: str) -> str:
        """The POSIX-relative form of `name` — what the model and the dashboard see."""
        full = self._path(name)
        return os.path.relpath(full, os.path.realpath(self.root)).replace(os.sep, "/")

    def write_text(self, name: str, content: str) -> str:
        p = self._path(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(content or "")
        return self.rel(name)

    def save_bytes(self, name: str, data: bytes) -> str:
        p = self._path(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as fh:
            fh.write(data)
        return self.rel(name)

    def read_text(self, name: str) -> str:
        with open(self._path(name), encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def path_if_exists(self, name: str):
        try:
            p = self._path(name)
        except ValueError:
            return None
        return p if os.path.isfile(p) else None

    def exists(self, name: str) -> bool:
        return self.path_if_exists(name) is not None

    def delete(self, name: str) -> bool:
        p = self.path_if_exists(name)
        if p:
            os.remove(p)
            return True
        return False

    def list(self, max_depth: int = 4) -> list[dict]:
        """Every file under the workspace, newest first, as POSIX-relative paths.

        Depth-capped so a runaway `mkdir -p` inside the container can't make listing the workspace
        expensive, and symlinked directories are not followed (os.walk defaults to followlinks=False,
        which is what keeps a symlink from walking us out of the tree)."""
        root_real = os.path.realpath(self.root)
        if not os.path.isdir(root_real):
            return []
        out = []
        for dirpath, dirnames, filenames in os.walk(root_real):
            depth = 0 if dirpath == root_real else dirpath[len(root_real) + 1:].count(os.sep) + 1
            if depth >= max_depth:
                dirnames[:] = []
                continue
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if os.path.isfile(p):
                    out.append({"name": os.path.relpath(p, root_real).replace(os.sep, "/"),
                                "size": os.path.getsize(p),
                                "modified": os.path.getmtime(p)})
        out.sort(key=lambda f: f["modified"], reverse=True)
        return out


class WriteFileTool(Tool):
    name = "write_file"
    description = ("Save text to a file in your workspace (a report, notes, extracted data, CSV, "
                   "etc.). Overwrites a file of the same name. Args: name, content.")

    class Params(BaseModel):
        name: str = Field(..., description="file name, e.g. report.md")
        content: str = Field(..., description="the text to write")

    def __init__(self, ws: FileWorkspace):
        self.ws = ws

    async def run(self, args: "WriteFileTool.Params") -> str:
        try:
            saved = self.ws.write_text(args.name, args.content)
        except ValueError as e:
            return f"write_file error: {e}."
        return f"write_file: saved '{saved}' ({len(args.content or '')} chars) to your workspace."


class ReadFileTool(Tool):
    name = "read_file"
    description = ("Read a text file from your workspace by name. For PDFs/Word/Excel or scanned "
                   "documents, use read_document instead. Arg: name.")

    class Params(BaseModel):
        name: str = Field(..., description="file name to read")

    def __init__(self, ws: FileWorkspace):
        self.ws = ws

    async def run(self, args: "ReadFileTool.Params") -> str:
        if not self.ws.exists(args.name):
            names = ", ".join(f["name"] for f in self.ws.list()) or "(empty)"
            return f"read_file: no file '{args.name}'. Files in workspace: {names}."
        text = self.ws.read_text(args.name)
        if len(text) > _MAX_READ:
            text = text[:_MAX_READ] + f"\n… (truncated; file is {len(text)} chars)"
        return text


class ListFilesTool(Tool):
    name = "list_files"
    description = ("List the files in your workspace (path, size). Paths may include "
                   "subdirectories, e.g. 'reports/july.md'. No arguments.")

    class Params(BaseModel):
        pass

    def __init__(self, ws: FileWorkspace):
        self.ws = ws

    async def run(self, args: "ListFilesTool.Params") -> str:
        files = self.ws.list()
        if not files:
            return "Your workspace is empty."
        return "Files in your workspace:\n" + "\n".join(
            f"  {f['name']}  ({f['size']} bytes)" for f in files)


class DownloadFileTool(Tool):
    name = "download_file"
    description = (
        "Download a file from a PUBLIC http(s) URL and save it to your workspace — then you can "
        "read it (read_document handles PDFs, incl. scanned via OCR) or add it to your knowledge "
        "base. Use this when the user gives you a link to a document/file. Args: url, and optional "
        "name (defaults to the URL's filename; an extension is inferred from the content type)."
    )

    class Params(BaseModel):
        url: str = Field(..., description="public http(s) URL of the file to download")
        name: str = Field("", description="file name to save as (optional)")

    def __init__(self, ws: FileWorkspace):
        self.ws = ws

    async def run(self, args: "DownloadFileTool.Params") -> str:
        from engine.tools.net_guard import BlockedURLError, safe_fetch
        url = args.url.strip()
        try:
            r = await safe_fetch(url, max_bytes=_MAX_DOWNLOAD)
        except BlockedURLError as e:
            return f"download_file: {e}. Only public http(s) URLs are allowed."
        except Exception as e:
            return f"download_file: couldn't download it ({type(e).__name__}: {e})."
        if r.status_code >= 400:
            return f"download_file: the server returned HTTP {r.status_code} for that URL."
        name = (args.name.strip() or os.path.basename(urlparse(url).path) or "download")
        if not os.path.splitext(name)[1]:            # no extension → infer from content type
            ctype = (r.headers.get("content-type", "").split(";")[0].strip().lower())
            name += _CTYPE_EXT.get(ctype, "")
        try:
            saved = self.ws.save_bytes(name, r.content)
        except ValueError as e:
            return f"download_file error: {e}."
        return (f"download_file: saved '{saved}' ({len(r.content)} bytes) to your workspace. "
                "You can now read it (read_document) or add it to your knowledge base.")


class DeleteFileTool(Tool):
    name = "delete_file"
    description = "Delete a file from your workspace by name. Arg: name."

    class Params(BaseModel):
        name: str = Field(..., description="file name to delete")

    def __init__(self, ws: FileWorkspace):
        self.ws = ws

    async def run(self, args: "DeleteFileTool.Params") -> str:
        try:
            deleted = self.ws.delete(args.name)
        except ValueError as e:
            return f"delete_file error: {e}."
        return (f"delete_file: '{args.name}' deleted." if deleted
                else f"delete_file: no file '{args.name}'.")
