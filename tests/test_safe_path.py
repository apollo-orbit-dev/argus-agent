"""safe_path lets the workspace be a TREE while still confining everything under root.

The old safe_name() flattened every name to a basename, so `reports/july.md` and `july.md` were
the same file. A container mounts this directory as the agent's home, so it has to be a real tree —
but every containment property of the flat version must survive.
"""
import os

import pytest

from engine.tools.files import FileWorkspace, safe_path


def test_allows_a_subdirectory(tmp_path):
    p = safe_path(str(tmp_path), "reports/july.md")
    assert p == os.path.join(os.path.realpath(str(tmp_path)), "reports", "july.md")


def test_allows_a_plain_name(tmp_path):
    p = safe_path(str(tmp_path), "notes.txt")
    assert p == os.path.join(os.path.realpath(str(tmp_path)), "notes.txt")


@pytest.mark.parametrize("bad", [
    "../outside.txt",
    "reports/../../outside.txt",
    "/etc/passwd",
    "",
    "   ",
    "/",
    "..",
])
def test_rejects_traversal_and_absolute_paths(tmp_path, bad):
    with pytest.raises(ValueError):
        safe_path(str(tmp_path), bad)


def test_rejects_a_symlink_that_escapes(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "ws"
    root.mkdir()
    os.symlink(str(outside), str(root / "escape"))
    with pytest.raises(ValueError):
        safe_path(str(root), "escape/secret.txt")


def test_sanitises_each_component(tmp_path):
    p = safe_path(str(tmp_path), "we;ird/na$me.txt")
    assert p.endswith(os.path.join("we_ird", "na_me.txt"))


def test_write_and_read_round_trip_in_a_subdirectory(tmp_path):
    ws = FileWorkspace(str(tmp_path))
    ws.write_text("reports/july.md", "hello")
    assert ws.read_text("reports/july.md") == "hello"
    assert ws.exists("reports/july.md")


def test_list_is_recursive_and_returns_relative_paths(tmp_path):
    ws = FileWorkspace(str(tmp_path))
    ws.write_text("top.txt", "a")
    ws.write_text("reports/july.md", "b")
    ws.write_text("reports/q3/summary.md", "c")
    names = {f["name"] for f in ws.list()}
    assert names == {"top.txt", "reports/july.md", "reports/q3/summary.md"}


def test_list_respects_a_depth_cap(tmp_path):
    ws = FileWorkspace(str(tmp_path))
    ws.write_text("a/b/c/d/e/deep.txt", "x")
    assert ws.list(max_depth=2) == []


def test_delete_works_in_a_subdirectory(tmp_path):
    ws = FileWorkspace(str(tmp_path))
    ws.write_text("reports/july.md", "hello")
    assert ws.delete("reports/july.md") is True
    assert ws.exists("reports/july.md") is False
