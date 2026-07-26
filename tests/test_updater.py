"""engine/updater.py — the self-update engine.

The git-level tests build REAL temporary repositories (a working repo used as `origin` plus a
clone), so checkout/tag/status behaviour is the real thing rather than a mock's opinion of it. Only
pip is stubbed, through the `_stream` seam — nothing here touches the network or the developer's
own install.
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
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
    monkeypatch.setattr(updater, "_running_prefix", lambda: (tmp_path / ".venv").resolve())
    pf = updater.preflight(tmp_path)
    assert _codes(pf) == ["not_a_checkout"]
    assert "not a git checkout" in _msg(pf, "not_a_checkout")
    assert "install.sh" in _msg(pf, "not_a_checkout")
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
    _fake_service_status(monkeypatch, active=False)
    monkeypatch.setattr(updater, "_run",
                        lambda argv, cwd=updater.ROOT, timeout=20.0: (0, "MainPID=0", ""))
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
    assert ".argus-update.json" in (REPO_ROOT / ".gitignore").read_text()


def test_revert_command_is_literal_and_runnable(tmp_path):
    updater.write_state(tmp_path, state="applied", from_ref="v0.1.0")
    cmd = updater.revert_command(tmp_path)
    assert str(tmp_path) in cmd and "git checkout v0.1.0" in cmd and "pip install -e ." in cmd
