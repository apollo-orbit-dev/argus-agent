"""Self-update for a git-checkout install: move this clone to the newest **release TAG** and
reinstall it. Stdlib only.

TAGS, NEVER main (settled decision, argus-rzu). "Newest version" is the newest `v*` tag, because a
tag is the point where the release process has asserted coherence (version bumped, CHANGELOG
written, suite green). A commit merged to main is not an update candidate until it is tagged.

Shape matched to install.sh: clone -> checkout newest tag -> .venv -> `pip install -e .`. The
updater does exactly the last three steps again, so an install produced by the installer and an
install produced by this module are the same thing.

USER STATE IS NEVER TOUCHED — and that is structural, not a code path: `.env`, `*.db`,
`model_presets.json`, `workspaces/`, `created_tools/`, `SOUL.md`, `trusted_tools.json` … are all in
.gitignore, and `git checkout <tag>` does not touch ignored or untracked files. There is deliberately
no "clean the tree" step anywhere in here. tests/test_updater.py proves it against the real
.gitignore.

Every subprocess call goes through `_run` (capture) or `_stream` (line-by-line) — the two seams the
suite monkeypatches, so tests never invoke real git/pip against the network.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Callable, Optional

from engine.version import compare_versions, get_version

ROOT = Path(__file__).resolve().parents[1]          # clone dir (holds main.py, pyproject.toml, .env)

# THE definition of "newest release tag". install.sh:118 runs the same expression when it pins a
# fresh clone; tests/test_updater.py::test_tag_expression_matches_install_sh greps install.sh and
# asserts equality. If you change one, change the other — the test exists to make that impossible
# to forget, because an installer and an updater that disagree about "newest" is a silent downgrade.
NEWEST_TAG_ARGV = ["git", "tag", "-l", "v*", "--sort=-v:refname"]

STATE_FILE = ROOT / ".argus-update.json"            # gitignored; see state_path()
CHANGELOG_CAP = 8000
PIP_TIMEOUT = 600.0                                 # a cold-cache wheel build is slow (matches sandbox setup)
FETCH_TIMEOUT = 120.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")     # pip colour codes render as garbage in a <pre>
_SECTION_RE = re.compile(r"^## (\d+\.\d+\.\d+)\s*$", re.MULTILINE)

Emit = Callable[[dict], None]


# --------------------------------------------------------------------------
# subprocess seams
# --------------------------------------------------------------------------
def _run(argv: list[str], cwd: Path = ROOT, timeout: float = 20.0) -> tuple[int, str, str]:
    """Capture seam. Returns (returncode, stdout_stripped, stderr_stripped)."""
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"{argv[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"{argv[0]}: timed out after {timeout:.0f}s"
    except OSError as e:
        return 1, "", str(e)


def _stream(argv: list[str], cwd: Path = ROOT, timeout: float = PIP_TIMEOUT,
            emit: Optional[Callable[[str], None]] = None) -> int:
    """Streaming seam: run argv, hand every output line to `emit` as it arrives, return the exit
    code. stderr is folded into stdout so pip's warnings appear in order."""
    say = emit or (lambda _line: None)
    say("$ " + " ".join(argv))
    try:
        p = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1)
    except FileNotFoundError:
        say(f"{argv[0]}: not found")
        return 127
    except OSError as e:
        say(str(e))
        return 1
    deadline = time.monotonic() + timeout
    try:
        assert p.stdout is not None
        for line in p.stdout:
            say(_ANSI_RE.sub("", line.rstrip("\n")))
            if time.monotonic() > deadline:
                p.kill()
                say(f"timed out after {timeout:.0f}s")
                return 124
        return p.wait(timeout=max(1.0, deadline - time.monotonic()))
    except subprocess.TimeoutExpired:
        p.kill()
        say(f"timed out after {timeout:.0f}s")
        return 124


def _pip_argv() -> list[str]:
    return [sys.executable, "-m", "pip", "install", "-e", ".", "--disable-pip-version-check"]


def _running_prefix() -> Path:
    """sys.prefix as a resolved Path. A function so tests have a seam for the wrong_venv check."""
    return Path(sys.prefix).resolve()


# --------------------------------------------------------------------------
# git facts
# --------------------------------------------------------------------------
def is_checkout(clone_dir: Path = ROOT) -> bool:
    return (clone_dir / ".git").exists()


def newest_tag(clone_dir: Path = ROOT) -> Optional[str]:
    """The newest `v*` tag by VERSION order (`--sort=-v:refname`, so v0.10.0 > v0.9.0), or None."""
    rc, out, _ = _run(NEWEST_TAG_ARGV, clone_dir)
    if rc != 0:
        return None
    for line in out.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def current_ref(clone_dir: Path = ROOT) -> dict:
    """What HEAD is right now: {"kind": "branch"|"detached", "name", "sha", "tag"}.

    `name` is what a revert should check out — the BRANCH NAME when we're on a branch (so reverting
    restores the branch rather than leaving a detached sha), otherwise the tag or the sha."""
    _, sha, _ = _run(["git", "rev-parse", "HEAD"], clone_dir)
    rc_b, branch, _ = _run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], clone_dir)
    if rc_b == 0 and branch:
        return {"kind": "branch", "name": branch, "sha": sha, "tag": None}
    _, tags, _ = _run(["git", "tag", "--points-at", "HEAD"], clone_dir)
    tag = next((t.strip() for t in tags.splitlines() if t.strip().startswith("v")), None)
    return {"kind": "detached", "name": tag or sha, "sha": sha, "tag": tag}


def _resolves(ref: str, clone_dir: Path = ROOT) -> bool:
    rc, _, _ = _run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], clone_dir)
    return rc == 0


def _pyproject_version(clone_dir: Path = ROOT) -> Optional[str]:
    """Read the version straight off disk. Deliberately NOT get_version() — that is lru_cached and
    would still report the pre-update version after the checkout."""
    try:
        with open(clone_dir / "pyproject.toml", "rb") as f:
            data = tomllib.load(f)
        return data.get("project", {}).get("version")
    except Exception:
        return None


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------
def _b(code: str, message: str, severity: str = "error") -> dict:
    return {"code": code, "severity": severity, "message": message}


def preflight(clone_dir: Path = ROOT) -> dict:
    """Every reason this install can't (or needn't) be updated, collected together — each with a
    readable, specific message. "Update failed" on the instance someone uses daily is the worst
    possible output, so every refusal says exactly what is wrong and what to do about it.

    Runs `git fetch --tags` before the tag checks. Fetch mutates refs only — never the working tree.
    """
    current = get_version()
    blockers: list[dict] = []

    if not is_checkout(clone_dir):
        blockers.append(_b("not_a_checkout",
            f"This install is not a git checkout — there is no .git directory in {clone_dir}, so "
            f"there is nothing to update from. Re-install with install.sh to get an updatable copy."))
        return {"ok": False, "current": current, "target": None,
                "update_available": False, "blockers": blockers}

    rc_o, origins, _ = _run(["git", "remote"], clone_dir)
    has_origin = rc_o == 0 and "origin" in origins.split()
    if not has_origin:
        blockers.append(_b("no_origin",
            "This checkout has no 'origin' remote, so there is nowhere to fetch new releases from. "
            "Add one with: git remote add origin <url>"))

    rc_s, porcelain, _ = _run(["git", "status", "--porcelain"], clone_dir)
    if rc_s == 0 and porcelain.strip():
        # Ignored files never appear in --porcelain, so .env / *.db / model_presets.json /
        # workspaces/ cannot trip this. Anything listed here is a tracked file you edited.
        # "XY path" — split on whitespace rather than slicing [3:], because _run() strips the
        # leading space of a " M path" status line and a fixed offset would eat the filename.
        names = [ln.strip().split(None, 1)[-1] for ln in porcelain.splitlines() if ln.strip()]
        shown = ", ".join(names[:8]) + (f", and {len(names) - 8} more" if len(names) > 8 else "")
        blockers.append(_b("dirty_tree",
            f"The working tree has uncommitted changes to tracked files ({shown}). Updating would "
            f"overwrite them — commit, stash or discard them first. (Your .env, databases, "
            f"workspaces and connections are gitignored and are never part of this.)"))

    expected_venv = (clone_dir / ".venv").resolve()
    running = _running_prefix()
    if running != expected_venv:
        blockers.append(_b("wrong_venv",
            f"The running Python lives in {running}, but this checkout's virtualenv is "
            f"{expected_venv}. Updating would install the new dependencies into a different "
            f"environment than the one that gets restarted, so the restart would come back on the "
            f"old code. Start Argus from {expected_venv} and try again."))

    if not _pip_available():
        blockers.append(_b("no_pip",
            f"pip is not available in {running}, so the new dependencies cannot be installed. "
            f"Install it with: {sys.executable} -m ensurepip --upgrade"))

    if has_origin:
        rc_f, _, err_f = _run(["git", "fetch", "--tags", "--quiet", "origin"], clone_dir,
                              timeout=FETCH_TIMEOUT)
        if rc_f != 0:
            blockers.append(_b("no_network",
                f"Could not reach origin to look for new releases: {err_f or 'git fetch failed'}. "
                f"Check this machine's network (or the remote) and try again."))

    target = newest_tag(clone_dir)
    if not target:
        blockers.append(_b("no_tags",
            "No release tags (v*) exist in this checkout, so there is no release to update to. "
            "Updates follow published release tags, never the main branch."))
        return {"ok": False, "current": current, "target": None,
                "update_available": False, "blockers": blockers}

    # ONE comparator (engine.version.compare_versions) asked both ways — never a second parser.
    newer = compare_versions(current, target)["update_available"]
    older = compare_versions(target, current)["update_available"]
    if not newer and not older:
        blockers.append(_b("up_to_date",
            f"Already up to date — running v{current}, and the newest release is {target}.", "info"))
    elif older:
        blockers.append(_b("ahead_of_tags",
            f"This checkout is ahead of every published release: it reports v{current}, but the "
            f"newest release tag is {target}. There is nothing to update to.", "info"))
    elif not _resolves(target, clone_dir):
        blockers.append(_b("target_missing",
            f"The tag {target} is listed but does not resolve to a commit in this checkout — that "
            f"is what a shallow clone looks like. Fetch the full history first: git fetch --unshallow"))

    errors = [b for b in blockers if b["severity"] == "error"]
    return {"ok": not errors and newer, "current": current, "target": target,
            "update_available": bool(newer) and not errors, "blockers": blockers}


def _pip_available() -> bool:
    try:
        import importlib.util
        return importlib.util.find_spec("pip") is not None
    except Exception:
        return False


# --------------------------------------------------------------------------
# changelog
# --------------------------------------------------------------------------
def changelog_between(clone_dir: Path, target: str, current: str) -> tuple[Optional[str], bool, Optional[str]]:
    """The CHANGELOG sections a user is about to receive: every `## X.Y.Z` block newer than the
    running version and no newer than the target.

    Read from the TARGET tag (`git show <tag>:CHANGELOG.md`), not from disk — the section describing
    the new release does not exist in the checkout we are currently running. Returns
    (text|None, truncated, note)."""
    rc, text, _ = _run(["git", "show", f"{target}:CHANGELOG.md"], clone_dir, timeout=30.0)
    if rc != 0 or not text.strip():
        return None, False, (f"{target} has no CHANGELOG.md, so there is no summary of what changed.")

    marks = list(_SECTION_RE.finditer(text))
    if not marks:
        return None, False, f"{target}'s CHANGELOG.md has no recognizable '## X.Y.Z' sections."

    chunks: list[str] = []
    for i, m in enumerate(marks):
        version = m.group(1)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        newer_than_current = compare_versions(current, version)["update_available"]
        newer_than_target = compare_versions(target, version)["update_available"]
        if newer_than_current and not newer_than_target:
            chunks.append(text[m.start():end].rstrip())
    if not chunks:
        return None, False, (f"No CHANGELOG section between v{current} and {target} — the release "
                             f"notes may not have been written for this tag.")
    body = "\n\n".join(chunks)
    truncated = len(body) > CHANGELOG_CAP
    return (body[:CHANGELOG_CAP] if truncated else body), truncated, None


# --------------------------------------------------------------------------
# restart strategy
# --------------------------------------------------------------------------
def restart_strategy(clone_dir: Path = ROOT) -> dict:
    """How this process should be replaced after an update: {"strategy", "unit", "instruction"}.

    systemd ONLY when the unit is active AND its MainPID is this process. That equality is the
    bead's "is the current process the thing that would be restarted?" check made exact: an
    installed-but-INACTIVE unit would not restart us, it would START A SECOND INSTANCE, which then
    collides with us on the port. Anything else POSIX falls back to re-exec; win32 is manual.
    """
    if sys.platform == "win32":
        return {"strategy": "manual", "unit": None,
                "instruction": ("Argus is installed but still running the OLD code. Stop this "
                                "process (Ctrl+C in its window) and start it again:\n"
                                f"    cd {clone_dir}\n    .venv\\Scripts\\argus run")}
    try:
        from engine import service
        st = service.status(clone_dir=clone_dir)
    except Exception:
        st = {}
    if st.get("supported") and st.get("installed") and st.get("active"):
        unit = st.get("name") or "argus.service"
        rc, out, _ = _run(["systemctl", "--user", "show", unit, "--property=MainPID"], clone_dir)
        main_pid = out.split("=", 1)[1].strip() if (rc == 0 and "=" in out) else ""
        if main_pid and main_pid == str(os.getpid()):
            return {"strategy": "systemd", "unit": unit,
                    "instruction": f"systemctl --user restart {unit}"}
    # Not a no-op: os.execv is the same primitive /admin/restart already uses. It picks up the new
    # site-packages because imports resolve at exec time, and it keeps the PID so .argus.pid stays
    # valid.
    return {"strategy": "exec", "unit": None,
            "instruction": "re-exec this process in place (same PID)"}


def perform_restart(info: dict) -> None:
    """Actually replace the process. Called ONLY from a background task, after the HTTP response /
    Telegram reply has flushed — never inline in a request handler."""
    strategy = info.get("strategy")
    if strategy == "systemd":
        # start_new_session so the helper is NOT our child: it has to outlive the SIGTERM systemd
        # is about to send us.
        subprocess.Popen(["systemctl", "--user", "restart", info["unit"]], start_new_session=True)
    elif strategy == "exec":
        os.execv(sys.executable, [sys.executable] + sys.argv)
    # "manual": nothing to do — the instruction was handed to the user instead.


# --------------------------------------------------------------------------
# persisted state (so a revert is still offerable after the process restarts)
# --------------------------------------------------------------------------
def state_path(clone_dir: Path = ROOT) -> Path:
    return clone_dir / ".argus-update.json"


def read_state(clone_dir: Path = ROOT) -> dict:
    try:
        data = json.loads(state_path(clone_dir).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_state(clone_dir: Path = ROOT, **fields) -> dict:
    st = read_state(clone_dir)
    st.update(fields)
    st["at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    try:
        state_path(clone_dir).write_text(json.dumps(st, indent=2), encoding="utf-8")
    except OSError:
        pass
    return st


def clear_state(clone_dir: Path = ROOT) -> None:
    try:
        state_path(clone_dir).unlink()
    except OSError:
        pass


def revert_command(clone_dir: Path = ROOT, from_ref: Optional[str] = None) -> str:
    """The literal shell command that puts this install back. Printed everywhere a failure is
    reported — a stated way back is the whole point."""
    from_ref = from_ref or read_state(clone_dir).get("from_ref") or "<previous ref>"
    if sys.platform == "win32":
        py = clone_dir / ".venv" / "Scripts" / "python.exe"
    else:
        py = clone_dir / ".venv" / "bin" / "python"
    return (f'cd "{clone_dir}" && git checkout {from_ref} && "{py}" -m pip install -e .')


def can_revert(clone_dir: Path = ROOT) -> tuple[bool, str]:
    st = read_state(clone_dir)
    if not st or st.get("state") in (None, "none"):
        return False, "There is no recorded update to revert — this install has not been updated from here."
    from_ref = st.get("from_ref")
    if not from_ref:
        return False, "The recorded update has no previous ref, so there is nothing to go back to."
    if not _resolves(from_ref, clone_dir):
        return False, (f"The previous ref {from_ref} no longer resolves in this checkout (it may have "
                       f"been garbage-collected). Re-install with install.sh to get back to a known state.")
    return True, ""


# --------------------------------------------------------------------------
# preview / apply / revert
# --------------------------------------------------------------------------
def preview(clone_dir: Path = ROOT) -> dict:
    """Everything the UI needs to decide, without mutating the working tree."""
    pf = preflight(clone_dir)
    ref = current_ref(clone_dir) if is_checkout(clone_dir) else {"kind": "unknown", "name": None,
                                                                 "sha": None, "tag": None}
    changelog, truncated, note = None, False, None
    if pf.get("update_available") and pf.get("target"):
        changelog, truncated, note = changelog_between(clone_dir, pf["target"], pf["current"])
    branch_note = None
    if ref.get("kind") == "branch":
        branch_note = (f"This checkout is on branch '{ref['name']}'. Updating checks out the release "
                       f"tag and leaves HEAD detached; reverting restores '{ref['name']}'.")
    return {
        "current": pf["current"],
        "current_ref": ref,
        "target": pf.get("target"),
        "update_available": pf.get("update_available", False),
        "ok": pf.get("ok", False),
        "changelog": changelog,
        "changelog_truncated": truncated,
        "changelog_note": note,
        "branch_note": branch_note,
        "clone_dir": str(clone_dir),
        "restart": restart_strategy(clone_dir),
        "revert_command": revert_command(clone_dir, from_ref=ref.get("name")),
        "blockers": pf.get("blockers", []),
    }


def _verify(clone_dir: Path, target: str) -> tuple[bool, str]:
    _, head, _ = _run(["git", "rev-parse", "HEAD"], clone_dir)
    _, want, _ = _run(["git", "rev-parse", f"{target}^{{commit}}"], clone_dir)
    if not head or head != want:
        return False, f"HEAD is {head or '?'} but {target} is {want or '?'}"
    version = _pyproject_version(clone_dir)
    expected = target[1:] if target.startswith("v") else target
    if version != expected:
        return False, f"pyproject.toml reports {version or '?'}, expected {expected}"
    return True, f"HEAD at {target}, pyproject reports {version}"


def apply_update(target: str, clone_dir: Path = ROOT, emit: Optional[Emit] = None) -> dict:
    """Move this clone to `target` and reinstall. Emits {type: step|log|done} events.

    Nothing here cleans, resets or deletes anything: the only mutating git command is
    `git checkout <tag>`, which leaves ignored and untracked files (all of the user's state) alone.

    If pip or verification fails the previous ref is checked out again and pip is re-run
    IMMEDIATELY, both streamed, and the result is state="reverted" / ok=False with NO restart
    offered. Only if that rollback itself fails do we fall back to printing commands.
    """
    say: Emit = emit or (lambda _ev: None)

    def step(name: str, text: str) -> None:
        say({"type": "step", "step": name, "text": text})

    def log(line: str) -> None:
        say({"type": "log", "line": line})

    ref = current_ref(clone_dir)
    from_ref = ref["name"]
    from_tag = ref.get("tag") or f"v{get_version()}"

    # 1. record — BEFORE anything is touched, so a crash mid-update still leaves a way back.
    step("record", f"recording the current state ({from_ref}) so this can be undone")
    write_state(clone_dir, state="applying", from_tag=from_tag, from_ref=from_ref,
                to_tag=target, failed_step=None, pending_notice=None)
    log(f"previous ref: {from_ref} ({from_tag})")

    def finish(ok: bool, state: str, failed_step: Optional[str], detail: str,
               restart: Optional[dict], commands: Optional[list[str]] = None) -> dict:
        write_state(clone_dir, state=state, failed_step=failed_step)
        result = {"ok": ok, "state": state, "failed_step": failed_step, "detail": detail,
                  "from_tag": from_tag, "from_ref": from_ref, "to_tag": target,
                  "restart": restart, "revert_command": revert_command(clone_dir, from_ref),
                  "commands": commands or []}
        say({"type": "done", **result})
        return result

    def rollback(failed_step: str, detail: str) -> dict:
        step("rollback", f"{failed_step} failed — putting this install back on {from_ref}")
        log(detail)
        rc_co = _stream(["git", "-c", "advice.detachedHead=false", "checkout", "-q", from_ref],
                        clone_dir, 120.0, log)
        rc_pip = _stream(_pip_argv(), clone_dir, PIP_TIMEOUT, log) if rc_co == 0 else 1
        if rc_co == 0 and rc_pip == 0:
            log(f"rolled back to {from_ref} — still running {from_tag}")
            return finish(False, "reverted", failed_step,
                          f"{detail} — rolled back to {from_tag}", None)
        log("ROLLBACK FAILED — this install needs manual attention")
        return finish(False, "needs_manual", failed_step,
                      f"{detail} — and the automatic rollback also failed", None,
                      commands=[revert_command(clone_dir, from_ref)])

    # 2. checkout
    step("checkout", f"checking out {target}")
    rc = _stream(["git", "-c", "advice.detachedHead=false", "checkout", "-q", target],
                 clone_dir, 120.0, log)
    if rc != 0:
        # A failed checkout does not switch HEAD, so the tree is already where it was — no rollback
        # to run, just report it with the way back spelled out.
        return finish(False, "failed", "checkout",
                      f"git checkout {target} failed (exit {rc}) — nothing was changed", None,
                      commands=[revert_command(clone_dir, from_ref)])

    # 3. pip
    step("pip", "installing dependencies (pip install -e .) — this can take a few minutes")
    rc = _stream(_pip_argv(), clone_dir, PIP_TIMEOUT, log)
    if rc != 0:
        return rollback("pip", f"pip install -e . failed (exit {rc})")

    # 4. verify
    step("verify", "verifying the new version is actually installed")
    ok, detail = _verify(clone_dir, target)
    log(detail)
    if not ok:
        return rollback("verify", f"verification failed: {detail}")

    return finish(True, "applied", None, detail, restart_strategy(clone_dir))


def revert(clone_dir: Path = ROOT, emit: Optional[Emit] = None) -> dict:
    """Undo the recorded update: check the previous ref out again and reinstall."""
    say: Emit = emit or (lambda _ev: None)

    def log(line: str) -> None:
        say({"type": "log", "line": line})

    okc, reason = can_revert(clone_dir)
    st = read_state(clone_dir)
    from_ref = st.get("from_ref")
    from_tag = st.get("from_tag") or from_ref
    if not okc:
        result = {"ok": False, "state": st.get("state", "none"), "failed_step": "precondition",
                  "detail": reason, "restart": None, "commands": []}
        say({"type": "done", **result})
        return result

    say({"type": "step", "step": "checkout", "text": f"checking out {from_ref}"})
    rc = _stream(["git", "-c", "advice.detachedHead=false", "checkout", "-q", from_ref],
                 clone_dir, 120.0, log)
    if rc != 0:
        write_state(clone_dir, state="needs_manual", failed_step="checkout")
        result = {"ok": False, "state": "needs_manual", "failed_step": "checkout",
                  "detail": f"git checkout {from_ref} failed (exit {rc})", "restart": None,
                  "commands": [revert_command(clone_dir, from_ref)]}
        say({"type": "done", **result})
        return result

    say({"type": "step", "step": "pip", "text": "reinstalling dependencies"})
    rc = _stream(_pip_argv(), clone_dir, PIP_TIMEOUT, log)
    if rc != 0:
        write_state(clone_dir, state="needs_manual", failed_step="pip")
        result = {"ok": False, "state": "needs_manual", "failed_step": "pip",
                  "detail": f"pip install -e . failed (exit {rc})", "restart": None,
                  "commands": [revert_command(clone_dir, from_ref)]}
        say({"type": "done", **result})
        return result

    write_state(clone_dir, state="reverted", failed_step=None, to_tag=from_tag)
    result = {"ok": True, "state": "reverted", "failed_step": None,
              "detail": f"reverted to {from_tag}", "restart": restart_strategy(clone_dir),
              "from_tag": from_tag, "commands": []}
    say({"type": "done", **result})
    return result
