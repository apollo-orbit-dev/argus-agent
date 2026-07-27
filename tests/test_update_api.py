"""backend/app.py — the /update/* routes.

Nothing here runs git or pip: every test drives the endpoints with `engine.updater` stubbed, so
what is under test is the HTTP contract — the admin gate, the server-side re-check, the SSE framing,
and (the important one) that /update/restart answers BEFORE the process is replaced.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from backend.app import create_app
from config import Config
from engine.engine import Engine


def _client(tmp_path, admin_token=""):
    cfg = Config(model_base_url="http://x/v1", model_name="main", telegram_bot_token="",
                 admin_token=admin_token)
    eng = Engine(cfg, env_path=str(tmp_path / ".env"))
    eng._system_prompt_file = tmp_path / "sp.txt"
    app = create_app(eng)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


def _preview(**over):
    base = {
        "current": "0.1.0", "current_ref": {"kind": "detached", "name": "v0.1.0", "sha": "abc",
                                            "tag": "v0.1.0"},
        "target": "v0.2.0", "update_available": True, "ok": True,
        "changelog": "## 0.2.0\n\nStuff.", "changelog_truncated": False, "changelog_note": None,
        "branch_note": None, "clone_dir": "/opt/argus",
        "restart": {"strategy": "exec", "unit": None, "instruction": "re-exec"},
        "revert_command": "cd /opt/argus && git checkout v0.1.0 && pip install -e .",
        "blockers": [],
    }
    base.update(over)
    return base


def _preflight(**over):
    base = {"ok": True, "current": "0.1.0", "target": "v0.2.0", "update_available": True,
            "blockers": []}
    base.update(over)
    return base


@pytest.fixture
def upd(monkeypatch, tmp_path):
    """The updater module with every side-effecting entry point neutralised — no real git, no pip,
    and no writes into the developer's own checkout."""
    from engine import updater
    state: dict = {}
    monkeypatch.setattr(updater, "preview", lambda clone_dir=updater.ROOT: _preview())
    monkeypatch.setattr(updater, "preflight", lambda clone_dir=updater.ROOT: _preflight())
    monkeypatch.setattr(updater, "read_state", lambda clone_dir=updater.ROOT: dict(state))
    monkeypatch.setattr(updater, "write_state",
                        lambda clone_dir=updater.ROOT, **f: state.update(f) or dict(state))
    monkeypatch.setattr(updater, "restart_strategy",
                        lambda clone_dir=updater.ROOT: {"strategy": "exec", "unit": None,
                                                        "instruction": "re-exec"})
    monkeypatch.setattr(updater, "perform_restart", lambda info: state.update(restarted=True))
    monkeypatch.setattr(updater, "can_revert", lambda clone_dir=updater.ROOT: (True, ""))
    # A module-level flag set by /update/restart. monkeypatch owns it here so a test that schedules
    # a restart cannot leak "a restart is pending" into every test that runs after it.
    monkeypatch.setattr(updater, "_RESTART_PENDING", False)
    updater._test_state = state
    return updater


def _events(text: str) -> list[dict]:
    return [json.loads(chunk[len("data: "):]) for chunk in text.split("\n\n")
            if chunk.startswith("data: ")]


# --------------------------------------------------------------------------
# admin gate
# --------------------------------------------------------------------------
async def test_all_update_endpoints_are_admin_gated(tmp_path, upd):
    async with _client(tmp_path, admin_token="s3cret") as c:
        assert (await c.get("/update/preview")).status_code == 401
        assert (await c.get("/update/state")).status_code == 401
        assert (await c.post("/update/apply")).status_code == 401
        assert (await c.post("/update/restart")).status_code == 401
        assert (await c.post("/update/revert")).status_code == 401
        h = {"X-Admin-Token": "s3cret"}
        assert (await c.get("/update/preview", headers=h)).status_code == 200
        assert (await c.get("/update/state", headers=h)).status_code == 200


async def test_preview_returns_the_decision_object(tmp_path, upd):
    async with _client(tmp_path) as c:
        d = (await c.get("/update/preview")).json()
    assert d["current"] == "0.1.0" and d["target"] == "v0.2.0"
    assert d["update_available"] is True and "## 0.2.0" in d["changelog"]
    assert d["revert_command"].startswith("cd /opt/argus")


async def test_state_reports_none_when_nothing_recorded(tmp_path, upd):
    async with _client(tmp_path) as c:
        assert (await c.get("/update/state")).json() == {"state": "none"}
    upd._test_state.update(state="applied", from_tag="v0.1.0", to_tag="v0.2.0")
    async with _client(tmp_path) as c:
        d = (await c.get("/update/state")).json()
    assert d["state"] == "applied" and d["from_tag"] == "v0.1.0"


# --------------------------------------------------------------------------
# apply — the server never trusts the client
# --------------------------------------------------------------------------
async def test_apply_requires_a_target(tmp_path, upd):
    async with _client(tmp_path) as c:
        r = await c.post("/update/apply", json={})
    assert r.status_code == 400 and "target" in r.json()["detail"]


async def test_apply_rejects_a_mismatched_confirm(tmp_path, upd, monkeypatch):
    called = []
    monkeypatch.setattr(upd, "apply_update", lambda *a, **k: called.append(a))
    async with _client(tmp_path) as c:
        r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.1.9"})
    assert r.status_code == 400 and "confirm" in r.json()["detail"]
    assert called == [], "a mismatched confirmation must never reach the updater"


async def test_apply_409s_when_the_newest_tag_moved(tmp_path, upd, monkeypatch):
    called = []
    monkeypatch.setattr(upd, "apply_update", lambda *a, **k: called.append(a))
    monkeypatch.setattr(upd, "preflight", lambda clone_dir=upd.ROOT: _preflight(target="v0.3.0"))
    async with _client(tmp_path) as c:
        r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.2.0"})
    assert r.status_code == 409
    assert "newest release changed" in r.json()["detail"] and "v0.3.0" in r.json()["detail"]
    assert called == []


async def test_apply_reruns_preflight_server_side(tmp_path, upd, monkeypatch):
    """The client may have been looking at a stale, clean preview. The server re-checks, and the
    refusal that comes back is the readable one, not "update failed"."""
    called = []
    monkeypatch.setattr(upd, "apply_update", lambda *a, **k: called.append(a))
    monkeypatch.setattr(upd, "preflight", lambda clone_dir=upd.ROOT: _preflight(
        ok=False, update_available=False,
        blockers=[{"code": "dirty_tree", "severity": "error",
                   "message": "The working tree has uncommitted changes to tracked files (main.py)."}]))
    async with _client(tmp_path) as c:
        r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.2.0"})
    assert r.status_code == 409
    assert "uncommitted changes" in r.json()["detail"] and "main.py" in r.json()["detail"]
    assert called == [], "preflight must refuse BEFORE apply_update is reached"


async def test_apply_409s_when_there_is_no_update_to_apply(tmp_path, upd, monkeypatch):
    """A DOWNGRADE is reachable without a crafted request: a dashboard tab left open while the
    instance moves ahead of the newest tag still holds a live Apply button. up_to_date and
    ahead_of_tags are INFO blockers, so an errors-only check waves them through and the updater
    happily checks out an OLDER tag. The verdict field is the one that has to be honoured."""
    called = []
    monkeypatch.setattr(upd, "apply_update", lambda *a, **k: called.append(a))
    monkeypatch.setattr(upd, "preflight", lambda clone_dir=upd.ROOT: _preflight(
        ok=False, current="9.9.9", target="v0.2.0", update_available=False,
        blockers=[{"code": "ahead_of_tags", "severity": "info",
                   "message": "This checkout is ahead of every published release: it reports "
                              "v9.9.9, but the newest release tag is v0.2.0."}]))
    async with _client(tmp_path) as c:
        r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.2.0"})
    assert r.status_code == 409, "applying an older tag over a newer install is a downgrade"
    assert "ahead of every published release" in r.json()["detail"]
    assert called == [], "the updater must never be reached for a downgrade"


async def test_apply_409s_when_already_up_to_date(tmp_path, upd, monkeypatch):
    called = []
    monkeypatch.setattr(upd, "apply_update", lambda *a, **k: called.append(a))
    monkeypatch.setattr(upd, "preflight", lambda clone_dir=upd.ROOT: _preflight(
        ok=False, current="0.2.0", target="v0.2.0", update_available=False,
        blockers=[{"code": "up_to_date", "severity": "info",
                   "message": "Already up to date — running v0.2.0, and the newest release is v0.2.0."}]))
    async with _client(tmp_path) as c:
        r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.2.0"})
    assert r.status_code == 409 and "Already up to date" in r.json()["detail"]
    assert called == []


async def test_apply_streams_sse_with_a_terminal_done(tmp_path, upd, monkeypatch):
    def fake_apply(target, clone_dir=upd.ROOT, emit=None):
        emit({"type": "step", "step": "checkout", "text": f"checking out {target}"})
        emit({"type": "log", "line": "Successfully installed argus"})
        emit({"type": "done", "ok": True, "state": "applied", "failed_step": None,
              "restart": {"strategy": "exec"}, "to_tag": target})
    monkeypatch.setattr(upd, "apply_update", fake_apply)
    async with _client(tmp_path) as c:
        r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.2.0"})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["x-accel-buffering"] == "no"
    evs = _events(r.text)
    assert [e["type"] for e in evs] == ["step", "log", "done"]
    assert evs[-1]["ok"] is True and evs[-1]["state"] == "applied"


async def test_apply_crash_still_ends_the_stream_with_done(tmp_path, upd, monkeypatch):
    def boom(target, clone_dir=upd.ROOT, emit=None):
        raise RuntimeError("git exploded")
    monkeypatch.setattr(upd, "apply_update", boom)
    async with _client(tmp_path) as c:
        r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.2.0"})
    evs = _events(r.text)
    assert evs[-1]["type"] == "done" and evs[-1]["ok"] is False
    assert "git exploded" in evs[-1]["detail"]


# --------------------------------------------------------------------------
# restart — the response must be delivered BEFORE the process dies
# --------------------------------------------------------------------------
async def test_restart_returns_before_restarting(tmp_path, upd, monkeypatch):
    """THE ordering proof. The process performing the restart is the process being replaced, so if
    perform_restart ran inside the handler the client would be left holding a socket that never
    answers. Assert it has NOT been called at the moment the response lands, and only fires after."""
    calls: list = []
    monkeypatch.setattr(upd, "perform_restart", calls.append)
    async with _client(tmp_path) as c:
        r = await c.post("/update/restart")
        assert r.status_code == 200 and r.json()["restarting"] is True
        assert r.json()["strategy"] == "exec"
        assert calls == [], "the restart happened before the response was delivered"
        await asyncio.sleep(1.0)                       # longer than the handler's 0.6s handoff
        assert len(calls) == 1, "the restart never happened after the response"
        assert calls[0]["strategy"] == "exec"


async def test_restart_records_state_before_dying(tmp_path, upd, monkeypatch):
    monkeypatch.setattr(upd, "perform_restart", lambda info: None)
    async with _client(tmp_path) as c:
        await c.post("/update/restart")
    assert upd._test_state["state"] == "restarting"


async def test_restart_records_the_state_it_will_come_back_in(tmp_path, upd, monkeypatch):
    """"restarting" is transient and something has to put the real state back on boot. That settle
    can only be honest if the state being restarted INTO was recorded here: a revert restarts too,
    and calling its result "applied" makes can_revert() offer "Revert to v0.1.0" on an install
    already running v0.1.0 — the exact lie the "reverted" state exists to prevent."""
    monkeypatch.setattr(upd, "perform_restart", lambda info: None)
    upd._test_state.update(state="reverted", from_tag="v0.1.0", to_tag="v0.1.0")
    async with _client(tmp_path) as c:
        await c.post("/update/restart")
    assert upd._test_state["state"] == "restarting"
    assert upd._test_state["before_restart"] == "reverted"


async def test_restart_refuses_while_an_update_is_being_applied(tmp_path, upd, monkeypatch):
    """Nothing serialises the restart button against an update started somewhere else (Telegram,
    a second tab). Restarting mid-`pip install` kills it with HEAD already on the new tag and the
    rollback never reached — a bricked instance.

    Round 2: the gate is the update LOCK actually being held, not the word in the state file — see
    test_restart_is_not_bricked_by_an_applying_left_over_from_a_crash for why the string alone was
    both too weak and too strong. So this test now holds the real lock."""
    calls: list = []
    monkeypatch.setattr(upd, "perform_restart", calls.append)
    upd._test_state.update(state="applying", from_ref="v0.1.0", to_tag="v0.2.0")
    async with _client(tmp_path) as c:
        async with upd.exclusive():                 # an update is genuinely in flight
            r = await c.post("/update/restart")
    assert r.status_code == 409
    assert "halfway" in r.json()["detail"]
    assert upd._test_state["state"] == "applying", "the refusal must not overwrite the update state"
    await asyncio.sleep(0.9)
    assert calls == [], "no restart may be attempted while an update is in flight"


async def test_restart_stands_down_if_an_update_starts_inside_the_handoff_window(tmp_path, upd,
                                                                                 monkeypatch):
    """The refusal above is a POINT-IN-TIME read, and the restart it guards happens at least 0.6s
    later — after the response has flushed. An apply started from Telegram anywhere in that window
    takes the lock legitimately, AFTER the check, and `pip install -e .` is then killed halfway
    through writing site-packages with HEAD already on the new tag. Asking once is not a gate."""
    calls: list = []
    monkeypatch.setattr(upd, "perform_restart", calls.append)
    async with _client(tmp_path) as c:
        r = await c.post("/update/restart")
        assert r.status_code == 200 and r.json()["restarting"] is True
        assert upd._test_state["state"] == "restarting"
        async with upd.exclusive():          # an update starts inside the handoff window
            await asyncio.sleep(1.0)         # ...and is still running when the restart would fire
    assert calls == [], "the restart killed an update that started after the check"
    assert upd._test_state["state"] != "restarting", (
        "standing down must put the recorded state back, or the boot-side settle is left waiting")
    assert upd.restart_pending() is False, "a restart that stood down must unblock the next update"


async def test_a_restart_that_raises_does_not_brick_every_future_update(tmp_path, upd, monkeypatch):
    """`perform_restart` can leave this process alive. `Popen` raises OSError on a fork failure, and
    "right after a pip install" is exactly when a machine is under the memory pressure that causes
    one. The old code called it with nothing to unwind the pending flag, so the flag stayed up: every
    later apply AND revert was refused with "try again once it is back", forever, from a process that
    was never coming back. The only cure was the command line — the outcome this button exists to
    remove. Restarting is best-effort; refusing all future work is not an acceptable failure mode."""
    applied: list = []
    monkeypatch.setattr(upd, "perform_restart",
                        lambda info: (_ for _ in ()).throw(OSError("Cannot allocate memory")))
    monkeypatch.setattr(upd, "apply_update", lambda t, clone_dir=upd.ROOT, emit=None: (
        applied.append(t) or emit({"type": "done", "ok": True, "state": "applied", "to_tag": t})))
    async with _client(tmp_path) as c:
        assert (await c.post("/update/restart")).json()["restarting"] is True
        await asyncio.sleep(1.0)                 # the restart fires ~0.6s in, and blows up
        assert upd.restart_pending() is False, "a restart that never happened must not fence forever"
        assert upd._test_state["state"] != "restarting", (
            "the recorded state must be put back, or the boot-side settle waits on a boot that "
            "never comes")
        r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.2.0"})
    assert _events(r.text)[-1]["state"] == "applied"
    assert applied == ["v0.2.0"], "the install was locked out of updating from the UI for good"


async def test_an_apply_is_refused_once_a_restart_has_been_scheduled(tmp_path, upd, monkeypatch):
    """The other direction: an apply that arrives AFTER the restart was decided. The lock is free —
    the restart does not hold it — so only the pending-restart flag can refuse this."""
    monkeypatch.setattr(upd, "perform_restart", lambda info: None)
    monkeypatch.setattr(upd, "apply_update",
                        lambda *a, **k: pytest.fail("an update started into a pending restart"))
    async with _client(tmp_path) as c:
        assert (await c.post("/update/restart")).json()["restarting"] is True
        r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.2.0"})
        evs = _events(r.text)
    assert [e["type"] for e in evs] == ["done"]
    assert evs[-1]["state"] == "busy" and evs[-1]["ok"] is False
    assert "restarting" in evs[-1]["detail"]


async def test_restart_is_not_bricked_by_an_applying_left_over_from_a_crash(tmp_path, upd,
                                                                           monkeypatch):
    """state="applying" is written before the work begins and cleared only by finish(). A process
    that dies mid-apply — which a stalled pip is exactly the thing that provokes someone to arrange
    — leaves it on disk forever. Gating the restart button on that STRING then refuses it forever,
    with no affordance anywhere in the UI to clear it. The lock cannot get stuck that way: it lives
    in this process and dies with it."""
    calls: list = []
    monkeypatch.setattr(upd, "perform_restart", calls.append)
    upd._test_state.update(state="applying", from_ref="v0.1.0", to_tag="v0.2.0")
    assert upd.update_in_progress() is False, "nothing is running — the previous process died"
    async with _client(tmp_path) as c:
        r = await c.post("/update/restart")
    assert r.status_code == 200 and r.json()["restarting"] is True
    await asyncio.sleep(0.9)
    assert len(calls) == 1, "the one button that could clear this state must still work"


async def test_a_second_apply_while_one_is_running_streams_a_busy_done(tmp_path, upd, monkeypatch):
    """The user-visible half of the exclusion: the refusal arrives as a normal terminal `done`
    event on the SSE stream, so the dashboard reports it through its existing failure path rather
    than hanging or 500ing."""
    monkeypatch.setattr(upd, "apply_update", lambda *a, **k: pytest.fail("a second apply ran"))
    async with _client(tmp_path) as c:
        async with upd.exclusive():
            r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.2.0"})
            evs = _events(r.text)
    assert r.status_code == 200
    assert [e["type"] for e in evs] == ["done"]
    assert evs[-1]["state"] == "busy" and evs[-1]["ok"] is False
    assert evs[-1]["failed_step"] == "lock" and evs[-1]["restart"] is None


async def test_restart_manual_on_windows_attempts_nothing(tmp_path, upd, monkeypatch):
    calls: list = []
    monkeypatch.setattr(upd, "perform_restart", calls.append)
    monkeypatch.setattr(upd, "restart_strategy", lambda clone_dir=upd.ROOT: {
        "strategy": "manual", "unit": None, "instruction": "Stop this process and run: argus run"})
    async with _client(tmp_path) as c:
        d = (await c.post("/update/restart")).json()
    assert d["restarting"] is False and d["strategy"] == "manual"
    assert d["instruction"] == "Stop this process and run: argus run"
    await asyncio.sleep(0.9)
    assert calls == [], "manual strategy must never attempt a restart"


# --------------------------------------------------------------------------
# revert
# --------------------------------------------------------------------------
async def test_revert_requires_the_typed_confirmation(tmp_path, upd, monkeypatch):
    called = []
    monkeypatch.setattr(upd, "revert", lambda *a, **k: called.append(a))
    async with _client(tmp_path) as c:
        assert (await c.post("/update/revert", json={})).status_code == 400
        assert (await c.post("/update/revert", json={"confirm": "yes"})).status_code == 400
    assert called == []


async def test_revert_409s_when_there_is_nothing_to_revert_to(tmp_path, upd, monkeypatch):
    monkeypatch.setattr(upd, "can_revert", lambda clone_dir=upd.ROOT: (
        False, "The previous ref v0.1.0 no longer resolves in this checkout"))
    async with _client(tmp_path) as c:
        r = await c.post("/update/revert", json={"confirm": "revert"})
    assert r.status_code == 409 and "no longer resolves" in r.json()["detail"]


async def test_revert_streams_sse(tmp_path, upd, monkeypatch):
    def fake_revert(clone_dir=upd.ROOT, emit=None):
        emit({"type": "done", "ok": True, "state": "reverted", "from_tag": "v0.1.0"})
    monkeypatch.setattr(upd, "revert", fake_revert)
    async with _client(tmp_path) as c:
        r = await c.post("/update/revert", json={"confirm": "revert"})
    assert r.headers["content-type"].startswith("text/event-stream")
    assert _events(r.text)[-1]["state"] == "reverted"


# --------------------------------------------------------------------------
# the stash has to reach the person who pressed the button
# --------------------------------------------------------------------------
async def test_a_rolled_back_update_streams_the_stash_name_as_a_field(tmp_path, upd, monkeypatch):
    """`detail` is a paragraph neither UI renders in full, and a log line is one of hundreds. The
    name of the stash the rollback made is its own field on the terminal `done` event, so the card
    can show it without parsing prose."""
    def fake_apply(target, clone_dir=upd.ROOT, emit=None):
        emit({"type": "done", "ok": False, "state": "reverted", "failed_step": "verify",
              "stash": "argus-update-v0.1.0-v0.2.0", "restart": None, "commands": [],
              "detail": "verification failed — rolled back to v0.1.0."})
        return {}
    monkeypatch.setattr(upd, "apply_update", fake_apply)
    async with _client(tmp_path) as c:
        r = await c.post("/update/apply", json={"target": "v0.2.0", "confirm": "v0.2.0"})
    assert _events(r.text)[-1]["stash"] == "argus-update-v0.1.0-v0.2.0"


async def test_a_successful_revert_streams_its_stash_name_as_a_field(tmp_path, upd, monkeypatch):
    """The Revert button's own path to the user, which did not exist.

    `revert()` produced no `stash` key at all, so even once it started preserving the tree the
    dashboard had nothing to render. And this is the SUCCESS case: a revert that works is exactly
    when the stash matters, because the install has been running the new release and writing runtime
    data at a path the old release ships."""
    def fake_revert(clone_dir=upd.ROOT, emit=None):
        emit({"type": "done", "ok": True, "state": "reverted", "failed_step": None,
              "stash": "argus-revert-v0.2.0-v0.1.0-ignored (stash@{0})", "from_tag": "v0.1.0",
              "restart": {"strategy": "exec", "unit": None, "instruction": "re-exec"},
              "detail": "reverted to v0.1.0.", "commands": []})
        return {}
    monkeypatch.setattr(upd, "revert", fake_revert)
    async with _client(tmp_path) as c:
        r = await c.post("/update/revert", json={"confirm": "revert"})
    done = _events(r.text)[-1]
    assert done["ok"] is True and done["state"] == "reverted"
    assert done["stash"] == "argus-revert-v0.2.0-v0.1.0-ignored (stash@{0})"


def test_the_update_card_shows_the_stash_name_on_a_SUCCESSFUL_run_too():
    """The card only ever mentioned the stash inside `if (!result.ok)`, and that branch RETURNS. A
    successful revert — the likeliest run to have a stash — fell straight through to the restart and
    told the user nothing. (A source assertion because the dashboard has no JS test harness.)"""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "dashboard" / "app.js").read_text(encoding="utf-8")
    success_branch = js.split("async function finishUpdate", 1)[1] \
                       .split("el.innerHTML = updOk", 1)[1] \
                       .split("\n  }\n", 1)[0]
    assert "result.stash" in success_branch, "a successful revert never mentions its stash"
    assert "esc(result.stash)" in success_branch, "the stash name must reach the CARD, escaped"
    assert "git stash list" in success_branch, "and it must say how to get the files back"
    assert "--include-untracked" in success_branch, (
        'a bare `git stash show -p "stash@{0}"` prints nothing for an untracked-only entry')
    assert "git stash pop" not in success_branch, (
        "pop fails once the checkout has put the release's own copy back at that path")
    assert 'stash@{0}' not in success_branch, (
        "there can be TWO entries; the name carries each one's own index, so the commands must not "
        "hardcode 0 — that sent users to the wrong entry")


def test_the_update_card_shows_the_stash_name():
    """Round 3 MED-5: the rescue path went into `res["detail"]`, which NEITHER UI renders — so the
    one message telling a user where their work went was never displayed anywhere. A non-CLI user
    lives in this card; if it is not here, it does not exist. (A source assertion because the
    dashboard has no JS test harness — it is still the only thing that fails if the line is
    deleted.)"""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "dashboard" / "app.js").read_text(encoding="utf-8")
    failure_branch = js.split("async function finishUpdate", 1)[1] \
                       .split("if (!result.ok)", 1)[1] \
                       .split("el.innerHTML = updOk", 1)[0]
    assert "result.stash" in failure_branch, "the update card never mentions the stash"
    assert "el.innerHTML +=" in failure_branch and "esc(result.stash)" in failure_branch, (
        "the stash name must land in the CARD (escaped), not only in the scrollback pane")
    assert "git stash list" in failure_branch, "and it must say how to get the files back"
    # ...and the commands it prints have to be ones that WORK on what this actually saves.
    assert "--include-untracked" in failure_branch, (
        'a bare `git stash show -p "stash@{0}"` prints nothing at all, exit 0, for an entry holding '
        'only untracked files — the user concludes their stash is empty')
    assert "git stash pop" not in failure_branch, (
        "`git stash pop` FAILS in the exact shape this saves for — the rollback has already put the "
        "release's own copy back at that path, so it refuses with 'already exists, no checkout'")
