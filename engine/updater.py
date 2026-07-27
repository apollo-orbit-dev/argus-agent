"""Self-update for a git-checkout install: move this clone to the newest **release TAG** and
reinstall it. Stdlib only.

TAGS, NEVER main (settled decision, argus-rzu). "Newest version" is the newest `v*` tag, because a
tag is the point where the release process has asserted coherence (version bumped, CHANGELOG
written, suite green). A commit merged to main is not an update candidate until it is tagged.

Shape matched to install.sh: clone -> checkout newest tag -> .venv -> `pip install -e .`. The
updater does exactly the last three steps again, so an install produced by the installer and an
install produced by this module are the same thing.

USER STATE SURVIVES AN UPDATE — but because of what the RELEASES contain, not because git protects
it. `git checkout <ref>` writes every path <ref> tracks, ignored or not: .gitignore does not defend a
path the target commit tracks, and `--force` overwrites an untracked file there too. What makes the
guarantee hold is that `.env`, `*.db`, `model_presets.json`, `workspaces/`, `created_tools/`,
`SOUL.md`, `trusted_tools.json` … are gitignored AND tracked by no release, so nothing a checkout
writes ever lands on them. There is deliberately no "clean the tree" step anywhere in here.

BOTH WAYS BACK PRESERVE THE TREE WITH GIT'S OWN STASH, not a bespoke copy-aside. There are exactly
TWO checkouts onto an older ref in this module — the automatic rollback inside a failed apply, and
the Revert button (`revert()`) — and both go through `_stash_working_tree` before they check
anything out. Git already gets right every case a hand-rolled copy got wrong — a path that changed
between file and directory, paths git quotes for spaces or non-ASCII, symlinks (saved as links, not
as dereferenced content), permissions and modes — and what it saves is discoverable through a
documented interface (`git stash list`) rather than a directory nobody was told about. It is also
what makes the PLAIN checkout viable: the stash leaves the tree clean, so there is nothing for the
checkout to refuse and no reason to reach for `--force`.

REVERT IS THE LIKELIER OF THE TWO, not the edge case, and it went five rounds unprotected. On the
rollback path the at-risk file can only exist if the running instance recreated it during the pip
window, because the forward checkout to the target already deleted it. On the revert path the file
is there BY CONSTRUCTION: the user has been running the new release for days and the app has been
writing runtime data at that path the whole time, and then they press Revert.

If the stash FAILS, the checkout does not run at all — on either path. A way back that cannot
preserve the tree is the one thing worse than no way back, so the result is state="needs_manual"
with the stash error verbatim and HEAD left exactly where it is. A non-zero exit does NOT license
the report to say "nothing on disk was touched", though: real git writes the stash, deletes the
files, and only then reports a failure it hit while removing one of them. So the stash ref is
re-read on that path and the entry is named if one exists — see `_stash_working_tree`.

`--include-untracked` does not save IGNORED files, and a bare `--all` is not the answer — with no
pathspec it would strip .env, every *.db and .venv/ out of a live install, which is categorically
worse. The shape that leaves exposed is a release that turns a formerly-shipped path into a
*gitignored* runtime data dir: going back writes the old release's file over the user's data there,
silently, because that is what `git checkout` does to a path the target commit tracks. So a SECOND
push covers exactly it — `git stash push --all` restricted by pathspec to the paths `from_ref` tracks
that HEAD does not and where something exists now (`_once_shipped_paths`). The pathspec is what makes
`--all` safe: .env, the databases and .venv/ are never named by it and are never touched, and every
path in it is spelled `:(literal)` so a `*` in a filename cannot widen it onto the neighbours.

Every subprocess call goes through `_run` (capture) or `_stream` (line-by-line) — the two seams the
suite monkeypatches, so tests never invoke real git/pip against the network.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path
from typing import AsyncIterator, Callable, Optional

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


def _run_z(argv: list[str], cwd: Path = ROOT, timeout: float = 60.0) -> tuple[int, list[str]]:
    """The seam for git's `-z` (NUL-separated) output. Returns (returncode, paths).

    A SEPARATE seam from `_run` on purpose. `_run` strips its stdout, which is right for a sha or a
    tag name and WRONG here: `git ls-tree -z` sorts a name with a leading space first, so the strip
    ate the leading space of " leading.txt" and `_once_shipped_paths` then produced a pathspec that
    matched nothing — the file was reported as not at risk and the checkout wrote the release's copy
    over the user's data. The whole point of `-z` is that git emits the name exactly as it is stored,
    and a shared helper that trims it defeats that one layer down.

    Bytes, then `os.fsdecode`, because a path in a git tree is bytes and need not be UTF-8. `text=True`
    raised UnicodeDecodeError straight out of subprocess for such a path, which the SSE catch-all
    turned into "the update crashed" with the state file left at `applying`, HEAD on the failed
    target, and no rollback. fsdecode round-trips through subprocess's own fsencode on POSIX, so the
    surrogate-escaped name is handed back to git byte-identical.
    """
    try:
        p = subprocess.run(argv, cwd=str(cwd), capture_output=True, timeout=timeout)
    except FileNotFoundError:
        return 127, []
    except subprocess.TimeoutExpired:
        return 124, []
    except OSError:
        return 1, []
    if p.returncode != 0:
        return p.returncode, []
    return 0, [os.fsdecode(chunk) for chunk in p.stdout.split(b"\0") if chunk]


def _stream(argv: list[str], cwd: Path = ROOT, timeout: float = PIP_TIMEOUT,
            emit: Optional[Callable[[str], None]] = None) -> int:
    """Streaming seam: run argv, hand every output line to `emit` as it arrives, return the exit
    code. stderr is folded into stdout so pip's warnings appear in order.

    The deadline is enforced OUT OF BAND, by a watchdog thread — never from inside the read loop.
    A checked-per-line deadline is no deadline at all: the pathological case is a child that stops
    producing output entirely (a pip resolver stuck on a dead index, a git asking for credentials),
    and that child never reaches the check. A stalled step here parks a worker thread forever with
    HEAD already on the new tag, so the rollback never runs — the worst outcome this module has.

    The child gets its own PROCESS GROUP (`start_new_session`) and the watchdog kills the GROUP.
    Killing only the direct child does not bound anything for the one command that matters: every
    descendant inherited the stdout pipe, so the write end stays open and `for line in p.stdout`
    keeps blocking with the child already dead. `pip install -e .` always has descendants — PEP 517
    runs the build backend in a subprocess, which in turn runs compilers and git — so a direct-child
    kill would leave exactly the stall this watchdog exists to end.
    """
    say = emit or (lambda _line: None)
    say("$ " + " ".join(argv))
    try:
        # start_new_session is POSIX-only and silently ignored on Windows, where the fallback below
        # (kill the child alone) is all the stdlib offers without a job object.
        p = subprocess.Popen(argv, cwd=str(cwd), stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, bufsize=1,
                             start_new_session=True)
    except FileNotFoundError:
        say(f"{argv[0]}: not found")
        return 127
    except OSError as e:
        say(str(e))
        return 1

    done = threading.Event()
    timed_out = threading.Event()

    def _watchdog() -> None:
        if not done.wait(timeout):
            # `done` being unset does NOT prove the child is still unreaped: the wait can return
            # False microseconds before the main thread's p.wait() reaps it, and killpg would then
            # signal whatever process group has since inherited that pgid. So ask the process. A
            # child that has already exited is not a timeout at all — the read loop is about to end
            # and return its real exit code. (poll() is safe from this thread: Popen serialises
            # reaping on its own _waitpid_lock.)
            #
            # This narrows the recycled-pgid window to the gap between the poll and the signal; it
            # does not close it, and nothing available here would. The cost is real and named: if
            # the leader has exited while a DESCENDANT still holds the stdout pipe, this returns
            # without killing the group and the read loop stays blocked.
            if p.poll() is not None:
                return
            timed_out.set()
            try:
                # p.pid IS the group id: start_new_session made the child a session/group leader, so
                # this needs no getpgid round-trip that could race.
                if hasattr(os, "killpg"):
                    os.killpg(p.pid, signal.SIGKILL)
                else:
                    p.kill()
            except Exception:                       # noqa: BLE001 - already gone is fine
                pass

    watchdog = threading.Thread(target=_watchdog, name="updater-watchdog", daemon=True)
    watchdog.start()
    try:
        assert p.stdout is not None
        for line in p.stdout:
            say(_ANSI_RE.sub("", line.rstrip("\n")))
        rc = p.wait()                               # reap: no zombie left behind
    finally:
        done.set()
        watchdog.join(timeout=5.0)
        if p.stdout is not None:
            try:
                p.stdout.close()                    # close the pipe: no leaked fd
            except Exception:                       # noqa: BLE001
                pass
    if timed_out.is_set():
        say(f"timed out after {timeout:.0f}s")
        return 124
    return rc


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
        # Refusing is the CORRECT answer here, and the message has to say why in a way that fits
        # the install someone actually has: an instance provisioned by copying files (rsync, scp, a
        # container image, an unpacked archive) has no .git, and there is no update this button
        # could perform on it. It is updated at its source and re-deployed — not from in here.
        blockers.append(_b("not_a_checkout",
            f"This install is not a git checkout — there is no .git directory in {clone_dir}, so "
            f"there are no releases to move between and nothing here can be updated in place. If "
            f"this instance was deployed by copying files (rsync, scp, a container image), update "
            f"it where it is built and deploy it again. To make an instance that CAN update itself, "
            f"install it with install.sh, which clones the repository."))
        return {"ok": False, "current": current, "target": None,
                "update_available": False, "blockers": blockers}

    rc_o, origins, _ = _run(["git", "remote"], clone_dir)
    has_origin = rc_o == 0 and "origin" in origins.split()
    if not has_origin:
        blockers.append(_b("no_origin",
            "This checkout has no 'origin' remote, so there is nowhere to fetch new releases from. "
            "Add one with: git remote add origin <url>"))

    # TRACKED files only (`--untracked-files=no`). Untracked files are not at risk — `git checkout`
    # never touches them — and listing them here made this blocker fire on EVERY LIVE INSTANCE:
    # sessions.db is opened in WAL mode, so `sessions.db-wal` / `sessions.db-shm` sit in the repo
    # root whenever Argus is running. That is the whole update button, refusing itself forever.
    # (The sidecars are gitignored too now, but -uno is the structural fix: the question this
    # blocker asks is "would updating overwrite work you have not committed?", and only tracked
    # modifications can answer yes.)
    rc_s, porcelain, _ = _run(["git", "status", "--porcelain", "--untracked-files=no"], clone_dir)
    if rc_s == 0 and porcelain.strip():
        # "XY path" — split on whitespace rather than slicing [3:], because _run() strips the
        # leading space of a " M path" status line and a fixed offset would eat the filename.
        names = [ln.strip().split(None, 1)[-1] for ln in porcelain.splitlines() if ln.strip()]
        shown = ", ".join(names[:8]) + (f", and {len(names) - 8} more" if len(names) > 8 else "")
        blockers.append(_b("dirty_tree",
            f"The working tree has uncommitted changes to files that are part of the release "
            f"({shown}). Updating would overwrite them — commit, stash or discard them first. "
            f"(Only tracked files are listed here: your .env, databases, workspaces and "
            f"connections are gitignored, and new files you have added are left alone.)"))

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
        # start_new_session detaches the helper from THIS process (own session/pgid), so our death
        # does not take it down with us as a child. It does NOT survive the unit stopping — it stays
        # in the unit's cgroup and is killed along with it. That is fine: `systemctl restart`
        # enqueues the job with systemd before we die, and systemd, not the helper, carries it out.
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


class UpdateBusy(RuntimeError):
    """An update or revert is already running. Raised only by `exclusive()`."""


BUSY_DETAIL = ("Another update is already running. Wait for it to finish — running two at once can "
               "leave this install half-installed.")

# Exclusion lives in the EVENT LOOP, not on the filesystem. Argus is one process: uvicorn and the
# Telegram bot share a single asyncio loop (main.py), and nothing outside that process calls
# apply_update — so an asyncio.Lock taken before the blocking work is handed to a worker thread is
# correct by construction. There is no create-then-write window, no staleness to guess at, no
# ownership to verify on release, and nothing left on disk when the process dies. A lock FILE had a
# hole in every one of those, and each patch for one would have added machinery to a mechanism this
# process does not need.
#
# Two `pip install -e .` runs into one venv can leave site-packages half-written, and two
# apply_update()s interleaving their write_state() calls can clobber `from_ref` — the only recorded
# way back. Dashboard and Telegram are separate entry points into the same install, so the exclusion
# itself is still needed; only its implementation was over-built.
_EXCLUSIVE = asyncio.Lock()


# The other half of the exclusion, and the converse of the lock: a restart that has been DECIDED but
# has not happened yet. /update/restart answers the HTTP request first and replaces the process ~0.6s
# later, and the update lock is not held across that gap — so an apply started from Telegram inside
# it takes the lock legitimately and is then killed mid-pip, which is the precise way to brick an
# install. In-process, so there is nothing to clean up on disk.
#
# IT EXPIRES, and the reason is the whole point of this feature. "Only ever set by a process about to
# stop existing" is FALSE: `perform_restart` for systemd is a fire-and-forget Popen whose exit code
# is never read, so a systemctl that fails, is masked, or no-ops leaves this process alive with the
# flag up; Popen itself can raise OSError on fork failure under memory pressure, which is exactly the
# state a machine is in right after a pip install; and /update/restart's own stand-down path already
# proves a marked restart can decline to happen. A stuck flag refuses EVERY future apply and revert
# with "try again once it is back" from a process that is never coming back — curable only from the
# command line, which is the one outcome this button exists to avoid. So the flag is a timestamp, and
# a restart that has not replaced this process within the window did not happen.
_RESTART_PENDING: Optional[float] = None            # monotonic mark, or None/False for "not pending"

# Generous against a slow systemd handoff (the process is normally dead in well under a second) and
# still short enough that a wedged flag clears itself long before anyone reaches for a terminal.
#
# IT MUST NOT BE 90. `engine/service.py` renders the unit with no TimeoutStopSec, so systemd uses
# DefaultTimeoutStopSec — 90s on every mainstream distro — and a `systemctl restart` whose stop hangs
# (uvicorn's graceful shutdown waiting on open SSE streams, with no timeout_graceful_shutdown set)
# gets SIGKILLed at mark+90s while this process is still serving requests. A 90s TTL put the flag's
# expiry on exactly that instant: an apply accepted in the ~0.6s before the kill takes the lock,
# starts pip, and dies with HEAD already moved — the precise brick this flag exists to prevent. Any
# value well under the kill deadline bounds a stuck flag just as well with no overlap at all.
RESTART_PENDING_TTL = 20.0

RESTART_PENDING_DETAIL = ("Argus is restarting right now — starting an update in the moment before "
                          "the process is replaced would kill it halfway through. Try again once it "
                          "is back.")


def restart_pending() -> bool:
    # `is None or is False`, never `if not at`: a mark of 0.0 is a real timestamp (time.monotonic()
    # is only guaranteed monotonic, not large — a freshly booted machine can hand out 0.0), and
    # `not at` read it as "no restart pending", dropping the fence at the one moment it is needed.
    # False is accepted alongside None because that is what the suite pins the flag to.
    at = _RESTART_PENDING
    if at is None or at is False:
        return False
    return (time.monotonic() - float(at)) < RESTART_PENDING_TTL


def mark_restart_pending() -> None:
    global _RESTART_PENDING
    _RESTART_PENDING = time.monotonic()


def clear_restart_pending() -> None:
    """For the caller that decided NOT to go through with the restart after all — and for the one
    whose restart RAISED, which leaves this process alive and owing the next update an answer."""
    global _RESTART_PENDING
    _RESTART_PENDING = None


def update_in_progress() -> bool:
    """Is an update or revert running RIGHT NOW? The only honest answer to "would restarting kill an
    install halfway through" — the state file cannot answer it, because a process that died
    mid-apply leaves "applying" written there forever."""
    return _EXCLUSIVE.locked()


@contextlib.asynccontextmanager
async def exclusive() -> AsyncIterator[None]:
    """Hold the update lock, or raise UpdateBusy immediately.

    Deliberately never queues: a second update that waited its turn would then run against a tree
    the first one has already moved, deciding what to do from a preflight taken before that. Busy is
    the correct answer, not a delay. (The check and the acquire cannot race — asyncio.Lock.acquire
    on a free lock returns without yielding to the loop, and there is only one loop.)
    """
    if _EXCLUSIVE.locked():
        raise UpdateBusy(BUSY_DETAIL)
    async with _EXCLUSIVE:
        yield


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
    if st.get("state") == "reverted":
        # Already back on the previous ref — whether by hand, by `revert()`, or by the automatic
        # rollback inside a failed apply. The record is kept (it is what the failure report quotes),
        # but offering "Revert to v0.13.0" while running v0.13.0 is a lie.
        return False, (f"This install has already been put back on "
                       f"{st.get('from_tag') or st.get('from_ref') or 'the previous release'} — "
                       f"there is nothing further to revert.")
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


def _tree_is_clean(clone_dir: Path) -> tuple[bool, str]:
    """Does every TRACKED file match what git thinks is checked out? (`--untracked-files=no`, so the
    user's untracked and ignored state is not the subject.) Empty output is the only pass."""
    rc, porcelain, err = _run(["git", "status", "--porcelain", "--untracked-files=no"], clone_dir)
    if rc != 0:
        return False, f"could not read the state of the tree: {err or f'git status exit {rc}'}"
    if not porcelain.strip():
        return True, ""
    names = [ln.strip().split(None, 1)[-1] for ln in porcelain.splitlines() if ln.strip()]
    shown = ", ".join(names[:8]) + (f", and {len(names) - 8} more" if len(names) > 8 else "")
    return False, f"{len(names)} file(s) do not match the checked-out release ({shown})"


def _stash_ref(clone_dir: Path) -> str:
    """The sha refs/stash points at, or "" when this clone has no stash at all. Compared either
    side of the push because `git stash push` EXITS 0 on a clean tree without creating anything
    ("No local changes to save") — the return code alone cannot say whether a stash exists."""
    rc, out, _ = _run(["git", "rev-parse", "--verify", "--quiet", "refs/stash"], clone_dir)
    return out.strip() if rc == 0 else ""


# The commands that actually get a file back out of one of these stashes. `git stash show -p` and
# `git stash pop` — the obvious pair — are both wrong for what this saves:
#   * `git stash show -p "stash@{0}"` prints NOTHING and exits 0 for an untracked-only stash, so a
#     user reasonably concludes their entry is empty. `--include-untracked` makes it show them.
#   * `git stash pop` FAILS in the exact shape this mechanism exists for — "<path> already exists,
#     no checkout / error: could not restore untracked files" — because the rollback checkout has
#     since put the release's own copy back at that path. Checking one path out of the stash commit
#     works: `stash@{0}` for a file that was tracked, `stash@{0}^3` for one that was not.
#
# THE INDEX IS A PARAMETER, never a hardcoded 0. This can create TWO entries, and git pushes onto the
# top: the SECOND one made is `stash@{0}` and the first is `stash@{1}`. Every message used to name
# `stash@{0}` for both, so a user whose files were in the first entry ran the advertised command,
# saw only the other entry's contents, and concluded the rest was gone — an advertised recovery that
# does not recover, which is the failure mode this whole block exists to close.
def _recovery_commands(index: int = 0) -> str:
    e = f"stash@{{{index}}}"
    return (f'git stash list  /  git stash show -p --include-untracked "{e}"  /  '
            f'git checkout "{e}" -- <path>   (for a file that was untracked or '
            f'ignored: git checkout "{e}^3" -- <path>)')


RECOVERY_COMMANDS = _recovery_commands(0)


def _recovery_hint(names: list[str]) -> str:
    """The recovery commands for EVERY entry this made, each addressed by its own index."""
    if len(names) <= 1:
        return RECOVERY_COMMANDS
    return "  /  ".join(_recovery_commands(_stash_index(names, i)) for i in range(len(names)))


def _stash_index(names: list[str], position: int) -> int:
    """Where the entry created `position`-th ends up in `git stash list`. Newest first, so the last
    one created is 0."""
    return len(names) - 1 - position


def _stash_label(names: list[str]) -> str:
    """The `stash` field both UIs render: every entry with the index that actually addresses it."""
    return ", ".join(f"{n} (stash@{{{_stash_index(names, i)}}})" for i, n in enumerate(names))


# `_tree_is_clean` has always bounded its file list at 8; the at-risk list did not, and it is joined
# into a log line, into `detail`, and into a Telegram message body that is not split at 4096 chars.
# One release that stops shipping a populated directory is enough to make that unbounded.
_SHOW_MAX = 8


def _shown(names: list[str]) -> str:
    extra = len(names) - _SHOW_MAX
    return ", ".join(names[:_SHOW_MAX]) + (f", and {extra} more" if extra > 0 else "")


def _once_shipped_paths(clone_dir: Path, from_ref: str) -> list[str]:
    """Paths `from_ref` TRACKS that HEAD does not, and where something exists on disk right now.

    This is the one shape `--include-untracked` cannot save, and it is a real release pattern: a
    version turns a formerly-shipped file into a gitignored runtime path. `git checkout <from_ref>`
    then writes the old release's copy over whatever is there — .gitignore does not defend a path the
    target commit tracks — so the rollback destroys live data, silently, and the stash above never
    saw it because the file is ignored.

    Deliberately NOT "did .gitignore change between the two refs". Measured over this repo's own 23
    adjacent tag transitions, .gitignore changed in 8 of them (~35%) for reasons with nothing to do
    with this hazard, so refusing the rollback on that signal would push roughly one failed update in
    three to the command line — a worse outcome than the bug. This predicate names the paths actually
    at risk, which is what lets the caller save exactly those and nothing else.
    """
    rc_old, old = _run_z(["git", "ls-tree", "-r", "--name-only", "-z", from_ref], clone_dir)
    rc_now, now = _run_z(["git", "ls-files", "-z"], clone_dir)
    if rc_old != 0 or rc_now != 0:
        return []                               # can't tell — the general stash is what we have
    # -z through `_run_z`, NOT `_run`: git emits raw bytes-as-written and never quotes a path for
    # spaces or non-ASCII, and those are precisely the paths a naive split — or `_run`'s .strip() —
    # would mangle into a pathspec that matches nothing.
    tracked_now = set(now)
    at_risk = []
    for p in old:
        if p in tracked_now:
            continue
        here = clone_dir / p
        if here.exists() or here.is_symlink():
            at_risk.append(p)
    return sorted(at_risk)


def _stash_working_tree(clone_dir: Path, name: str, from_ref: str,
                        log: Callable[[str], None]) -> tuple[bool, list[str], str, list[str]]:
    """Hand the working tree to git before the rollback checkout.

    Returns (ok, names, error, ignored_paths). `ok` false means the tree could NOT be fully preserved
    and the caller must not check anything out — see rollback(). `names` is every stash entry this
    created, IN CREATION ORDER, and it can be non-empty even when ok is false (see below). `names`
    empty with ok true means there was nothing to save.

    Git, not a hand-rolled copy, because git is what already handles a path that swapped between
    file and directory, paths it quotes for spaces or non-ASCII, symlinks, and modes — and because
    `git stash list` is an interface a user can be pointed at. It is also what lets the rollback use
    a PLAIN checkout: the stash leaves the tree clean, so there is nothing left to force past.

    TWO pushes, because one cannot cover both halves. The first is `--include-untracked`, which saves
    everything except IGNORED files; plain `--all` is not an option, since with no pathspec it would
    strip .env, every *.db and .venv/ out of a live install. The second is `--all` RESTRICTED BY
    PATHSPEC to the once-shipped paths — the pathspec is the entire reason `--all` is safe there, and
    it was verified to save an ignored file at an at-risk path while leaving .env, sessions.db and
    .venv/ completely untouched. Two entries rather than one, with distinct names, because git offers
    no way to add to a stash already made and identically-named entries cannot be told apart in
    `git stash list`.

    A NON-ZERO EXIT DOES NOT MEAN NOTHING HAPPENED. Real git writes the stash, deletes the files, and
    THEN reports a failure it hit while removing one of them ("warning: failed to remove x: Permission
    denied", rc=1) — a root-owned leftover from a past sudo, a read-only mount, ENOSPC, an NFS
    silly-rename. So the stash ref is re-read on the failure path too: the caller still must not check
    anything out, but it must be told the entry exists and where, because the user's files are in it.
    """
    def _push(argv: list[str]) -> tuple[int, str, bool]:
        before = _stash_ref(clone_dir)
        rc, out, err = _run(argv, clone_dir, timeout=120.0)
        after = _stash_ref(clone_dir)
        # Compared either side rather than trusting rc: `git stash push` EXITS 0 on a clean tree
        # without creating anything ("No local changes to save"), and exits NON-ZERO in cases where
        # it did create something. The ref is the only honest witness.
        return rc, (err or out or f"{' '.join(argv[1:3])} exited {rc}"), bool(after) and after != before

    names: list[str] = []
    rc, err, created = _push(["git", "stash", "push", "--include-untracked", "-m", name])
    if created:
        names.append(name)
    if rc != 0:
        return False, names, err, []

    # Anything the first push saved is gone from disk, so this sees only what it could not: the
    # ignored files sitting at paths the ref we are about to check out still ships.
    at_risk = _once_shipped_paths(clone_dir, from_ref)
    ignored_name = f"{name}-ignored"
    ignored_created = False
    if at_risk:
        # `:(literal)` on every path. A git pathspec is a wildmatch PATTERN, not a filename, and
        # these paths come from a git tree — whatever characters a release happened to ship. An
        # at-risk path containing `*` also swept unrelated neighbours into the stash (verified with
        # real git 2.43: `star*file.txt` took starSECRETfile.txt with it), `[`/`?` are pattern
        # syntax whose meaning is not the filename's, and a leading `:` parses as pathspec magic
        # rather than as a name at all.
        rc, err, ignored_created = _push(["git", "stash", "push", "--all", "-m", ignored_name,
                                          "--", *(f":(literal){p}" for p in at_risk)])
        if ignored_created:
            names.append(ignored_name)
        if rc != 0:
            return False, names, err, at_risk

    # LOGGED ONLY NOW, because until the second push has (or has not) happened the index that
    # addresses the first entry is not known: one entry makes it stash@{0}, two demotes it to
    # stash@{1}. Naming an index that moved under the user is how the advertised recovery stopped
    # recovering.
    if name in names:
        i = _stash_index(names, names.index(name))
        log(f'the tree does not match the release, so your version of it was saved to the git stash '
            f'as "{name}" (stash@{{{i}}}) before putting the release back — nothing has been '
            f'discarded. Recover it with: {_recovery_commands(i)}')
    if not ignored_created:
        return True, names, "", []              # nothing there after all (an empty directory)
    j = _stash_index(names, names.index(ignored_name))
    log(f'{from_ref} ships {_shown(at_risk)}, which this release does not — putting it back would '
        f'write the old release\'s copy over what is there now, ignored or not. Saved to the git '
        f'stash as "{ignored_name}" (stash@{{{j}}}). Recover it with: {_recovery_commands(j)}')
    return True, names, "", at_risk


def _verify(clone_dir: Path, target: str) -> tuple[bool, str]:
    """Did the checkout actually land — all of it?

    HEAD and the root pyproject version are NOT sufficient, because `git checkout` reports success
    (exit 0) on a PARTIAL checkout: if it cannot write some files — a read-only directory left by a
    past run, ENOSPC, a file held open — it prints "unable to create file", moves HEAD anyway, and
    returns 0. The result is a tree that is a mix of the old and the new release, with both of the
    cheap checks passing. The clean-tree assertion is the one that catches it: every file git could
    not write shows up as a modification against the new index.

    It CANNOT say why the tree is dirty, and must not pretend to. A file the maintainer edited during
    the multi-minute pip step looks identical to one git failed to write (a checkout carries such an
    edit across when the path is unchanged between the two refs, and exits 0). Both are reported the
    same way, and the rollback preserves either.
    """
    _, head, _ = _run(["git", "rev-parse", "HEAD"], clone_dir)
    _, want, _ = _run(["git", "rev-parse", f"{target}^{{commit}}"], clone_dir)
    if not head or head != want:
        return False, f"HEAD is {head or '?'} but {target} is {want or '?'}"
    version = _pyproject_version(clone_dir)
    expected = target[1:] if target.startswith("v") else target
    if version != expected:
        return False, f"pyproject.toml reports {version or '?'}, expected {expected}"
    clean, why = _tree_is_clean(clone_dir)
    if not clean:
        return False, (f"the checkout of {target} is incomplete — {why}. Either git could not write "
                       f"them, or they were edited while the update was running.")
    return True, f"HEAD at {target}, pyproject reports {version}, tree matches the release"


def _busy(say: Emit, detail: str) -> dict:
    """The refusal an already-running update gets. Shaped exactly like every other `done` event so
    both callers (SSE and Telegram) report it through their normal failure path."""
    result = {"ok": False, "state": "busy", "failed_step": "lock", "detail": detail,
              "restart": None, "commands": []}
    say({"type": "done", **result})
    return result


async def apply_update_async(target: str, clone_dir: Path = ROOT,
                             emit: Optional[Emit] = None) -> dict:
    """THE entry point for both callers (the /update/apply route and /update in Telegram).

    Holds the exclusion in the event loop and only then hands the blocking git+pip work to a worker
    thread. `emit` is therefore called FROM that thread — every caller already crosses back to the
    loop itself (call_soon_threadsafe in the SSE bridge, a plain list append in the bot)."""
    say: Emit = emit or (lambda _ev: None)
    if restart_pending():
        # The converse of the lock. Taking it here would be legitimate and still fatal: the restart
        # is already scheduled and will land in the middle of pip.
        return _busy(say, RESTART_PENDING_DETAIL)
    try:
        async with exclusive():
            return await asyncio.to_thread(apply_update, target, clone_dir, emit)
    except UpdateBusy as e:
        return _busy(say, str(e))


async def revert_async(clone_dir: Path = ROOT, emit: Optional[Emit] = None) -> dict:
    """revert() under the same exclusion as apply_update_async — a revert racing an apply is two pip
    installs into one venv and two writers of the state file."""
    say: Emit = emit or (lambda _ev: None)
    if restart_pending():
        return _busy(say, RESTART_PENDING_DETAIL)
    try:
        async with exclusive():
            return await asyncio.to_thread(revert, clone_dir, emit)
    except UpdateBusy as e:
        return _busy(say, str(e))


def apply_update(target: str, clone_dir: Path = ROOT, emit: Optional[Emit] = None) -> dict:
    """Move this clone to `target` and reinstall. Emits {type: step|log|done} events. BLOCKING —
    call it through apply_update_async, which is what holds the exclusion.

    Nothing here cleans, resets or deletes anything: the only mutating git command is
    `git checkout <tag>`, which writes only the paths the target tracks — none of which is user
    state (see the module docstring).

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
               restart: Optional[dict], commands: Optional[list[str]] = None,
               stash: Optional[str] = None) -> dict:
        # `stash` is written only when there IS one. Writing None on every success overwrote the name
        # recorded by an earlier FAILED update — the record of where those files went — with nothing.
        write_state(clone_dir, state=state, failed_step=failed_step,
                    **({"stash": stash} if stash else {}))
        result = {"ok": ok, "state": state, "failed_step": failed_step, "detail": detail,
                  "from_tag": from_tag, "from_ref": from_ref, "to_tag": target,
                  # `stash` is a FIELD, not a line buried in `detail`: the dashboard card and the
                  # Telegram reply both render it, because a user who never opens a terminal is the
                  # one this has to reach.
                  "stash": stash,
                  "restart": restart, "revert_command": revert_command(clone_dir, from_ref),
                  "commands": commands or []}
        say({"type": "done", **result})
        return result

    def rollback(failed_step: str, detail: str) -> dict:
        step("rollback", f"{failed_step} failed — putting this install back on {from_ref}")
        log(detail)
        # PRESERVE FIRST, always. The tree being dirty is the normal reason we are here, and it does
        # NOT mean git wrote it: a maintainer editing a file during the multi-minute pip step lands
        # in exactly this branch, and that edit is theirs. `git stash push --include-untracked` puts
        # the whole tree somewhere git can give it back, and leaves it clean enough that the
        # checkout below needs no --force.
        stash_name = f"argus-update-{from_ref}-{target}"
        ok_stash, stash_names, stash_err, at_risk = _stash_working_tree(clone_dir, stash_name,
                                                                       from_ref, log)
        stash = _stash_label(stash_names) or None
        if not ok_stash:
            # FAIL SAFE. A rollback that cannot preserve the tree must not run: checking out over
            # unsaved work is the one outcome worse than staying broken. HEAD is untouched.
            log(f"COULD NOT SAVE THE WORKING TREE: {stash_err}")
            if stash:
                # ...but git may have got PART of the way: written the stash, removed the files, and
                # only then hit what it reported. Saying "nothing here has been changed" while a
                # file is gone from disk is worse than saying nothing, and the entry has to be named
                # or nobody can find what is in it.
                log(f'a PARTIAL stash WAS created as "{stash}" — some files may already have been '
                    f'removed from disk. Look there FIRST: {_recovery_hint(stash_names)}')
                log(f"the rollback was NOT run — HEAD is still on {target}")
                return finish(False, "needs_manual", failed_step,
                              f"{detail} — and saving the working tree failed part-way through "
                              f"(git stash push failed: {stash_err}), so the rollback was NOT run "
                              f"and HEAD is still on {target}. A PARTIAL stash was created as "
                              f'"{stash}": git may already have removed files from disk before it '
                              f"reported that failure, so check that stash before anything else.",
                              None,
                              # NOT another `git stash push -m "<same name>"`: re-running it would
                              # make a SECOND entry with an identical name, and `git stash list`
                              # could then no longer tell the user which one holds their files.
                              commands=[f'cd "{clone_dir}" && git stash list',
                                        *[f'cd "{clone_dir}" && git stash show -p '
                                          f'--include-untracked "stash@{{{_stash_index(stash_names, i)}}}"'
                                          for i in range(len(stash_names))],
                                        revert_command(clone_dir, from_ref)],
                              stash=stash)
            log("the rollback was NOT run — nothing on disk has been changed")
            return finish(False, "needs_manual", failed_step,
                          f"{detail} — and the working tree could not be saved first "
                          f"(git stash push failed: {stash_err}), so the rollback was NOT run and "
                          f"nothing here has been changed. HEAD is still on {target}.", None,
                          commands=[f'cd "{clone_dir}" && git stash push --include-untracked '
                                    f'-m "{stash_name}"',
                                    revert_command(clone_dir, from_ref)])
        kept = (f' Your version of the files it replaced was saved to the git stash as '
                f'"{stash}" — see `git stash list`.') if stash else ""
        if at_risk:
            # Named, not merely stashed: these are paths {from_ref} SHIPS and this release does not,
            # so what is at them now is the user's runtime data and the rollback would have written
            # the old release's copy straight over it.
            kept += (f' That includes {_shown(at_risk)}, which {from_ref} ships as part of the '
                     f'release — the rollback puts the release\'s own copy back there.')
        # A PLAIN checkout. The stash above already emptied the tree of anything git would refuse to
        # overwrite, so --force would only add the power to destroy something without saying so.
        rc_co = _stream(["git", "-c", "advice.detachedHead=false", "checkout", "-q", from_ref],
                        clone_dir, 120.0, log)
        rc_pip = _stream(_pip_argv(), clone_dir, PIP_TIMEOUT, log) if rc_co == 0 else 1
        clean, why = _tree_is_clean(clone_dir) if rc_co == 0 else (False, "the checkout failed")
        if not clean:
            log(f"the tree is still not what {from_ref} says it should be: {why}")
        if rc_co == 0 and rc_pip == 0 and clean:
            log(f"rolled back to {from_ref} — still running {from_tag}")
            return finish(False, "reverted", failed_step,
                          f"{detail} — rolled back to {from_tag}.{kept}", None, stash=stash)
        log("ROLLBACK FAILED — this install needs manual attention")
        return finish(False, "needs_manual", failed_step,
                      f"{detail} — and the automatic rollback also failed.{kept}", None,
                      commands=[revert_command(clone_dir, from_ref)], stash=stash)

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
    """Undo the recorded update: check the previous ref out again and reinstall. BLOCKING — call it
    through revert_async, which is what holds the exclusion.

    PRESERVES THE TREE FIRST, through the same `_stash_working_tree` the rollback uses, and does not
    check anything out if that fails. This used to be a bare `git checkout <from_ref>` on the
    reasoning that "git refuses to overwrite a local modification". Git does no such thing for a path
    the target commit TRACKS: it writes the release's copy straight over what is there, .gitignore
    and all, and exits 0. That is the hazard in its likeliest form — the user has been running the
    new release, the app has been writing runtime data at a path the OLD release shipped as a
    tracked file, and Revert silently replaces it with the shipped default. On the rollback path the
    same file usually does not exist yet; here it exists by construction.
    """
    say: Emit = emit or (lambda _ev: None)

    def log(line: str) -> None:
        say({"type": "log", "line": line})

    okc, reason = can_revert(clone_dir)
    st = read_state(clone_dir)
    from_ref = st.get("from_ref")
    from_tag = st.get("from_tag") or from_ref
    if not okc:
        result = {"ok": False, "state": st.get("state", "none"), "failed_step": "precondition",
                  "detail": reason, "restart": None, "stash": None, "commands": []}
        say({"type": "done", **result})
        return result

    # 1. preserve — before the checkout, exactly as the rollback does it.
    now_ref = current_ref(clone_dir).get("name") or st.get("to_tag") or "current"
    stash_name = f"argus-revert-{now_ref}-{from_ref}"
    say({"type": "step", "step": "preserve",
         "text": f"saving the working tree before putting {from_ref} back"})
    ok_stash, stash_names, stash_err, at_risk = _stash_working_tree(clone_dir, stash_name,
                                                                    from_ref, log)
    stash = _stash_label(stash_names) or None
    if not ok_stash:
        # FAIL SAFE, the same rule as the rollback: a way back that cannot preserve the tree must not
        # run. HEAD is untouched, so the install keeps working on the release it is on.
        log(f"COULD NOT SAVE THE WORKING TREE: {stash_err}")
        if stash:
            # git may have written the stash and removed the files before it reported the failure.
            log(f'a PARTIAL stash WAS created as "{stash}" — some files may already have been '
                f'removed from disk. Look there FIRST: {_recovery_hint(stash_names)}')
            detail = (f"the working tree could not be saved (git stash push failed: {stash_err}), so "
                      f"the revert was NOT run and HEAD is still on {now_ref}. A PARTIAL stash was "
                      f'created as "{stash}": git may already have removed files from disk before it '
                      f"reported that failure, so check that stash before anything else.")
            commands = [f'cd "{clone_dir}" && git stash list',
                        *[f'cd "{clone_dir}" && git stash show -p --include-untracked '
                          f'"stash@{{{_stash_index(stash_names, i)}}}"'
                          for i in range(len(stash_names))],
                        revert_command(clone_dir, from_ref)]
        else:
            log("the revert was NOT run — nothing on disk has been changed")
            detail = (f"the working tree could not be saved first (git stash push failed: "
                      f"{stash_err}), so the revert was NOT run and nothing here has been changed. "
                      f"HEAD is still on {now_ref}.")
            # NOT a second push under the same name: `git stash list` could not then tell them apart.
            commands = [f'cd "{clone_dir}" && git stash push --include-untracked -m "{stash_name}"',
                        revert_command(clone_dir, from_ref)]
        write_state(clone_dir, state="needs_manual", failed_step="preserve",
                    **({"stash": stash} if stash else {}))
        result = {"ok": False, "state": "needs_manual", "failed_step": "preserve", "detail": detail,
                  "restart": None, "stash": stash, "from_tag": from_tag, "commands": commands}
        say({"type": "done", **result})
        return result

    kept = (f' Your version of the files it replaced was saved to the git stash as "{stash}" — see '
            f'`git stash list`.') if stash else ""
    if at_risk:
        kept += (f' That includes {_shown(at_risk)}, which {from_ref} ships as part of the release '
                 f'— the revert puts the release\'s own copy back there.')

    # 2. checkout — PLAIN, because the stash above left the tree clean.
    say({"type": "step", "step": "checkout", "text": f"checking out {from_ref}"})
    rc = _stream(["git", "-c", "advice.detachedHead=false", "checkout", "-q", from_ref],
                 clone_dir, 120.0, log)
    if rc != 0:
        write_state(clone_dir, state="needs_manual", failed_step="checkout",
                    **({"stash": stash} if stash else {}))
        result = {"ok": False, "state": "needs_manual", "failed_step": "checkout",
                  "detail": f"git checkout {from_ref} failed (exit {rc}).{kept}", "restart": None,
                  "stash": stash, "from_tag": from_tag,
                  "commands": [revert_command(clone_dir, from_ref)]}
        say({"type": "done", **result})
        return result

    say({"type": "step", "step": "pip", "text": "reinstalling dependencies"})
    rc = _stream(_pip_argv(), clone_dir, PIP_TIMEOUT, log)
    if rc != 0:
        write_state(clone_dir, state="needs_manual", failed_step="pip",
                    **({"stash": stash} if stash else {}))
        result = {"ok": False, "state": "needs_manual", "failed_step": "pip",
                  "detail": f"pip install -e . failed (exit {rc}).{kept}", "restart": None,
                  "stash": stash, "from_tag": from_tag,
                  "commands": [revert_command(clone_dir, from_ref)]}
        say({"type": "done", **result})
        return result

    write_state(clone_dir, state="reverted", failed_step=None, to_tag=from_tag,
                **({"stash": stash} if stash else {}))
    result = {"ok": True, "state": "reverted", "failed_step": None,
              "detail": f"reverted to {from_tag}.{kept}", "restart": restart_strategy(clone_dir),
              # A FIELD, not prose: a successful revert is the COMMON case for this stash, and both
              # UIs render it from here. Burying it in `detail` is how it reached nobody.
              "stash": stash,
              "from_tag": from_tag, "commands": []}
    say({"type": "done", **result})
    return result
