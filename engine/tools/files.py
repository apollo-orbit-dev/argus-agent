"""File workspace — a vetted, path-safe files area the agent can read/write/list.

The datastore's sibling for FILES instead of key/values. The sandbox forbids file I/O, so
this is the safe, first-class way to let the agent keep working files: save a generated
report, read an uploaded CSV, hand a file to the document reader or build_web_page. Names are
flattened to a safe basename (no path traversal, no absolute paths); everything lives under one
workspace directory.
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


class FileWorkspace:
    def __init__(self, root: str):
        self.root = root

    def _path(self, name: str) -> str:
        safe = safe_name(name)
        if not safe:
            raise ValueError("invalid file name")
        return os.path.join(self.root, safe)

    def write_text(self, name: str, content: str) -> str:
        os.makedirs(self.root, exist_ok=True)
        safe = safe_name(name)
        with open(self._path(name), "w", encoding="utf-8") as fh:
            fh.write(content or "")
        return safe

    def save_bytes(self, name: str, data: bytes) -> str:
        os.makedirs(self.root, exist_ok=True)
        safe = safe_name(name)
        with open(self._path(name), "wb") as fh:
            fh.write(data)
        return safe

    def read_text(self, name: str) -> str:
        with open(self._path(name), encoding="utf-8", errors="replace") as fh:
            return fh.read()

    def path_if_exists(self, name: str):
        p = self._path(name)
        return p if os.path.isfile(p) else None

    def exists(self, name: str) -> bool:
        return self.path_if_exists(name) is not None

    def delete(self, name: str) -> bool:
        p = self._path(name)
        if os.path.isfile(p):
            os.remove(p)
            return True
        return False

    def list(self) -> list[dict]:
        if not os.path.isdir(self.root):
            return []
        out = []
        for fn in os.listdir(self.root):
            p = os.path.join(self.root, fn)
            if os.path.isfile(p):
                out.append({"name": fn, "size": os.path.getsize(p),
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
        if not safe_name(args.name):
            return "write_file error: invalid file name."
        saved = self.ws.write_text(args.name, args.content)
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
    description = "List the files in your workspace (name, size). No arguments."

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
        name = safe_name(args.name.strip() or os.path.basename(urlparse(url).path)) or "download"
        if not os.path.splitext(name)[1]:            # no extension → infer from content type
            ctype = (r.headers.get("content-type", "").split(";")[0].strip().lower())
            name += _CTYPE_EXT.get(ctype, "")
        saved = self.ws.save_bytes(name, r.content)
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
        return (f"delete_file: '{safe_name(args.name)}' deleted."
                if self.ws.delete(args.name) else f"delete_file: no file '{args.name}'.")
