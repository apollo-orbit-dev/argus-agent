"""engine/updater.py — the self-update engine.

The git-level tests build REAL temporary repositories (a working repo used as `origin` plus a
clone), so checkout/tag/status behaviour is the real thing rather than a mock's opinion of it. Only
pip is stubbed, through the `_stream` seam — nothing here touches the network or the developer's
own install.
"""
from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import pytest

from engine import updater

REPO_ROOT = Path(__file__).resolve().parents[1]

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_TERMINAL_PROMPT": "0",
}

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs a real git binary")


def _git(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=GIT_ENV)
    assert p.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {p.stderr or p.stdout}"
    return p.stdout.strip()


def _changelog(versions: list[str]) -> str:
    """Newest-first, same shape as the real CHANGELOG.md."""
    out = ["# Changelog", ""]
    for v in reversed(versions):
        out += [f"## {v}", "", f"Notes for {v}.", ""]
    return "\n".join(out)


def _make_origin(tmp_path: Path, versions=("0.1.0", "0.2.0")) -> Path:
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    # The REAL .gitignore, so "user state is never touched" is proven against the shipped rules and
    # not against a convenient copy.
    shutil.copy(REPO_ROOT / ".gitignore", origin / ".gitignore")
    seen: list[str] = []
    for v in versions:
        seen.append(v)
        (origin / "pyproject.toml").write_text(f'[project]\nname = "argus"\nversion = "{v}"\n')
        (origin / "CHANGELOG.md").write_text(_changelog(seen))
        (origin / "main.py").write_text(f"# argus {v}\n")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-m", f"release {v}")
        _git(origin, "tag", f"v{v}")
    return origin


def _make_clone(tmp_path: Path, origin: Path, at: str = "v0.1.0") -> Path:
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "--quiet", str(origin), str(clone))
    if at:
        _git(clone, "-c", "advice.detachedHead=false", "checkout", "-q", at)
    (clone / ".venv").mkdir(exist_ok=True)
    return clone


def _pin(monkeypatch, clone: Path) -> None:
    """Make the module see this temp clone as "the install we are running from": the venv check and
    the running-version lookup both otherwise point at the developer's real checkout."""
    monkeypatch.setattr(updater, "_running_prefix", lambda: (clone / ".venv").resolve())

    def _version():
        with open(clone / "pyproject.toml", "rb") as f:
            return tomllib.load(f)["project"]["version"]
    monkeypatch.setattr(updater, "get_version", _version)
    # The developer venv this suite runs in has no `pip` module, which is a real (and correctly
    # reported) preflight blocker — but not the one any of these tests is about. Pin it healthy;
    # test_preflight_no_pip turns it back off.
    monkeypatch.setattr(updater, "_pip_available", lambda: True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("needs a real git binary")
    origin = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, origin)
    _pin(monkeypatch, clone)
    return clone


def _stub_pip(monkeypatch, rc: int = 0, calls: list | None = None, fail_first_only: bool = False):
    """Route pip through the `_stream` seam (never a real install) while letting git run for real.

    `fail_first_only` models the realistic failure: the forward install breaks, the rollback
    install then succeeds."""
    real = updater._stream
    seen = {"n": 0}

    def fake(argv, cwd=updater.ROOT, timeout=updater.PIP_TIMEOUT, emit=None):
        if "pip" in argv:
            seen["n"] += 1
            code = rc if (not fail_first_only or seen["n"] == 1) else 0
            if calls is not None:
                calls.append(list(argv))
            (emit or (lambda _l: None))(f"[stub pip] exit {code}")
            return code
        return real(argv, cwd, timeout, emit)
    monkeypatch.setattr(updater, "_stream", fake)


def _codes(pf: dict) -> list[str]:
    return [b["code"] for b in pf["blockers"]]


def _msg(pf: dict, code: str) -> str:
    return next(b["message"] for b in pf["blockers"] if b["code"] == code)


# --------------------------------------------------------------------------
# anti-drift: the installer and the updater must agree on "newest release"
# --------------------------------------------------------------------------
def test_tag_expression_matches_install_sh():
    """install.sh pins a fresh clone to the newest tag; the updater moves an existing clone to the
    newest tag. If those two expressions ever diverge, an update becomes a silent downgrade (or a
    no-op) for everyone who used the installer. This test is the guard."""
    text = (REPO_ROOT / "install.sh").read_text(encoding="utf-8")
    m = re.search(r'git -C "\$DIR_NAME"\s+(tag[^|]*?)\s*\|\s*head', text)
    assert m, "could not find the newest-tag expression in install.sh"
    assert shlex.split(m.group(1)) == updater.NEWEST_TAG_ARGV[1:], (
        f"install.sh uses {m.group(1)!r} but engine/updater.NEWEST_TAG_ARGV is "
        f"{updater.NEWEST_TAG_ARGV!r} — keep them identical")


@needs_git
def test_newest_tag_sorts_by_version_not_lexically(tmp_path):
    origin = _make_origin(tmp_path, versions=("0.9.0", "0.10.0"))
    clone = _make_clone(tmp_path, origin, at="v0.9.0")
    # Lexical sort would answer v0.9.0; version sort must answer v0.10.0.
    assert updater.newest_tag(clone) == "v0.10.0"


# --------------------------------------------------------------------------
# preflight — each refusal asserts the CODE and a distinctive slice of the MESSAGE, so the wording
# cannot silently regress to a generic "update failed".
# --------------------------------------------------------------------------
@needs_git
def test_preflight_clean_checkout_offers_the_update(repo):
    pf = updater.preflight(repo)
    assert pf["current"] == "0.1.0" and pf["target"] == "v0.2.0"
    assert pf["update_available"] is True and pf["ok"] is True
    assert [b for b in pf["blockers"] if b["severity"] == "error"] == []


def test_preflight_not_a_checkout(tmp_path, monkeypatch):
    """Refusing is CORRECT — an install provisioned by copying files has no releases to move
    between. The message therefore has to name that case and say where the update does happen,
    instead of only telling someone to re-run an installer they may not have."""
    monkeypatch.setattr(updater, "_running_prefix", lambda: (tmp_path / ".venv").resolve())
    pf = updater.preflight(tmp_path)
    assert _codes(pf) == ["not_a_checkout"]
    m = _msg(pf, "not_a_checkout")
    assert "not a git checkout" in m
    assert "copying files" in m and "deploy it again" in m
    assert "install.sh" in m
    assert pf["update_available"] is False


@needs_git
def test_preflight_no_origin(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, origin)
    _git(clone, "remote", "remove", "origin")
    _pin(monkeypatch, clone)
    pf = updater.preflight(clone)
    assert "no_origin" in _codes(pf)
    assert "no 'origin' remote" in _msg(pf, "no_origin")


@needs_git
def test_preflight_dirty_tree_names_the_files(repo):
    (repo / "main.py").write_text("# locally edited\n")
    pf = updater.preflight(repo)
    assert "dirty_tree" in _codes(pf)
    m = _msg(pf, "dirty_tree")
    assert "main.py" in m, "the refusal must name the file that is in the way"
    assert "commit, stash or discard" in m
    assert pf["update_available"] is False


@needs_git
def test_preflight_dirty_tree_ignores_user_state(repo):
    """.env / *.db / model_presets.json / workspaces/ are gitignored, so they must never be
    mistaken for local edits that block an update."""
    (repo / ".env").write_text("ADMIN_TOKEN=x\n")
    (repo / "memory.db").write_bytes(b"sqlite")
    (repo / "model_presets.json").write_text("{}")
    (repo / "workspaces").mkdir()
    (repo / "workspaces" / "f.txt").write_text("mine")
    (repo / "SOUL.md").write_text("persona")
    pf = updater.preflight(repo)
    assert "dirty_tree" not in _codes(pf)
    assert pf["update_available"] is True


@needs_git
def test_preflight_ignores_the_wal_sidecars_of_a_live_instance(repo):
    """THE regression that made this button refuse itself on every running instance.

    sessions.db is opened with `PRAGMA journal_mode=WAL` (engine/state.py), so a live Argus always
    has `sessions.db-wal` and `sessions.db-shm` sitting in the repo root. `*.db` did not cover them,
    so `git status --porcelain` listed them as untracked and dirty_tree fired forever."""
    (repo / "sessions.db").write_bytes(b"SQLite format 3\x00")
    (repo / "sessions.db-wal").write_bytes(b"\x00" * 32)
    (repo / "sessions.db-shm").write_bytes(b"\x00" * 32)
    # Not just "the updater tolerates them": they must be gitignored, so they never show up as a
    # change to anything, in any tool. This is what asserts the .gitignore half of the fix.
    assert _git(repo, "status", "--porcelain") == "", (
        "the WAL sidecars of a live instance must be gitignored")
    pf = updater.preflight(repo)
    assert "dirty_tree" not in _codes(pf)
    assert pf["update_available"] is True


@needs_git
def test_preflight_ignores_untracked_files(repo):
    """`git checkout <tag>` does not touch untracked files, so they cannot be "overwritten" by an
    update and must not block one. Only tracked modifications answer the question this blocker
    asks. (This is the `--untracked-files=no` half of the fix.)"""
    (repo / "notes-to-self.txt").write_text("scratch\n")
    (repo / "somedir").mkdir()
    (repo / "somedir" / "thing.py").write_text("mine\n")
    assert _git(repo, "status", "--porcelain") != "", "these ARE untracked (not gitignored)"
    pf = updater.preflight(repo)
    assert "dirty_tree" not in _codes(pf)
    assert pf["update_available"] is True


@needs_git
def test_preflight_wrong_venv(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, origin)
    _pin(monkeypatch, clone)
    monkeypatch.setattr(updater, "_running_prefix", lambda: (tmp_path / "someother" / ".venv"))
    pf = updater.preflight(clone)
    assert "wrong_venv" in _codes(pf)
    m = _msg(pf, "wrong_venv")
    assert "different environment than the one that gets restarted" in m
    assert str((clone / ".venv").resolve()) in m


@needs_git
def test_preflight_no_network(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, origin)
    _git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    _pin(monkeypatch, clone)
    pf = updater.preflight(clone)
    assert "no_network" in _codes(pf)
    assert "Could not reach origin" in _msg(pf, "no_network")


@needs_git
def test_preflight_no_tags(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "pyproject.toml").write_text('[project]\nname = "argus"\nversion = "0.1.0"\n')
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "no releases yet")
    clone = _make_clone(tmp_path, origin, at="")
    _pin(monkeypatch, clone)
    pf = updater.preflight(clone)
    assert "no_tags" in _codes(pf)
    assert "never the main branch" in _msg(pf, "no_tags")


@needs_git
def test_preflight_up_to_date_is_info_not_error(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, origin, at="v0.2.0")
    _pin(monkeypatch, clone)
    pf = updater.preflight(clone)
    assert "up_to_date" in _codes(pf)
    assert next(b for b in pf["blockers"] if b["code"] == "up_to_date")["severity"] == "info"
    assert "Already up to date" in _msg(pf, "up_to_date")
    assert pf["update_available"] is False


@needs_git
def test_preflight_ahead_of_tags_is_info(repo, monkeypatch):
    monkeypatch.setattr(updater, "get_version", lambda: "9.9.9")
    pf = updater.preflight(repo)
    assert "ahead_of_tags" in _codes(pf)
    assert next(b for b in pf["blockers"] if b["code"] == "ahead_of_tags")["severity"] == "info"
    assert "ahead of every published release" in _msg(pf, "ahead_of_tags")
    assert pf["update_available"] is False


@needs_git
def test_preflight_target_missing_hints_at_a_shallow_clone(repo, monkeypatch):
    monkeypatch.setattr(updater, "_resolves", lambda ref, clone_dir=updater.ROOT: False)
    pf = updater.preflight(repo)
    assert "target_missing" in _codes(pf)
    assert "shallow clone" in _msg(pf, "target_missing")
    assert "--unshallow" in _msg(pf, "target_missing")


@needs_git
def test_preflight_no_pip(repo, monkeypatch):
    monkeypatch.setattr(updater, "_pip_available", lambda: False)
    pf = updater.preflight(repo)
    assert "no_pip" in _codes(pf)
    assert "ensurepip" in _msg(pf, "no_pip")


# --------------------------------------------------------------------------
# changelog
# --------------------------------------------------------------------------
@needs_git
def test_changelog_is_read_from_the_target_tag(repo):
    # The 0.2.0 section does not exist in the running (v0.1.0) checkout at all.
    assert "## 0.2.0" not in (repo / "CHANGELOG.md").read_text()
    text, truncated, note = updater.changelog_between(repo, "v0.2.0", "0.1.0")
    assert text and "## 0.2.0" in text and note is None and truncated is False
    assert "## 0.1.0" not in text, "only sections NEWER than the running version belong in a preview"


@needs_git
def test_changelog_spans_every_intervening_version(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path, versions=("0.1.0", "0.2.0", "0.3.0"))
    clone = _make_clone(tmp_path, origin, at="v0.1.0")
    _pin(monkeypatch, clone)
    text, _, _ = updater.changelog_between(clone, "v0.3.0", "0.1.0")
    assert "## 0.3.0" in text and "## 0.2.0" in text and "## 0.1.0" not in text


@needs_git
def test_changelog_missing_file_returns_a_note(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    (origin / "pyproject.toml").write_text('[project]\nname = "argus"\nversion = "0.1.0"\n')
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "a")
    _git(origin, "tag", "v0.1.0")
    clone = _make_clone(tmp_path, origin, at="v0.1.0")
    text, truncated, note = updater.changelog_between(clone, "v0.1.0", "0.0.9")
    assert text is None and truncated is False
    assert note and "no CHANGELOG.md" in note


@needs_git
def test_changelog_is_capped(repo, monkeypatch):
    monkeypatch.setattr(updater, "CHANGELOG_CAP", 10)
    text, truncated, _ = updater.changelog_between(repo, "v0.2.0", "0.1.0")
    assert truncated is True and len(text) == 10


# --------------------------------------------------------------------------
# apply — MUST-HAVE 4 and the automatic rollback
# --------------------------------------------------------------------------
@needs_git
def test_update_never_touches_user_state(repo, monkeypatch):
    """The guarantee is structural (every one of these paths is in .gitignore, and `git checkout`
    does not touch ignored or untracked files) — but it is the promise printed on the button, so it
    gets an explicit test against the REAL .gitignore."""
    _stub_pip(monkeypatch)
    monkeypatch.setattr(updater, "restart_strategy",
                        lambda clone_dir=updater.ROOT: {"strategy": "exec", "unit": None,
                                                        "instruction": "x"})
    state = {
        ".env": b"ADMIN_TOKEN=hunter2\nMODEL_NAME=local\n",
        "memory.db": b"SQLite format 3\x00fake",
        "tables.db": b"SQLite format 3\x00fake2",
        "model_presets.json": b'{"connections": [{"api_key": "sk-secret"}]}',
        "SOUL.md": b"# who I am\n",
        "system_prompt.txt": b"you are argus\n",
        "trusted_tools.json": b"{}",
        "scheduled_jobs.json": b"[]",
        "workspaces/dashboard/notes.txt": b"user's file\n",
        "created_tools/my_tool.py": b"def run():\n    return 1\n",
    }
    for rel, data in state.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    res = updater.apply_update("v0.2.0", repo)
    assert res["ok"] is True and res["state"] == "applied"
    assert (repo / "pyproject.toml").read_text().count('version = "0.2.0"') == 1  # really moved

    for rel, data in state.items():
        assert (repo / rel).read_bytes() == data, f"{rel} was modified by the update"
    assert _git(repo, "status", "--porcelain") == "", (
        "the update left the tree dirty — user state must be gitignored, including "
        ".argus-update.json")


@needs_git
def test_apply_records_state_before_checkout(repo, monkeypatch):
    """The previous ref must be on disk before anything is touched, or a crash mid-update leaves no
    way back."""
    seen: dict = {}
    real_stream = updater._stream

    def fake(argv, cwd=updater.ROOT, timeout=updater.PIP_TIMEOUT, emit=None):
        if "checkout" in argv and "state_at_checkout" not in seen:
            seen["state_at_checkout"] = updater.read_state(repo)
        if "pip" in argv:
            return 0
        return real_stream(argv, cwd, timeout, emit)
    monkeypatch.setattr(updater, "_stream", fake)
    monkeypatch.setattr(updater, "restart_strategy",
                        lambda clone_dir=updater.ROOT: {"strategy": "exec", "unit": None,
                                                        "instruction": "x"})
    updater.apply_update("v0.2.0", repo)
    at_checkout = seen["state_at_checkout"]
    assert at_checkout["state"] == "applying"
    assert at_checkout["from_tag"] == "v0.1.0"
    assert at_checkout["to_tag"] == "v0.2.0"


@needs_git
def test_apply_rolls_back_when_pip_fails(repo, monkeypatch):
    before = _git(repo, "rev-parse", "HEAD")
    calls: list = []
    _stub_pip(monkeypatch, rc=1, calls=calls, fail_first_only=True)
    events: list = []
    res = updater.apply_update("v0.2.0", repo, emit=events.append)

    assert res["ok"] is False
    assert res["state"] == "reverted"
    assert res["failed_step"] == "pip"
    assert res["restart"] is None, "a failed update must not offer a restart"
    assert _git(repo, "rev-parse", "HEAD") == before, "HEAD must be back at the previous ref"
    assert (repo / "pyproject.toml").read_text().count('version = "0.1.0"') == 1
    assert updater.read_state(repo)["state"] == "reverted"
    assert len(calls) == 2, "pip runs once forward and once for the rollback"
    steps = [e["step"] for e in events if e["type"] == "step"]
    assert steps == ["record", "checkout", "pip", "rollback"]
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1 and done[0]["ok"] is False and done[0]["restart"] is None
    assert "git checkout" in done[0]["revert_command"]


@needs_git
def test_apply_rollback_failure_reports_needs_manual_with_commands(repo, monkeypatch):
    """Only when the rollback ITSELF fails do we fall back to printing commands."""
    real_stream = updater._stream

    def fake(argv, cwd=updater.ROOT, timeout=updater.PIP_TIMEOUT, emit=None):
        if "pip" in argv:
            return 1
        if "checkout" in argv and argv[-1] != "v0.2.0":
            return 1                                    # the rollback checkout fails too
        return real_stream(argv, cwd, timeout, emit)
    monkeypatch.setattr(updater, "_stream", fake)
    res = updater.apply_update("v0.2.0", repo)
    assert res["state"] == "needs_manual" and res["ok"] is False
    assert res["commands"] and "git checkout v0.1.0" in res["commands"][0]


@needs_git
def test_apply_rolls_back_when_verification_fails(repo, monkeypatch):
    before = _git(repo, "rev-parse", "HEAD")
    _stub_pip(monkeypatch)
    monkeypatch.setattr(updater, "_verify", lambda clone_dir, target: (False, "version mismatch"))
    res = updater.apply_update("v0.2.0", repo)
    assert res["state"] == "reverted" and res["failed_step"] == "verify"
    assert _git(repo, "rev-parse", "HEAD") == before


@needs_git
def test_apply_detects_a_partial_checkout_and_rolls_back(tmp_path, monkeypatch):
    """`git checkout` RETURNS 0 on a partial checkout.

    When it cannot write some paths — a directory left read-only by a past run, ENOSPC, a file held
    open — it prints "unable to create file", moves HEAD to the target anyway, and exits 0. HEAD and
    the root pyproject version then both check out while the tree is a MIX of two releases, so
    without a clean-tree assertion the updater pip-installs and restarts onto a Frankenstein tree.
    """
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root writes through the read-only directory this test relies on")
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    shutil.copy(REPO_ROOT / ".gitignore", origin / ".gitignore")
    (origin / "engine").mkdir()
    for v in ("0.1.0", "0.2.0"):
        (origin / "pyproject.toml").write_text(f'[project]\nname = "argus"\nversion = "{v}"\n')
        (origin / "engine" / "mod.py").write_text(f"VERSION = {v!r}\n")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-m", f"release {v}")
        _git(origin, "tag", f"v{v}")
    clone = _make_clone(tmp_path, origin, at="v0.1.0")
    _pin(monkeypatch, clone)
    before = _git(clone, "rev-parse", "HEAD")

    # engine/ cannot be written, so `git checkout v0.2.0` updates pyproject.toml and the index but
    # leaves engine/mod.py at the old release. The (stubbed) pip step then puts the permissions back,
    # standing in for the transient condition clearing — the point under test is that VERIFICATION
    # notices the mixed tree, not what caused it.
    (clone / "engine").chmod(0o555)
    real_stream = updater._stream
    pip_calls: list = []

    def fake(argv, cwd=updater.ROOT, timeout=updater.PIP_TIMEOUT, emit=None):
        if "pip" in argv:
            pip_calls.append(list(argv))
            (clone / "engine").chmod(0o755)
            return 0
        return real_stream(argv, cwd, timeout, emit)
    monkeypatch.setattr(updater, "_stream", fake)
    try:
        res = updater.apply_update("v0.2.0", clone)
    finally:
        (clone / "engine").chmod(0o755)

    assert _git(clone, "rev-parse", "v0.2.0^{commit}") == _git(origin, "rev-parse", "v0.2.0^{commit}")
    assert res["ok"] is False, "a half-written tree must never be reported as a successful update"
    assert res["failed_step"] == "verify"
    assert "incomplete" in res["detail"] and "engine/mod.py" in res["detail"]
    assert res["state"] == "reverted" and res["restart"] is None
    assert _git(clone, "rev-parse", "HEAD") == before
    assert (clone / "engine" / "mod.py").read_text() == "VERSION = '0.1.0'\n"
    assert len(pip_calls) == 2, "pip ran forward and then again for the rollback"


def _two_release_clone(tmp_path, monkeypatch) -> Path:
    """A clone at v0.1.0 whose SETTINGS.md is IDENTICAL in both releases — so a checkout of v0.2.0
    carries a mid-update edit of it straight across and exits 0."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    shutil.copy(REPO_ROOT / ".gitignore", origin / ".gitignore")
    for v in ("0.1.0", "0.2.0"):
        (origin / "pyproject.toml").write_text(f'[project]\nname = "argus"\nversion = "{v}"\n')
        (origin / "SETTINGS.md").write_text("shipped default\n")     # unchanged between releases
        _git(origin, "add", "-A")
        _git(origin, "commit", "-m", f"release {v}")
        _git(origin, "tag", f"v{v}")
    clone = _make_clone(tmp_path, origin, at="v0.1.0")
    _pin(monkeypatch, clone)
    return clone


@needs_git
def test_rollback_stashes_a_file_the_maintainer_edited_during_the_update(tmp_path, monkeypatch):
    """THE data-loss sequence, exactly:

      1. preflight passes on a clean tree.
      2. during the multi-minute fetch/checkout/pip window the maintainer edits a tracked file that
         is IDENTICAL in both refs — so `git checkout <target>` CARRIES THE EDIT OVER and exits 0.
      3. verification then sees a dirty tree and cannot tell that edit from a file git failed to
         write, so it rolls back.
      4. the rollback puts the release back over it.

    The edit must survive. `_verify` cannot diagnose the dirt, so the rollback must not destroy
    either explanation of it: it hands the tree to `git stash` first and says what the stash is
    called.
    """
    clone = _two_release_clone(tmp_path, monkeypatch)
    mine = "the note I was in the middle of writing\n"
    real_stream = updater._stream
    pip = {"n": 0}

    def fake(argv, cwd=updater.ROOT, timeout=updater.PIP_TIMEOUT, emit=None):
        if "pip" in argv:
            pip["n"] += 1
            if pip["n"] == 1:
                (clone / "SETTINGS.md").write_text(mine)     # the maintainer, mid-update
            return 0
        return real_stream(argv, cwd, timeout, emit)
    monkeypatch.setattr(updater, "_stream", fake)

    events: list = []
    res = updater.apply_update("v0.2.0", clone, emit=events.append)

    # Refusing is right — the updater genuinely cannot tell this from a half-written checkout.
    assert res["state"] == "reverted" and res["failed_step"] == "verify"
    assert _git(clone, "rev-parse", "HEAD") == _git(clone, "rev-parse", "v0.1.0^{commit}")
    assert (clone / "SETTINGS.md").read_text() == "shipped default\n", "the rollback did run"

    # ...but the maintainer's bytes are in git's own stash, under a name the report gives back.
    name = "argus-update-v0.1.0-v0.2.0"
    assert res["stash"] == name, "the result must name the stash — both UIs render this field"
    assert name in _git(clone, "stash", "list"), "the stash is not discoverable from git stash list"
    assert _git(clone, "show", "stash@{0}:SETTINGS.md") == mine.strip()
    assert name in res["detail"]
    assert any(name in str(e.get("line", "")) for e in events if e["type"] == "log")

    # And it must not misdiagnose whose fault it was: git wrote the file perfectly well.
    assert "git reported success but could not write" not in res["detail"]
    assert "or they were edited while the update was running" in res["detail"], (
        "the report must offer both explanations, because verification cannot tell them apart")


def _rollback_with_a_failing_stash(tmp_path, monkeypatch, fake_run_stash):
    """Drive a real apply_update to the rollback, with `git stash push` failing the way
    `fake_run_stash(real_run, argv, cwd, timeout)` says. Returns (clone, result, checkouts)."""
    clone = _two_release_clone(tmp_path, monkeypatch)
    real_stream, real_run = updater._stream, updater._run
    checkouts: list = []
    pip = {"n": 0}

    def fake_stream(argv, cwd=updater.ROOT, timeout=updater.PIP_TIMEOUT, emit=None):
        if "pip" in argv:
            pip["n"] += 1
            if pip["n"] == 1:
                (clone / "SETTINGS.md").write_text(_MID_UPDATE_EDIT)
            return 0
        if "checkout" in argv:
            checkouts.append(list(argv))
        return real_stream(argv, cwd, timeout, emit)

    def fake_run(argv, cwd=updater.ROOT, timeout=20.0):
        if argv[:3] == ["git", "stash", "push"]:
            return fake_run_stash(real_run, argv, cwd, timeout)
        return real_run(argv, cwd, timeout)
    monkeypatch.setattr(updater, "_stream", fake_stream)
    monkeypatch.setattr(updater, "_run", fake_run)
    return clone, updater.apply_update("v0.2.0", clone), checkouts


_MID_UPDATE_EDIT = "the note I was in the middle of writing\n"


@needs_git
def test_a_rollback_whose_stash_fails_before_saving_anything_changes_nothing_at_all(tmp_path,
                                                                                    monkeypatch):
    """FAIL SAFE — the one thing worse than no rollback is a rollback that checks out over work it
    failed to preserve. When `git stash push` fails having done NOTHING, the checkout must not run:
    HEAD stays where it is, the tree is left alone, and the report says needs_manual with git's own
    words. (The other shape — git fails having already saved and deleted — is the test below.)"""
    def fails_cleanly(real_run, argv, cwd, timeout):
        return 1, "", "fatal: cannot save the current worktree state: Disk quota exceeded"
    clone, res, checkouts = _rollback_with_a_failing_stash(tmp_path, monkeypatch, fails_cleanly)
    at_target = _git(clone, "rev-parse", "v0.2.0^{commit}")

    assert res["state"] == "needs_manual" and res["failed_step"] == "verify"
    assert res["stash"] is None, "nothing was created, so there is no entry to point anyone at"
    assert [c for c in checkouts if "v0.1.0" in c] == [], (
        "the rollback checked out over a tree it had just failed to preserve")
    assert _git(clone, "rev-parse", "HEAD") == at_target, "HEAD was moved after a failed stash"
    assert (clone / "SETTINGS.md").read_text() == _MID_UPDATE_EDIT, "the unsaved edit was destroyed"
    assert "Disk quota exceeded" in res["detail"], "git's own error must be quoted verbatim"
    assert "nothing here has been changed" in res["detail"]
    assert any("git stash push" in c for c in res["commands"]), "no way forward was offered"
    assert _git(clone, "stash", "list") == ""


@needs_git
def test_a_stash_that_fails_AFTER_saving_and_deleting_still_names_what_it_saved(tmp_path,
                                                                                monkeypatch):
    """`git stash push` returning non-zero does NOT mean nothing happened.

    Real git writes the stash, removes the files from disk, and only THEN reports a failure it hit
    while removing one of them — a root-owned leftover from a past sudo, a read-only mount, ENOSPC,
    an NFS silly-rename:

        $ git stash push --include-untracked -m argus-test
        Saved working directory and index state On (no branch): argus-test
        warning: failed to remove locked/thing.txt: Permission denied
        rc=1        <-- non-zero, and SETTINGS.md is already gone from disk

    The fail-safe half is still right — nothing may be checked out. But the report has to stop
    claiming "nothing here has been changed" while a file has vanished, has to NAME the entry (the
    `stash` field is the only thing either UI renders), and must not offer a recovery command that
    pushes a SECOND stash with the same name, which `git stash list` could not then tell apart.

    NOTE THE STUB: it runs the real `git stash push` and then overrides the exit code. The previous
    version of this test returned (1, "", "...") without calling git at all, so the failure it
    simulated changed nothing BY CONSTRUCTION and could not observe the bug.
    """
    def fails_after_doing_the_work(real_run, argv, cwd, timeout):
        real_run(argv, cwd, timeout)             # git really stashes: entry created, file deleted
        return 1, "", "warning: failed to remove locked/thing.txt: Permission denied"
    clone, res, checkouts = _rollback_with_a_failing_stash(tmp_path, monkeypatch,
                                                          fails_after_doing_the_work)
    at_target = _git(clone, "rev-parse", "v0.2.0^{commit}")
    name = "argus-update-v0.1.0-v0.2.0"

    # Still fail-safe: no checkout over a tree we cannot vouch for.
    assert res["state"] == "needs_manual" and res["failed_step"] == "verify"
    assert [c for c in checkouts if "v0.1.0" in c] == []
    assert _git(clone, "rev-parse", "HEAD") == at_target, "HEAD was moved after a failed stash"

    # ...but git DID change the disk, and the report must say where the bytes went.
    assert (clone / "SETTINGS.md").read_text() != _MID_UPDATE_EDIT, (
        "precondition: real git removed the file while failing — otherwise this proves nothing")
    assert res["stash"] == name, "the entry exists and nothing named it — neither UI can show it"
    assert name in _git(clone, "stash", "list")
    assert _git(clone, "show", f"stash@{{0}}:SETTINGS.md") == _MID_UPDATE_EDIT.strip()
    assert "nothing here has been changed" not in res["detail"], (
        "a file is gone from disk — this sentence is false")
    assert "PARTIAL" in res["detail"] and name in res["detail"]
    assert "Permission denied" in res["detail"], "git's own error must still be quoted verbatim"

    # And no duplicate-name push, which would make `git stash list` ambiguous.
    assert not any("stash push" in c for c in res["commands"]), (
        f"offering another push of {name} creates a second entry with an identical name")
    assert any("git stash list" in c for c in res["commands"])


@needs_git
def test_the_offered_recovery_commands_work_for_untracked_content(tmp_path, monkeypatch):
    """`git stash show -p "stash@{0}"` and `git stash pop` — the obvious pair, and what was printed
    — are both wrong for what this mechanism saves.

    `show -p` without `--include-untracked` prints NOTHING, exit 0, for an entry holding only
    untracked files, so a user reasonably concludes their stash is empty. And `pop` FAILS in the
    exact shape this exists for ("<path> already exists, no checkout / could not restore untracked
    files"), because the rollback has since put the release's own copy back at that path. No data is
    lost either way — but the advertised one-command recovery does not work, for the one person it
    was written for."""
    clone = _two_release_clone(tmp_path, monkeypatch)
    (clone / "notes.md").write_text("the untracked notes I keep in here\n")
    _stub_pip(monkeypatch, rc=1, fail_first_only=True)
    events: list = []
    res = updater.apply_update("v0.2.0", clone, emit=events.append)

    assert res["state"] == "reverted" and res["stash"] == "argus-update-v0.1.0-v0.2.0"
    hint = next(ln for ln in (str(e.get("line", "")) for e in events if e["type"] == "log")
                if "Recover it with" in ln)
    assert 'git stash show -p --include-untracked "stash@{0}"' in hint
    assert "git stash pop" not in hint, "pop fails in the shape this mechanism exists for"
    assert 'git checkout "stash@{0}^3" -- <path>' in hint, "the untracked case needs the ^3 form"

    # The evidence, from real git rather than from reasoning about it.
    assert _git(clone, "stash", "show", "-p", "stash@{0}") == "", (
        "precondition: the command that WAS printed prints nothing at all here")
    assert "notes.md" in _git(clone, "stash", "show", "-p", "--include-untracked", "stash@{0}")
    assert _git(clone, "show", "stash@{0}^3:notes.md") == "the untracked notes I keep in here"


@needs_git
def test_a_successful_update_does_not_erase_a_previous_failures_stash_name(repo, monkeypatch):
    """finish() wrote `stash: None` on every path, so a later SUCCESSFUL update overwrote the record
    of where an earlier failed one had put the user's files."""
    updater.write_state(repo, state="needs_manual", stash="argus-update-v0.1.0-v0.2.0")
    _stub_pip(monkeypatch)
    res = updater.apply_update("v0.2.0", repo)
    assert res["ok"] is True and res["stash"] is None, "this run made no stash of its own"
    assert updater.read_state(repo)["stash"] == "argus-update-v0.1.0-v0.2.0", (
        "the only record of where the earlier failure put those files was erased")


@needs_git
def test_rollback_preserves_user_data_at_a_path_the_old_release_shipped(tmp_path, monkeypatch):
    """The shape `--include-untracked` cannot see, and the reason for the second, pathspec'd push.

    A release turns a formerly-SHIPPED file into a gitignored RUNTIME path (routines/daily.json here
    — Argus does exactly this kind of thing). The live instance writes real data there during the
    multi-minute pip window. The update then fails, and `git checkout v0.1.0` writes v0.1.0's own
    shipped copy straight over that data: .gitignore does not defend a path the target commit
    tracks. The first stash push never saw the file, because the file is ignored.

    `git stash push --all -- <the at-risk paths>` saves it. THE PATHSPEC IS WHAT MAKES `--all` SAFE
    — this test asserts that too, because a bare `--all` would strip .env, the databases and .venv/
    out of a live install, which is categorically worse than the bug it fixes.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-b", "main")
    # The REAL rules, and a real transition inside them: this repo's own .gitignore already carries
    # `/routines/`. v0.1.0 is that file WITHOUT the line (it still ships the path), v0.2.0 is it as
    # shipped today (the path has become runtime data).
    shipped_ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "/routines/\n" in shipped_ignore, "the shipped rules changed — pick another at-risk path"
    before_ignore = shipped_ignore.replace("/routines/\n", "")

    # v0.1.0 SHIPS routines/daily.json as a tracked default.
    (origin / ".gitignore").write_text(before_ignore)
    (origin / "pyproject.toml").write_text('[project]\nname = "argus"\nversion = "0.1.0"\n')
    (origin / "routines").mkdir()
    (origin / "routines" / "daily.json").write_text('{"shipped": "default"}\n')
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "release 0.1.0")
    _git(origin, "tag", "v0.1.0")

    # v0.2.0 stops shipping it and makes it a gitignored runtime directory instead.
    (origin / ".gitignore").write_text(shipped_ignore)
    (origin / "pyproject.toml").write_text('[project]\nname = "argus"\nversion = "0.2.0"\n')
    _git(origin, "rm", "-q", "-r", "--cached", "routines")
    shutil.rmtree(origin / "routines")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-m", "release 0.2.0")
    _git(origin, "tag", "v0.2.0")

    clone = _make_clone(tmp_path, origin, at="v0.1.0")
    _pin(monkeypatch, clone)

    # The live install's own state, none of which this may touch.
    (clone / ".env").write_text("ADMIN_TOKEN=hunter2\n")
    (clone / "sessions.db").write_bytes(b"SQLite format 3\x00not really")
    (clone / ".venv" / "pyvenv.cfg").write_text("home = /usr\n")

    mine = '{"my": "routine", "at": "07:00"}\n'
    real_stream = updater._stream
    pip = {"n": 0}

    def fake(argv, cwd=updater.ROOT, timeout=updater.PIP_TIMEOUT, emit=None):
        if "pip" in argv:
            pip["n"] += 1
            if pip["n"] == 1:
                # The running instance, mid-update, writing its runtime data at the new location.
                (clone / "routines").mkdir(exist_ok=True)
                (clone / "routines" / "daily.json").write_text(mine)
                return 1                        # ...and the install fails, so we roll back
            return 0
        return real_stream(argv, cwd, timeout, emit)
    monkeypatch.setattr(updater, "_stream", fake)

    res = updater.apply_update("v0.2.0", clone)

    assert res["state"] == "reverted" and res["failed_step"] == "pip"
    assert _git(clone, "rev-parse", "HEAD") == _git(clone, "rev-parse", "v0.1.0^{commit}")
    # The rollback DID put the release's file back — that is what a checkout of v0.1.0 does.
    assert (clone / "routines" / "daily.json").read_text() == '{"shipped": "default"}\n'
    # ...but the user's bytes are in git's own stash, in an entry the report names.
    ignored_name = "argus-update-v0.1.0-v0.2.0-ignored"
    assert ignored_name in (res["stash"] or ""), (
        "the entry holding the user's runtime data was not named — neither UI can show it")
    assert ignored_name in _git(clone, "stash", "list")
    assert _git(clone, "show", f"stash@{{0}}^3:routines/daily.json") == mine.strip()
    assert "routines/daily.json" in res["detail"], "the at-risk path must be named in the report"

    # THE PATHSPEC. A bare `--all` would have taken all of these.
    assert (clone / ".env").read_text() == "ADMIN_TOKEN=hunter2\n", "--all stripped .env"
    assert (clone / "sessions.db").exists(), "--all stripped the database"
    assert (clone / ".venv" / "pyvenv.cfg").exists(), "--all stripped the virtualenv"


@needs_git
def test_a_rollback_with_a_clean_tree_creates_no_stash(repo, monkeypatch):
    """The preserve step must be silent when there is nothing to preserve — a stash appearing after
    every failed update would train the maintainer to ignore the ones that matter."""
    _stub_pip(monkeypatch, rc=1, fail_first_only=True)
    res = updater.apply_update("v0.2.0", repo)
    assert res["state"] == "reverted"
    assert res["stash"] is None
    assert _git(repo, "stash", "list") == "", "a rollback with nothing to save left a stash behind"
    assert "git stash" not in res["detail"]


@needs_git
def test_apply_from_a_branch_records_the_branch_name_not_the_sha(tmp_path, monkeypatch):
    origin = _make_origin(tmp_path)
    clone = _make_clone(tmp_path, origin, at="")          # stays on main
    _pin(monkeypatch, clone)
    _stub_pip(monkeypatch, rc=1, fail_first_only=True)    # fail so we can watch the rollback target
    updater.apply_update("v0.2.0", clone)
    st = updater.read_state(clone)
    assert st["from_ref"] == "main", "reverting must restore the branch, not a detached sha"
    assert _git(clone, "symbolic-ref", "--short", "HEAD") == "main"


# --------------------------------------------------------------------------
# _stream — the deadline has to be enforced OUT OF BAND
# --------------------------------------------------------------------------
def _stream_with_a_hard_stop(argv, cwd, timeout, budget: float = 15.0):
    """Run _stream on a thread so a deadline that is NOT enforced fails the test instead of hanging
    the whole suite. Returns (result_box, thread, elapsed)."""
    box: dict = {}
    th = threading.Thread(target=lambda: box.update(rc=updater._stream(argv, cwd, timeout)),
                          daemon=True)
    started = time.monotonic()
    th.start()
    th.join(budget)
    return box, th, time.monotonic() - started


@pytest.mark.parametrize("label,body", [
    ("silent", "import time; time.sleep(60)"),
    ("one line then silence",
     "import sys, time; print('Collecting argus'); sys.stdout.flush(); time.sleep(60)"),
])
def test_stream_enforces_the_deadline_when_the_child_goes_quiet(tmp_path, label, body):
    """A deadline checked only inside `for line in p.stdout` is not a deadline: the case that
    matters is a child that stops producing output (pip stuck on a dead index, git waiting on
    credentials), and that child never reaches the check. A stalled step parks a worker thread
    forever with HEAD ALREADY on the new tag, so the rollback never runs."""
    box, th, elapsed = _stream_with_a_hard_stop([sys.executable, "-c", body], tmp_path, 1.0)
    assert not th.is_alive(), f"the deadline was never enforced ({label}) — _stream is still running"
    assert box["rc"] == 124, f"a timeout must report 124, got {box.get('rc')!r} ({label})"
    assert elapsed < 10.0, f"the 1s deadline took {elapsed:.1f}s to fire ({label})"


def test_stream_reaps_the_child_and_closes_the_pipe_on_timeout(tmp_path, monkeypatch):
    """Killing a process is not the same as reaping it: `p.kill()` alone leaves a zombie and an
    open pipe for the lifetime of this long-running process."""
    made: list = []
    real_popen = subprocess.Popen

    def spy(*a, **kw):
        p = real_popen(*a, **kw)
        made.append(p)
        return p
    monkeypatch.setattr(updater.subprocess, "Popen", spy)
    box, th, _ = _stream_with_a_hard_stop(
        [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, 1.0)
    assert not th.is_alive() and box["rc"] == 124
    assert len(made) == 1
    assert made[0].returncode is not None, "the killed child was never waited on — it is a zombie"
    assert made[0].stdout.closed, "the stdout pipe was never closed"


def test_stream_bounds_wall_clock_when_a_descendant_still_holds_the_pipe(tmp_path):
    """THE case the deadline is for, and the one the tests above cannot see.

    Both parametrized bodies above are CHILDLESS, which is the only shape a direct-child kill
    handles. `pip install -e .` never has that shape: PEP 517 runs the build backend
    (setuptools.build_meta) in a subprocess, which runs compilers and git. Every descendant inherited
    the stdout pipe, so killing the direct child alone leaves the write end open and
    `for line in p.stdout` blocks on with the child already dead — the deadline returns nothing, and
    a stalled pip still parks a worker thread forever with HEAD on the new tag and the rollback
    unreachable. Only killing the process GROUP ends it.
    """
    body = ("import subprocess, sys, time\n"
            "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)'])\n"
            "print(kid.pid, flush=True)\n"          # it inherited our stdout — that is the point
            "time.sleep(20)\n")
    lines: list = []
    box: dict = {}
    th = threading.Thread(
        target=lambda: box.update(rc=updater._stream([sys.executable, "-c", body], tmp_path, 1.0,
                                                     lines.append)),
        daemon=True)
    started = time.monotonic()
    th.start()
    th.join(8.0)
    elapsed = time.monotonic() - started

    assert not th.is_alive(), ("the 1s deadline never fired: the direct child was killed but a "
                               "descendant is still holding the stdout pipe open")
    assert box["rc"] == 124
    assert elapsed < 6.0, f"the 1s deadline took {elapsed:.1f}s to fire"

    descendant = next(int(ln) for ln in lines if ln.strip().isdigit())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(descendant, 0)
        except OSError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the descendant ({descendant}) outlived the timeout — the group was not killed")


def test_stream_watchdog_never_signals_a_child_that_has_already_been_reaped(tmp_path, monkeypatch):
    """`done.wait(timeout)` returning False does NOT prove the child is still running: it can lose
    that race by microseconds to the main thread's p.wait(). killpg would then send SIGKILL to
    whatever process group has since inherited that pgid — someone else's processes. Ask the
    process, not the event."""
    killed: list = []
    decided = threading.Event()          # the watchdog has finished making up its mind

    def _blocking_stdout():
        # Holds the read loop open until the watchdog has decided, so the assertion below is about
        # the guard and not about which thread won a race.
        decided.wait(5.0)
        return
        yield                            # pragma: no cover - generator, never reached

    class _AlreadyExited:
        """A child that finished — and was reaped by the main thread — just as the deadline fired."""
        pid = 4242424
        stdout = _blocking_stdout()

        def poll(self):
            decided.set()                # the guard asked; that IS the decision
            return 0

        def wait(self):
            return 0

        def kill(self):
            killed.append("kill")
            decided.set()

    proc = _AlreadyExited()
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **kw: proc)
    monkeypatch.setattr(updater.os, "killpg", lambda pid, sig: (killed.append(pid), decided.set()))
    updater._stream([sys.executable, "-c", "pass"], tmp_path, 0.05)
    assert killed == [], "the watchdog signalled a process group it may no longer own"


def test_stream_still_returns_the_real_exit_code_of_a_quick_command(tmp_path):
    lines: list = []
    rc = updater._stream([sys.executable, "-c", "print('hello'); raise SystemExit(3)"],
                         tmp_path, 30.0, lines.append)
    assert rc == 3
    assert "hello" in lines


# --------------------------------------------------------------------------
# mutual exclusion — two updates into one venv is a corrupt site-packages.
#
# The exclusion is an asyncio.Lock held in the ASYNC layer (updater.exclusive), so these tests drive
# the *_async entry points. There is no lock FILE to test: Argus is one process (uvicorn and the
# Telegram bot share one event loop), so a lock on the filesystem was solving a problem that does not
# exist here while introducing three of its own — a non-atomic create-then-write, a non-atomic stale
# takeover, and a release that unlinked whatever lock happened to be present.
# --------------------------------------------------------------------------
@needs_git
async def test_two_concurrent_applies_produce_exactly_one_apply_and_one_busy(repo, monkeypatch):
    """Dashboard and Telegram are separate entry points into the same install. Two concurrent
    `pip install -e .` runs can leave site-packages half-written, and two apply_update()s
    interleaving their write_state() calls can clobber `from_ref` — the only recorded way back."""
    calls: list = []
    _stub_pip(monkeypatch, calls=calls)
    monkeypatch.setattr(updater, "restart_strategy",
                        lambda clone_dir=updater.ROOT: {"strategy": "exec", "unit": None,
                                                        "instruction": "x"})
    first, second = await asyncio.gather(updater.apply_update_async("v0.2.0", repo),
                                         updater.apply_update_async("v0.2.0", repo))
    assert sorted([first["state"], second["state"]]) == ["applied", "busy"]
    busy = first if first["state"] == "busy" else second
    assert busy["ok"] is False and busy["failed_step"] == "lock"
    assert "Another update is already running" in busy["detail"]
    assert len(calls) == 1, "exactly one pip install ran"
    assert updater.read_state(repo)["state"] == "applied"
    assert updater.update_in_progress() is False, "the lock must be released on the way out"


@needs_git
async def test_an_update_refused_as_busy_touches_nothing(repo, monkeypatch):
    _stub_pip(monkeypatch)
    before = _git(repo, "rev-parse", "HEAD")
    events: list = []
    async with updater.exclusive():                  # stand in for an update already in flight
        assert updater.update_in_progress() is True
        res = await updater.apply_update_async("v0.2.0", repo, emit=events.append)
        res_revert = await updater.revert_async(repo)
    assert res["ok"] is False and res["state"] == "busy" and res["failed_step"] == "lock"
    assert res_revert["state"] == "busy", "revert must take the same lock as apply"
    assert _git(repo, "rev-parse", "HEAD") == before, "the refused update must not touch the tree"
    assert updater.read_state(repo) == {}, "nor overwrite the running update's recorded ref"
    assert [e["type"] for e in events] == ["done"], "the refusal is a terminal done event"
    # Released on the way out, so the next attempt is not blocked forever.
    assert updater.update_in_progress() is False
    assert (await updater.apply_update_async("v0.2.0", repo))["ok"] is True


async def test_an_apply_is_refused_once_a_restart_has_been_scheduled(tmp_path, monkeypatch):
    """The CONVERSE of the lock, and the half that was missing. /update/restart answers the request
    first and replaces the process ~0.6s later; the lock is not held across that gap. An apply
    starting inside it takes the lock legitimately and is then killed mid-pip — HEAD already on the
    new tag, the rollback never reached. So a scheduled restart refuses new work too."""
    monkeypatch.setattr(updater, "_RESTART_PENDING", False)
    monkeypatch.setattr(updater, "apply_update",
                        lambda *a, **k: pytest.fail("an update started into a pending restart"))
    monkeypatch.setattr(updater, "revert",
                        lambda *a, **k: pytest.fail("a revert started into a pending restart"))
    updater.mark_restart_pending()
    events: list = []
    res = await updater.apply_update_async("v0.2.0", tmp_path, events.append)
    assert res["state"] == "busy" and res["ok"] is False
    assert "restarting" in res["detail"]
    assert [e["type"] for e in events] == ["done"], "the refusal is a terminal done event"
    assert (await updater.revert_async(tmp_path))["state"] == "busy"
    assert updater.update_in_progress() is False, "the refusal must not take the lock"
    updater.clear_restart_pending()
    assert updater.restart_pending() is False


async def test_a_restart_that_never_happens_stops_refusing_everything_forever(monkeypatch):
    """The flag's old docstring claimed it "is only ever set by a process about to stop existing, so
    there is nothing to expire". That is false, and the cost of it being false is total.

    `perform_restart` for systemd is a fire-and-forget `Popen(["systemctl", "--user", "restart", …])`
    whose exit code is NEVER READ. A systemctl that fails, is masked, or no-ops leaves this process
    alive with the flag up — no exception anywhere. Every subsequent apply and revert is then refused
    with "try again once it is back", from a process that is never coming back, and the only cure is
    a manual restart from the command line: precisely the outcome this whole feature exists to avoid.
    """
    monkeypatch.setattr(updater, "_RESTART_PENDING", False)
    monkeypatch.setattr(updater, "RESTART_PENDING_TTL", 0.15)
    updater.mark_restart_pending()
    assert updater.restart_pending() is True, "it must still fence the handoff window it is for"
    await asyncio.sleep(0.25)
    assert updater.restart_pending() is False, (
        "a restart that has not replaced this process by now did not happen — and a flag that never "
        "clears bricks every future update from the UI")
    # ...and the next update really does get through.
    monkeypatch.setattr(updater, "apply_update", lambda *a, **k: {"ok": True, "state": "applied"})
    assert (await updater.apply_update_async("v0.2.0", Path("."), lambda _e: None))["ok"] is True


async def test_the_exclusion_never_queues(tmp_path):
    """A second update that WAITED would then run against a tree the first one already moved,
    deciding what to do from a preflight taken before that. Busy is the answer, not a delay."""
    async with updater.exclusive():
        started = time.monotonic()
        with pytest.raises(updater.UpdateBusy):
            async with updater.exclusive():
                pytest.fail("a second holder got in")
        assert time.monotonic() - started < 0.5, "it waited instead of refusing"
    async with updater.exclusive():                  # and it is usable again straight after
        pass


# --------------------------------------------------------------------------
# revert
# --------------------------------------------------------------------------
@needs_git
def test_revert_restores_the_previous_ref(repo, monkeypatch):
    _stub_pip(monkeypatch)
    monkeypatch.setattr(updater, "restart_strategy",
                        lambda clone_dir=updater.ROOT: {"strategy": "exec", "unit": None,
                                                        "instruction": "x"})
    before = _git(repo, "rev-parse", "HEAD")
    assert updater.apply_update("v0.2.0", repo)["ok"] is True
    assert _git(repo, "rev-parse", "HEAD") != before

    res = updater.revert(repo)
    assert res["ok"] is True and res["state"] == "reverted"
    assert _git(repo, "rev-parse", "HEAD") == before
    assert (repo / "pyproject.toml").read_text().count('version = "0.1.0"') == 1


@needs_git
def test_revert_refuses_with_no_recorded_update(repo):
    ok, reason = updater.can_revert(repo)
    assert ok is False and "no recorded update" in reason
    res = updater.revert(repo)
    assert res["ok"] is False and res["failed_step"] == "precondition"


@needs_git
def test_a_rolled_back_install_has_nothing_left_to_revert(repo, monkeypatch):
    """After the automatic rollback this install IS on the previous release again — offering
    "Revert to v0.1.0" while running v0.1.0 is a lie, and the button would do nothing useful."""
    _stub_pip(monkeypatch, rc=1, fail_first_only=True)
    assert updater.apply_update("v0.2.0", repo)["state"] == "reverted"
    ok, reason = updater.can_revert(repo)
    assert ok is False and "already been put back" in reason and "v0.1.0" in reason


@needs_git
def test_revert_refuses_when_the_previous_ref_no_longer_resolves(repo):
    updater.write_state(repo, state="applied", from_ref="deadbee", from_tag="v0.0.1",
                        to_tag="v0.2.0")
    ok, reason = updater.can_revert(repo)
    assert ok is False and "no longer resolves" in reason and "install.sh" in reason


# --------------------------------------------------------------------------
# restart strategy — the MainPID guard
# --------------------------------------------------------------------------
def _fake_service_status(monkeypatch, **fields):
    import engine.service as svc
    base = {"ok": True, "supported": True, "installed": True, "active": True,
            "name": "argus.service"}
    base.update(fields)
    monkeypatch.setattr(svc, "status", lambda name=None, clone_dir=None: dict(base))


def test_restart_strategy_systemd_requires_active_and_matching_mainpid(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    # 1. active unit whose MainPID IS us -> systemd is safe.
    _fake_service_status(monkeypatch)
    monkeypatch.setattr(updater, "_run",
                        lambda argv, cwd=updater.ROOT, timeout=20.0: (0, f"MainPID={os.getpid()}", ""))
    info = updater.restart_strategy(tmp_path)
    assert info["strategy"] == "systemd" and info["unit"] == "argus.service"

    # 2. a FOREIGN MainPID means restarting the unit would not replace us — fall back to exec.
    monkeypatch.setattr(updater, "_run",
                        lambda argv, cwd=updater.ROOT, timeout=20.0: (0, "MainPID=999999", ""))
    assert updater.restart_strategy(tmp_path)["strategy"] == "exec"

    # 3. installed but INACTIVE: `systemctl restart` would START A SECOND INSTANCE that collides
    #    with this one on the port. Must never pick systemd.
    #    MainPID is stubbed as OURS here on purpose: with the realistic MainPID=0 of a stopped unit
    #    the pid equality alone already forces exec, and this case would pass with the `active`
    #    guard deleted from restart_strategy() — a vacuous assertion. Our own pid is the only stub
    #    under which the `active` check is the thing being tested.
    _fake_service_status(monkeypatch, active=False)
    monkeypatch.setattr(updater, "_run",
                        lambda argv, cwd=updater.ROOT, timeout=20.0: (0, f"MainPID={os.getpid()}", ""))
    assert updater.restart_strategy(tmp_path)["strategy"] == "exec"

    # 4. not installed at all -> exec.
    _fake_service_status(monkeypatch, installed=False)
    assert updater.restart_strategy(tmp_path)["strategy"] == "exec"


def test_restart_strategy_manual_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    info = updater.restart_strategy(tmp_path)
    assert info["strategy"] == "manual" and info["unit"] is None
    assert "Ctrl+C" in info["instruction"] and str(tmp_path) in info["instruction"]


def test_perform_restart_manual_does_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(updater.subprocess, "Popen", lambda *a, **k: called.append(a))
    monkeypatch.setattr(updater.os, "execv", lambda *a: called.append(a))
    updater.perform_restart({"strategy": "manual", "unit": None})
    assert called == []


def test_perform_restart_systemd_is_detached(monkeypatch):
    seen = {}

    def fake_popen(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
    monkeypatch.setattr(updater.subprocess, "Popen", fake_popen)
    updater.perform_restart({"strategy": "systemd", "unit": "argus.service"})
    assert seen["argv"] == ["systemctl", "--user", "restart", "argus.service"]
    # Detached: the helper must survive the SIGTERM systemd is about to send us.
    assert seen["kw"].get("start_new_session") is True


# --------------------------------------------------------------------------
# state file
# --------------------------------------------------------------------------
def test_state_roundtrip_and_clear(tmp_path):
    assert updater.read_state(tmp_path) == {}
    updater.write_state(tmp_path, state="applying", from_ref="v0.1.0")
    updater.write_state(tmp_path, pending_notice={"chat_id": 7, "to": "v0.2.0"})
    st = updater.read_state(tmp_path)
    assert st["state"] == "applying" and st["from_ref"] == "v0.1.0"
    assert st["pending_notice"]["chat_id"] == 7
    updater.clear_state(tmp_path)
    assert updater.read_state(tmp_path) == {}


def test_state_file_is_gitignored():
    text = (REPO_ROOT / ".gitignore").read_text()
    assert ".argus-update.json" in text
    # Never written any more (the exclusion is an asyncio.Lock), but a file left behind by an older
    # version must still not show up as a change.
    assert ".argus-update.lock" in text
    # There is nothing else to ignore: a failed rollback saves the tree with `git stash`, which
    # keeps it inside .git rather than in a directory of its own.
    assert ".argus-rescue" not in text


def test_revert_command_is_literal_and_runnable(tmp_path):
    updater.write_state(tmp_path, state="applied", from_ref="v0.1.0")
    cmd = updater.revert_command(tmp_path)
    assert str(tmp_path) in cmd and "git checkout v0.1.0" in cmd and "pip install -e ." in cmd
