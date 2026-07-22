"""File workspace: path-safety + read/write/list/delete + SSRF-guarded download."""
import asyncio
import os
import types

from engine.tools.files import (DeleteFileTool, DownloadFileTool, FileWorkspace,
                                 ListFilesTool, ReadFileTool, WriteFileTool, safe_name)


def test_safe_name_blocks_traversal():
    assert safe_name("../../etc/passwd") == "passwd"
    assert safe_name("/abs/path/report.md") == "report.md"
    assert safe_name("a/b/c.txt") == "c.txt"
    assert safe_name("  ..  ") == ""


def test_workspace_roundtrip(tmp_path):
    ws = FileWorkspace(str(tmp_path / "ws"))
    ws.write_text("notes.md", "hello world")
    assert ws.exists("notes.md")
    assert ws.read_text("notes.md") == "hello world"
    files = ws.list()
    assert len(files) == 1 and files[0]["name"] == "notes.md"
    assert ws.delete("notes.md") is True
    assert ws.list() == []


def test_write_read_traversal_safe(tmp_path):
    ws = FileWorkspace(str(tmp_path / "ws"))
    wt = WriteFileTool(ws)
    out = asyncio.run(wt.run(wt.Params(name="../evil.txt", content="x")))
    # safe_path rejects traversal outright now (it no longer silently flattens it to a
    # basename inside the workspace) — nothing is written, inside or outside.
    assert "error" in out.lower() and "traversal" in out.lower()
    assert not ws.exists("evil.txt")
    assert not (tmp_path / "evil.txt").exists()


def test_write_text_refuses_to_follow_a_symlink_planted_at_the_leaf(tmp_path, monkeypatch):
    """TOCTOU: safe_path() resolves and validates the path, but a symlink planted at that exact
    leaf AFTER the check and BEFORE the open() would, without O_NOFOLLOW, be silently followed —
    letting a write land outside the workspace.

    Planting the symlink before calling write_text (as opposed to during the race window) would
    be caught by safe_path()'s own pre-existing-symlink check and never reach _open_no_follow at
    all — that would test something else entirely and wouldn't catch a regression that dropped
    O_NOFOLLOW. So here safe_path is wrapped to plant the symlink itself, immediately after it
    resolves and validates the path but before it returns — i.e. after validation, before open(),
    exactly the window _open_no_follow exists to close."""
    import engine.tools.files as files_mod

    ws = FileWorkspace(str(tmp_path / "ws"))
    os.makedirs(ws.root, exist_ok=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    assert not str(outside).startswith(os.path.realpath(ws.root) + os.sep)  # genuinely outside

    real_safe_path = files_mod.safe_path

    def racy_safe_path(root, name):
        p = real_safe_path(root, name)      # validation happens here — no symlink exists yet
        os.symlink(str(outside), p)         # ...the race: a symlink appears right after
        return p

    monkeypatch.setattr(files_mod, "safe_path", racy_safe_path)
    try:
        ws.write_text("evil.txt", "PWNED")
        assert False, "write_text must not follow a symlink planted at the leaf"
    except ValueError:
        pass
    assert outside.read_text() == "original"       # the outside file was never touched
    leaf = os.path.join(ws.root, "evil.txt")
    assert os.path.islink(leaf)                    # the planted symlink is still there...
    os.remove(leaf)                                # ...clean it up ourselves (tmp_path handles the rest)


def test_save_bytes_refuses_to_follow_a_symlink_planted_at_the_leaf(tmp_path, monkeypatch):
    """save_bytes twin of the write_text TOCTOU test above — see its docstring for why the
    symlink must be planted mid-race (via a wrapped safe_path) rather than before the call."""
    import engine.tools.files as files_mod

    ws = FileWorkspace(str(tmp_path / "ws"))
    os.makedirs(ws.root, exist_ok=True)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"original")
    assert not str(outside).startswith(os.path.realpath(ws.root) + os.sep)  # genuinely outside

    real_safe_path = files_mod.safe_path

    def racy_safe_path(root, name):
        p = real_safe_path(root, name)
        os.symlink(str(outside), p)
        return p

    monkeypatch.setattr(files_mod, "safe_path", racy_safe_path)
    try:
        ws.save_bytes("evil.bin", b"PWNED")
        assert False, "save_bytes must not follow a symlink planted at the leaf"
    except ValueError:
        pass
    assert outside.read_bytes() == b"original"
    leaf = os.path.join(ws.root, "evil.bin")
    assert os.path.islink(leaf)
    os.remove(leaf)


def test_tools_read_list_delete(tmp_path):
    ws = FileWorkspace(str(tmp_path / "ws"))
    ws.write_text("a.txt", "AAA")
    assert "AAA" in asyncio.run(ReadFileTool(ws).run(ReadFileTool.Params(name="a.txt")))
    assert "a.txt" in asyncio.run(ListFilesTool(ws).run(ListFilesTool.Params()))
    assert "no file" in asyncio.run(ReadFileTool(ws).run(ReadFileTool.Params(name="missing"))).lower()
    assert "deleted" in asyncio.run(DeleteFileTool(ws).run(DeleteFileTool.Params(name="a.txt")))


# ---- download_file (SSRF-guarded) ----

async def test_download_infers_extension_from_content_type(tmp_path, monkeypatch):
    ws = FileWorkspace(str(tmp_path / "ws"))

    async def fake_fetch(url, **kw):
        return types.SimpleNamespace(status_code=200,
                                     headers={"content-type": "application/pdf"},
                                     content=b"%PDF-1.4 fake pdf bytes")
    monkeypatch.setattr("engine.tools.net_guard.safe_fetch", fake_fetch)
    t = DownloadFileTool(ws)
    out = await t.run(t.Params(url="https://example.com/report"))    # no extension in URL
    assert "saved" in out.lower()
    assert ws.exists("report.pdf")                                   # .pdf inferred from content-type


async def test_download_uses_url_basename(tmp_path, monkeypatch):
    ws = FileWorkspace(str(tmp_path / "ws"))

    async def fake_fetch(url, **kw):
        return types.SimpleNamespace(status_code=200, headers={"content-type": "text/csv"},
                                     content=b"a,b\n1,2\n")
    monkeypatch.setattr("engine.tools.net_guard.safe_fetch", fake_fetch)
    out = await DownloadFileTool(ws).run(DownloadFileTool.Params(url="https://x.com/data/nums.csv"))
    assert ws.exists("nums.csv") and "saved" in out.lower()


async def test_download_blocks_internal_url(tmp_path, monkeypatch):
    ws = FileWorkspace(str(tmp_path / "ws"))
    from engine.tools.net_guard import BlockedURLError

    async def blocked(url, **kw):
        raise BlockedURLError(f"blocked non-public URL: {url}")
    monkeypatch.setattr("engine.tools.net_guard.safe_fetch", blocked)
    out = await DownloadFileTool(ws).run(
        DownloadFileTool.Params(url="http://169.254.169.254/latest/meta-data/"))
    assert "blocked" in out.lower() or "only public" in out.lower()
    assert ws.list() == []                                           # nothing saved


async def test_download_rejects_traversal_name_cleanly(tmp_path, monkeypatch):
    """safe_path raises ValueError on a bad name (traversal, absolute path, ...) — download_file
    must turn that into a clean '<tool> error: ...' string, not let it propagate out of run()."""
    ws = FileWorkspace(str(tmp_path / "ws"))

    async def fake_fetch(url, **kw):
        return types.SimpleNamespace(status_code=200, headers={"content-type": "text/plain"},
                                     content=b"pwned")
    monkeypatch.setattr("engine.tools.net_guard.safe_fetch", fake_fetch)
    out = await DownloadFileTool(ws).run(
        DownloadFileTool.Params(url="https://x.com/report.txt", name="../evil.txt"))
    assert out.startswith("download_file error:")
    assert ws.list() == []                                            # nothing saved
    assert not (tmp_path / "evil.txt").exists()                       # nothing escaped the workspace


async def test_download_http_error(tmp_path, monkeypatch):
    ws = FileWorkspace(str(tmp_path / "ws"))

    async def fake_fetch(url, **kw):
        return types.SimpleNamespace(status_code=404, headers={}, content=b"nope")
    monkeypatch.setattr("engine.tools.net_guard.safe_fetch", fake_fetch)
    out = await DownloadFileTool(ws).run(DownloadFileTool.Params(url="https://x.com/missing.pdf"))
    assert "404" in out and ws.list() == []
