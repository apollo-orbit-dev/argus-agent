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


def test_rejects_a_symlinked_file_that_escapes(tmp_path):
    """Not just symlinked directories: a leaf name that is itself a symlink to a file outside
    root must be rejected too — the realpath check has to catch both link kinds."""
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("outside secret")
    root = tmp_path / "ws"
    root.mkdir()
    os.symlink(str(outside_file), str(root / "link.txt"))
    with pytest.raises(ValueError):
        safe_path(str(root), "link.txt")


def test_rejects_a_prefix_collision_sibling(tmp_path):
    """A root of '.../ws' must not accept a resolved path that merely starts with the same
    string, like '.../wsx' — a bare `full.startswith(root_real)` (no separator) would wrongly
    allow this; the check must require root_real + os.sep (or an exact match). Reached via a
    symlink (not a literal '..' component) so this exercises the resolved-path boundary check
    itself, not the separate raw '..' rejection."""
    root = tmp_path / "ws"
    root.mkdir()
    sibling = tmp_path / "wsx"
    sibling.mkdir()
    (sibling / "secret.txt").write_text("not yours")
    os.symlink(str(sibling), str(root / "escape"))
    root_real = os.path.realpath(str(root))
    full = os.path.realpath(str(sibling / "secret.txt"))
    assert full.startswith(root_real)                       # the naive check would let this through
    assert not (full == root_real or full.startswith(root_real + os.sep))  # the real check rejects it
    with pytest.raises(ValueError):
        safe_path(str(root), "escape/secret.txt")


def test_normalises_backslashes_to_forward_slashes(tmp_path):
    p = safe_path(str(tmp_path), "reports\\july.md")
    assert p == os.path.join(os.path.realpath(str(tmp_path)), "reports", "july.md")


def test_allows_a_leading_dot_in_a_component(tmp_path):
    p = safe_path(str(tmp_path), ".bashrc")
    assert p == os.path.join(os.path.realpath(str(tmp_path)), ".bashrc")
    p = safe_path(str(tmp_path), ".config/settings.json")
    assert p == os.path.join(os.path.realpath(str(tmp_path)), ".config", "settings.json")


def test_still_rejects_dotdot_after_allowing_leading_dots(tmp_path):
    with pytest.raises(ValueError):
        safe_path(str(tmp_path), "..")
    with pytest.raises(ValueError):
        safe_path(str(tmp_path), "../escape.txt")
    with pytest.raises(ValueError):
        safe_path(str(tmp_path), ".config/../../escape.txt")


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
