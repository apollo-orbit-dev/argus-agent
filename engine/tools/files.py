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
    """Sanitise ONE path component with the same character policy as safe_name(), except a
    leading dot is preserved. This directory doubles as a container HOME, where dotfiles/dotdirs
    ('.bashrc', '.config/settings.json') are normal and shouldn't be mangled into 'bashrc' /
    'config/settings.json'. Trailing dots/spaces are still stripped. This is safe because
    safe_path() rejects any '..' component on the RAW, pre-sanitisation parts before this
    function ever runs — a leading dot surviving here can't resurrect path traversal.
    """
    part = re.sub(r"[^A-Za-z0-9._ -]+", "_", part)
    part = part.lstrip(" ").rstrip(". ")
    return part[:120]


def safe_path(root: str, name: str, *, resolve_leaf: bool = True) -> str:
    """Resolve `name` under `root`, allowing subdirectories. Returns an absolute path.

    Rejects absolute paths, any '..' component, and (when `resolve_leaf` is True, the default)
    anything whose realpath escapes root — including via a symlink already inside the workspace,
    which is why that check is on the RESOLVED path rather than the joined one. Raises ValueError
    on rejection; callers turn that into a tool-level error message rather than letting it reach
    the model as a traceback.

    `resolve_leaf=False` still requires every PARENT directory to resolve inside root (so
    traversal via a symlinked parent is still barred), but returns the leaf's own on-disk position
    without following it if the leaf itself is a symlink. list()/read/write never use this — they
    must keep refusing a symlink whose target escapes. It exists for delete(): a symlink planted
    in the workspace (trivial for in-container code to do) is neither readable nor writable through
    the normal path, and without this a user has no way to remove it either — see finding 5."""
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
    if resolve_leaf:
        full = os.path.realpath(os.path.join(root_real, *parts))
        if full != root_real and not full.startswith(root_real + os.sep):
            raise ValueError("path escapes the workspace")
        return full
    parent_real = (os.path.realpath(os.path.join(root_real, *parts[:-1]))
                  if len(parts) > 1 else root_real)
    if parent_real != root_real and not parent_real.startswith(root_real + os.sep):
        raise ValueError("path escapes the workspace")
    return os.path.join(parent_real, parts[-1])


def _open_no_follow(p: str, mode: str, encoding: str | None = None):
    """Open `p` for writing (create/truncate) with O_NOFOLLOW.

    safe_path() already rejects a leaf that IS a symlink at check-time, but that check and this
    open() are two separate syscalls — a symlink planted in between (TOCTOU) would otherwise be
    followed, letting a write land anywhere the link points. O_NOFOLLOW makes the open() itself
    fail if the leaf has become a symlink, closing that window. Raises ValueError (the same type
    every other rejection in this module raises) so callers don't need a second except clause.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    try:
        fd = os.open(p, flags, 0o644)
    except OSError as e:
        raise ValueError(f"couldn't write '{os.path.basename(p)}': {e.strerror or e}") from e
    return os.fdopen(fd, mode, encoding=encoding)


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
        with _open_no_follow(p, "w", encoding="utf-8") as fh:
            fh.write(content or "")
        return self.rel(name)

    def save_bytes(self, name: str, data: bytes) -> str:
        p = self._path(name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _open_no_follow(p, "wb") as fh:
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
        """Remove a file — or a symlink, which read()/write() correctly refuse to follow if its
        target escapes the workspace (safe_path's realpath check), but which the user still needs
        a way to get rid of (finding 5). Resolves only the parent directory, not the leaf itself, so
        a symlink at that exact position is located and unlinked rather than followed: os.remove()
        on a symlink removes the link entry, never the thing it points to."""
        try:
            p = safe_path(self.root, name, resolve_leaf=False)
        except ValueError:
            return False
        if os.path.islink(p) or os.path.isfile(p):
            os.remove(p)
            return True
        return False

    def _scan(self, max_depth: int) -> "tuple[list[dict], bool]":
        """Shared implementation behind list()/list_status(): every file under the workspace, plus
        whether the depth cap cut the walk short.

        Depth-capped so a runaway `mkdir -p` inside the container can't make listing the workspace
        unbounded, but the cap has to be generous enough that ordinary container trees (a
        `node_modules`, a git checkout) don't get silently dropped — safe_path() itself permits
        unlimited depth, so a low cap here just meant "the model is told the workspace is empty"
        while writes to it kept working fine (finding 6).

        Symlinks are never followed: symlinked directories are skipped via os.walk's default
        followlinks=False (which is also what keeps a symlink from walking us out of the tree), and
        a symlinked FILE is explicitly skipped below — os.path.isfile() follows symlinks, so
        without that check a symlink to something like /etc/passwd would be listed with the
        target's size, then read_file would refuse it (right) and delete_file couldn't remove it
        (wrong, now fixed above) — see finding 5.
        """
        root_real = os.path.realpath(self.root)
        if not os.path.isdir(root_real):
            return [], False
        out = []
        truncated = False
        for dirpath, dirnames, filenames in os.walk(root_real):
            depth = 0 if dirpath == root_real else dirpath[len(root_real) + 1:].count(os.sep) + 1
            if depth >= max_depth:
                dirnames[:] = []
                truncated = True
                continue
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if os.path.islink(p):
                    continue                       # never list a symlink — see finding 5
                if os.path.isfile(p):
                    out.append({"name": os.path.relpath(p, root_real).replace(os.sep, "/"),
                                "size": os.path.getsize(p),
                                "modified": os.path.getmtime(p)})
        out.sort(key=lambda f: f["modified"], reverse=True)
        return out, truncated

    def list(self, max_depth: int = 20) -> list[dict]:
        """Every file under the workspace, newest first, as POSIX-relative paths. See list_status()
        for a version that also reports whether the depth cap truncated the walk."""
        files, _truncated = self._scan(max_depth)
        return files

    def list_status(self, max_depth: int = 20) -> "tuple[list[dict], bool]":
        """Same files as list(), plus whether max_depth cut the walk short — used by
        ListFilesTool so the model is told when the listing may be incomplete, rather than being
        left to conclude an under-scanned workspace is an empty one (finding 6)."""
        return self._scan(max_depth)


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
        files, truncated = self.ws.list_status()
        note = ("\n(Note: the workspace tree is deeper than this listing scans — some deeply "
               "nested files may be missing above.)" if truncated else "")
        if not files:
            return "Your workspace is empty." + note
        return ("Files in your workspace:\n" + "\n".join(
            f"  {f['name']}  ({f['size']} bytes)" for f in files) + note)


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
