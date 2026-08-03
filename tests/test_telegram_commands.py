from backend.telegram_bot import (
    BOT_COMMANDS,
    build_telegram_app,
    rename_command,
    session_text,
    sessions_text,
)


def test_bot_commands_shape():
    assert BOT_COMMANDS, "at least one slash command should be registered"
    for entry in BOT_COMMANDS:
        cmd, desc = entry
        assert cmd.islower() and " " not in cmd  # valid Telegram command name
        assert 1 <= len(desc) <= 256


def test_registered_commands_have_handlers():
    # every registered slash command must have a matching CommandHandler
    class _Cfg:
        telegram_bot_token = "123:abc"
        allowed_chat_ids = [1]
    app = build_telegram_app(engine=None, config=_Cfg())
    handler_cmds = set()
    for group in app.handlers.values():
        for h in group:
            cmds = getattr(h, "commands", None)
            if cmds:
                handler_cmds.update(cmds)
    for cmd, _ in BOT_COMMANDS:
        assert cmd in handler_cmds, f"/{cmd} is advertised but has no handler"


def test_custom_command_catch_all_registered_and_hidden():
    # A MessageHandler on COMMAND must exist (the custom-alias catch-all), and it has to be the
    # LAST handler so built-in CommandHandlers claim their commands first. Custom aliases are
    # deliberately absent from BOT_COMMANDS so they don't appear in Telegram's `/` menu.
    from telegram.ext import MessageHandler
    from telegram.ext import filters as tg_filters

    class _Cfg:
        telegram_bot_token = "123:abc"
        allowed_chat_ids = [1]
    app = build_telegram_app(engine=None, config=_Cfg())
    group = app.handlers[0]
    catch_alls = [h for h in group if isinstance(h, MessageHandler) and h.filters is tg_filters.COMMAND]
    assert len(catch_alls) == 1, "expected exactly one COMMAND catch-all for custom aliases"
    assert group[-1] is catch_alls[0], "the catch-all must be registered last so built-ins win"


# --------------------------------------------------------------------------
# /sessions, /session, /rename (argus-aeu, with argus-9in folded in)
# --------------------------------------------------------------------------
def _sess(n, name=None, sid=None, updated="2026-07-24T10:15:00"):
    sid = sid or f"ses_{n:04d}"
    return {"id": sid, "name": name if name is not None else sid, "origin": "telegram",
            "updated": updated, "message_count": n}


class _Cfg:
    telegram_bot_token = "123:abc"
    allowed_chat_ids = [1]


class _FakeEngine:
    """Just enough of Engine for rename_command / the /reset alias test."""
    def __init__(self, sessions=None):
        self._sessions = sessions if sessions is not None else []
        self.renamed = []
        self.reset_calls = []
        self.new_session_calls = []

    def list_sessions(self):
        return self._sessions

    def rename_session(self, session_id, name):
        self.renamed.append((session_id, name))

    def reset(self, session_id):
        self.reset_calls.append(session_id)

    def new_session(self, session_id):
        self.new_session_calls.append(session_id)


def test_reset_advertised_new_hidden_same_callback():
    cmds = [c for c, _ in BOT_COMMANDS]
    assert "reset" in cmds
    assert "new" not in cmds
    app = build_telegram_app(engine=_FakeEngine(), config=_Cfg())
    handlers = {c: h.callback for h in app.handlers[0] for c in (getattr(h, "commands", None) or [])}
    assert "reset" in handlers and "new" in handlers
    assert handlers["reset"] is handlers["new"]


def test_sessions_text_marks_current_once_and_codes_every_id():
    sessions = [_sess(1), _sess(2), _sess(3)]
    current = sessions[1]["id"]
    text = sessions_text(sessions, current)
    assert text.count("← current") == 1
    assert text.count("<code>") == len(sessions)


def test_sessions_text_caps_at_20_but_always_keeps_current():
    sessions = [_sess(i) for i in range(25)]
    current = sessions[24]["id"]
    text = sessions_text(sessions, current)
    assert text.count("<code>") == 21
    assert "…and 4 more." in text
    assert current in text


def test_sessions_text_escapes_hostile_name():
    sessions = [_sess(1, name="<b>x</b> & co")]
    text = sessions_text(sessions, sessions[0]["id"])
    assert "&lt;b&gt;" in text
    assert "&amp;" in text
    assert "<b>" not in text


def test_no_markdownv2_escaping_of_negative_ids():
    sessions = [_sess(1, sid="-1001234567")]
    text = sessions_text(sessions, "-1001234567")
    assert "-1001234567" in text
    assert "\\" not in text
    text2 = session_text(sessions, "-1001234567")
    assert "-1001234567" in text2
    assert "\\" not in text2


def _assert_no_stray_angle_brackets(text: str) -> None:
    """Every reply here is sent with parse_mode=HTML. The only tag these helpers may emit is
    <code>...</code>; any other literal '<' (e.g. an un-escaped '<name>' placeholder) would make
    Telegram reject the message with "can't parse entities: Unsupported start tag"."""
    stripped = text.replace("<code>", "").replace("</code>", "")
    assert "<" not in stripped, f"stray unescaped '<' outside <code> tags: {text!r}"


def test_sessions_text_no_stray_html_tags():
    sessions = [_sess(1)]
    _assert_no_stray_angle_brackets(sessions_text(sessions, sessions[0]["id"]))


def test_session_text_no_stray_html_tags():
    sessions = [_sess(1)]
    _assert_no_stray_angle_brackets(session_text(sessions, sessions[0]["id"]))
    _assert_no_stray_angle_brackets(session_text([], "42"))


def test_rename_bare_no_stray_html_tags():
    eng = _FakeEngine([_sess(1, sid="42")])
    _assert_no_stray_angle_brackets(rename_command(eng, "42", []))
    eng_unknown = _FakeEngine([])
    _assert_no_stray_angle_brackets(rename_command(eng_unknown, "42", []))


def test_session_text_omits_name_when_equal_to_id_includes_when_renamed():
    same = session_text([_sess(1)], "ses_0001")
    assert same.count("ses_0001") == 1          # id shown once, no separate (identical) name line
    renamed = session_text([_sess(1, name="My Chat")], "ses_0001")
    assert "My Chat" in renamed
    assert "<code>ses_0001</code>" in renamed


def test_session_text_empty_sessions_still_shows_current_id():
    text = session_text([], "42")
    assert "<code>42</code>" in text


def test_rename_bare_shows_current_and_does_not_rename():
    eng = _FakeEngine([_sess(1, sid="42")])
    text = rename_command(eng, "42", [])
    assert eng.renamed == []
    assert "42" in text


def test_rename_sets_name():
    eng = _FakeEngine([_sess(1, sid="42")])
    text = rename_command(eng, "42", ["my", "chat"])
    assert eng.renamed == [("42", "my chat")]
    assert "my chat" in text


def test_rename_unknown_session_is_noop():
    eng = _FakeEngine([])
    text = rename_command(eng, "42", ["name"])
    assert eng.renamed == []
    assert "send me a message first" in text


def test_rename_truncates_to_80_chars():
    eng = _FakeEngine([_sess(1, sid="42")])
    long_name = "x" * 100
    rename_command(eng, "42", [long_name])
    assert eng.renamed == [("42", "x" * 80)]


async def test_on_new_reply_mentions_same_session_not_new_conversation():
    from types import SimpleNamespace as NS

    app = build_telegram_app(engine=_FakeEngine(), config=_Cfg())
    handlers = {c: h.callback for h in app.handlers[0] for c in (getattr(h, "commands", None) or [])}
    on_new = handlers["reset"]

    replies = []

    async def reply_text(text, **kw):
        replies.append(text)
    update = NS(effective_chat=NS(id=1), effective_message=NS(reply_text=reply_text))
    await on_new(update, NS(args=[]))
    assert len(replies) == 1
    assert "same session" in replies[0]
    assert "New conversation" not in replies[0]


async def test_on_new_calls_new_session_not_reset_for_both_aliases():
    # argus-nyp: /reset must clear the same things the dashboard's reset does — the working
    # set AND the event/trace buffers, i.e. engine.new_session, not the narrower engine.reset.
    # Both the /reset command and its /new alias share the same callback; check both entries.
    from types import SimpleNamespace as NS

    for alias in ("reset", "new"):
        eng = _FakeEngine()
        app = build_telegram_app(engine=eng, config=_Cfg())
        handlers = {c: h.callback for h in app.handlers[0] for c in (getattr(h, "commands", None) or [])}
        on_new = handlers[alias]

        replies = []

        async def reply_text(text, **kw):
            replies.append(text)
        update = NS(effective_chat=NS(id=1), effective_message=NS(reply_text=reply_text))
        await on_new(update, NS(args=[]))

        assert eng.new_session_calls == ["1"], f"/{alias} should call engine.new_session(chat_id)"
        assert eng.reset_calls == [], f"/{alias} must not call the narrower engine.reset"


# --------------------------------------------------------------------------
# /update (argus-rzu) — two-step by construction: /update previews, /update confirm acts.
# --------------------------------------------------------------------------
def _handlers(engine=None):
    app = build_telegram_app(engine=engine or _FakeEngine(), config=_Cfg())
    return {c: h.callback for h in app.handlers[0] for c in (getattr(h, "commands", None) or [])}


class _Msg:
    """Captures reply_text calls (reply_html goes through reply_text with parse_mode=HTML)."""
    def __init__(self):
        self.sent = []

    async def reply_text(self, text, **kw):
        self.sent.append(text)


def _upd_preview(**over):
    base = {"current": "0.1.0",
            "current_ref": {"kind": "detached", "name": "v0.1.0", "sha": "abc", "tag": "v0.1.0"},
            "target": "v0.2.0", "update_available": True, "ok": True,
            "changelog": "## 0.2.0\n\nFixed <script>alert(1)</script> & more.",
            "changelog_truncated": False, "changelog_note": None, "branch_note": None,
            "clone_dir": "/opt/argus",
            "restart": {"strategy": "exec", "unit": None, "instruction": "re-exec"},
            "revert_command": "cd /opt/argus && git checkout v0.1.0", "blockers": []}
    base.update(over)
    return base


def _stub_updater(monkeypatch, preview, apply_result=None, applied=None, log_lines=None):
    from engine import updater
    monkeypatch.setattr(updater, "preview", lambda clone_dir=updater.ROOT: preview)
    monkeypatch.setattr(updater, "write_state", lambda clone_dir=updater.ROOT, **f: dict(f))
    monkeypatch.setattr(updater, "read_state", lambda clone_dir=updater.ROOT: {})
    monkeypatch.setattr(updater, "perform_restart", lambda info: None)
    monkeypatch.setattr(updater, "_RESTART_PENDING", False)

    def fake_apply(target, clone_dir=updater.ROOT, emit=None):
        if applied is not None:
            applied.append(target)
        if emit:
            for line in (log_lines or ["Successfully installed argus"]):
                emit({"type": "log", "line": line})
        return apply_result or {"ok": True, "state": "applied", "to_tag": target,
                                "restart": {"strategy": "exec", "unit": None, "instruction": "x"}}
    monkeypatch.setattr(updater, "apply_update", fake_apply)
    return updater


def test_update_is_advertised():
    assert "update" in dict(BOT_COMMANDS)
    assert "confirm" in dict(BOT_COMMANDS)["update"]


async def test_update_without_confirm_previews_and_does_not_apply(monkeypatch):
    from types import SimpleNamespace as NS
    applied = []
    _stub_updater(monkeypatch, _upd_preview(), applied=applied)
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg), NS(args=[]))
    assert applied == [], "/update alone must never install anything"
    assert len(msg.sent) == 1
    body = msg.sent[0]
    assert "v0.1.0" in body and "v0.2.0" in body
    assert "/update confirm" in body


async def test_update_preview_escapes_the_changelog(monkeypatch):
    from types import SimpleNamespace as NS
    _stub_updater(monkeypatch, _upd_preview())
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg), NS(args=[]))
    body = msg.sent[0]
    assert "<script>" not in body, "changelog text must be HTML-escaped before it hits Telegram"
    assert "&lt;script&gt;" in body and "&amp; more" in body


async def test_update_reports_the_blocker_reason_verbatim(monkeypatch):
    from types import SimpleNamespace as NS
    applied = []
    reason = ("The working tree has uncommitted changes to tracked files (main.py). Updating "
              "would overwrite them — commit, stash or discard them first.")
    _stub_updater(monkeypatch, _upd_preview(
        update_available=False, ok=False,
        blockers=[{"code": "dirty_tree", "severity": "error", "message": reason}]), applied=applied)
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg), NS(args=[]))
    assert applied == []
    assert "main.py" in msg.sent[0] and "commit, stash or discard" in msg.sent[0]


async def test_update_up_to_date_says_so(monkeypatch):
    from types import SimpleNamespace as NS
    _stub_updater(monkeypatch, _upd_preview(
        update_available=False, target="v0.1.0",
        blockers=[{"code": "up_to_date", "severity": "info",
                   "message": "Already up to date — running v0.1.0, and the newest release is v0.1.0."}]))
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg), NS(args=[]))
    assert "Already up to date" in msg.sent[0]


async def test_update_confirm_applies_then_restarts_after_replying(monkeypatch):
    import asyncio
    from types import SimpleNamespace as NS
    applied, restarts = [], []
    upd = _stub_updater(monkeypatch, _upd_preview(), applied=applied)
    monkeypatch.setattr(upd, "perform_restart", restarts.append)
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg),
                                NS(args=["confirm"]))
    assert applied == ["v0.2.0"]
    assert any("Restarting" in s for s in msg.sent)
    assert restarts == [], "the reply must be sent before the process is replaced"
    await asyncio.sleep(0.9)
    assert len(restarts) == 1


async def test_update_confirm_fences_its_restart_window_against_a_dashboard_apply(monkeypatch):
    """The Telegram restart is a THIRD path to `perform_restart`, and it scheduled itself with
    neither `mark_restart_pending()` nor a re-check of the update lock. That made the new flag
    one-sided: `apply_update_async`'s restart_pending() guard could not see a Telegram-initiated
    restart at all, so an apply started from the dashboard inside the 0.6s handoff took the lock
    legitimately and was killed mid-`pip install` — HEAD already on the new tag, the rollback never
    reached. Half of the protection simply did not exist.

    Both halves are asserted here: the mark goes up (so a new apply is refused), and if one got in
    anyway the restart stands down rather than killing it."""
    import asyncio
    from types import SimpleNamespace as NS
    restarts: list = []
    upd = _stub_updater(monkeypatch, _upd_preview())
    monkeypatch.setattr(upd, "perform_restart", restarts.append)
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg),
                                NS(args=["confirm"]))
    assert upd.restart_pending() is True, "a new apply could start into this restart and be killed"

    async with upd.exclusive():                  # an apply gets in anyway, from the dashboard
        await asyncio.sleep(0.9)
    assert restarts == [], "the restart killed an update that started inside the handoff window"
    assert upd.restart_pending() is False, "standing down must unblock the next update"


async def test_update_confirm_records_the_state_it_will_come_back_in(monkeypatch):
    """The Telegram half of the same handoff: whatever this update turned the install INTO has to be
    written down before "restarting" replaces it, or the boot-side settle has to guess."""
    from types import SimpleNamespace as NS
    written: list = []
    upd = _stub_updater(monkeypatch, _upd_preview())
    monkeypatch.setattr(upd, "write_state", lambda clone_dir=upd.ROOT, **f: written.append(f))
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg),
                                NS(args=["confirm"]))
    restarting = [w for w in written if w.get("state") == "restarting"]
    assert restarting and restarting[-1]["before_restart"] == "applied"


async def test_update_confirm_refuses_when_preflight_blocks(monkeypatch):
    from types import SimpleNamespace as NS
    applied = []
    _stub_updater(monkeypatch, _upd_preview(
        update_available=False, ok=False,
        blockers=[{"code": "wrong_venv", "severity": "error",
                   "message": "The running Python lives in /usr, but this checkout's virtualenv is /opt/argus/.venv."}]),
        applied=applied)
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg),
                                NS(args=["confirm"]))
    assert applied == [], "confirm must re-run the preflight, not trust the earlier preview"
    assert "/opt/argus/.venv" in msg.sent[0]


async def test_update_failure_reports_rollback_and_the_way_back(monkeypatch):
    from types import SimpleNamespace as NS
    _stub_updater(monkeypatch, _upd_preview(), apply_result={
        "ok": False, "state": "reverted", "failed_step": "pip", "restart": None,
        "revert_command": "cd /opt/argus && git checkout v0.1.0", "commands": []})
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg),
                                NS(args=["confirm"]))
    body = msg.sent[-1]
    assert "rolled back" in body and "pip" in body
    assert "git checkout v0.1.0" in body
    assert "Restarting" not in body, "a failed update must not offer a restart"


async def test_update_failure_names_the_stash_outside_the_log_tail(monkeypatch):
    """Round 3 MED-5, the Telegram half. Where the rollback put the user's files was only ever a LOG
    LINE — and the reply quotes the last 20 lines, while the rollback writes that line FIRST. On any
    update that produced real output it fell off the top and the user was never told. It is now part
    of the message itself."""
    from types import SimpleNamespace as NS
    name = "argus-update-v0.1.0-v0.2.0"
    noisy = [f'saved your files to the git stash as "{name}"'] + [f"pip line {i}" for i in range(40)]
    _stub_updater(monkeypatch, _upd_preview(), log_lines=noisy, apply_result={
        "ok": False, "state": "reverted", "failed_step": "verify", "restart": None,
        "stash": name, "revert_command": "cd /opt/argus && git checkout v0.1.0", "commands": []})
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg),
                                NS(args=["confirm"]))
    body = msg.sent[-1]
    assert "pip line 39" in body and "pip line 5" not in body, "the tail is still the last 20 lines"
    assert name in body, "the user was never told where their files went"
    assert "git stash list" in body, "and never told how to get them back"
    assert "--include-untracked" in body, (
        'a bare `git stash show -p "stash@{0}"` prints nothing for an untracked-only entry')
    assert "git stash pop" not in body, (
        "pop fails once the rollback has put the release's own copy back at that path")
    assert body.index(name) < body.index("<pre>"), (
        "the stash must be named ABOVE the tail — inside it, it is the line that gets cut")


async def test_update_revert_needs_its_own_confirm(monkeypatch):
    from types import SimpleNamespace as NS
    from engine import updater
    reverted = []
    monkeypatch.setattr(updater, "can_revert", lambda clone_dir=updater.ROOT: (True, ""))
    monkeypatch.setattr(updater, "read_state",
                        lambda clone_dir=updater.ROOT: {"from_tag": "v0.1.0", "state": "applied"})
    monkeypatch.setattr(updater, "revert",
                        lambda clone_dir=updater.ROOT, emit=None: reverted.append(1) or
                        {"ok": True, "state": "reverted", "from_tag": "v0.1.0", "restart": None})
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg),
                                NS(args=["revert"]))
    assert reverted == []
    assert "/update revert confirm" in msg.sent[0] and "v0.1.0" in msg.sent[0]


async def test_update_revert_confirm_tells_the_user_where_its_stash_went(monkeypatch):
    """The Telegram half of the Revert button, and the case neither UI covered.

    `_update_finish` only mentioned `stash` inside `if not res.get("ok")`, and that branch returns.
    A revert that SUCCEEDS is the likeliest run of all to have a stash — the install has been on the
    new release for days, writing runtime data at a path the old release ships as a tracked file —
    and it said nothing at all."""
    from types import SimpleNamespace as NS
    from engine import updater
    stash = "argus-revert-v0.2.0-v0.1.0-ignored (stash@{0})"
    reverted = []
    monkeypatch.setattr(updater, "can_revert", lambda clone_dir=updater.ROOT: (True, ""))
    monkeypatch.setattr(updater, "read_state",
                        lambda clone_dir=updater.ROOT: {"from_tag": "v0.1.0", "state": "applied"})
    monkeypatch.setattr(updater, "write_state", lambda clone_dir=updater.ROOT, **f: dict(f))
    monkeypatch.setattr(updater, "perform_restart", lambda info: None)
    monkeypatch.setattr(updater, "_RESTART_PENDING", False)
    monkeypatch.setattr(updater, "revert",
                        lambda clone_dir=updater.ROOT, emit=None: reverted.append(1) or
                        {"ok": True, "state": "reverted", "failed_step": None, "stash": stash,
                         "from_tag": "v0.1.0", "detail": "reverted to v0.1.0.", "commands": [],
                         "restart": {"strategy": "exec", "unit": None, "instruction": "x"}})
    msg = _Msg()
    await _handlers()["update"](NS(effective_chat=NS(id=1), effective_message=msg),
                                NS(args=["revert", "confirm"]))
    assert reverted == [1], "/update revert confirm must actually revert"
    body = "\n".join(msg.sent)
    assert stash in body, "the user was never told where their runtime data went"
    assert "git stash list" in body, "and never told how to get it back"
    assert "--include-untracked" in body, (
        'a bare `git stash show -p "stash@{0}"` prints nothing for an untracked-only entry')
    assert "git stash pop" not in body, (
        "pop fails once the checkout has put the release's own copy back at that path")
    assert 'stash@{0}"' not in body.replace(stash, ""), (
        "there can be TWO entries and the name carries each one's own index — a hardcoded 0 sends "
        "the user to the wrong entry")


async def test_post_restart_ack_is_delivered_on_the_way_back_up(monkeypatch):
    from types import SimpleNamespace as NS

    from backend.telegram_bot import deliver_pending_update_notice
    from engine import updater
    sent, written = [], []
    monkeypatch.setattr(updater, "read_state", lambda clone_dir=updater.ROOT: {
        "state": "restarting", "pending_notice": {"chat_id": 42, "to": "v9.9.9"}})
    monkeypatch.setattr(updater, "write_state",
                        lambda clone_dir=updater.ROOT, **f: written.append(f))

    async def send_message(chat_id, text, **kw):
        sent.append((chat_id, text))
    await deliver_pending_update_notice(NS(bot=NS(send_message=send_message)))
    assert len(sent) == 1 and sent[0][0] == 42
    # The booted version is the repo's real one, which is NOT v9.9.9 — so this must WARN, not
    # claim success.
    assert "not the expected" in sent[0][1] and "v9.9.9" in sent[0][1]
    assert written and written[-1]["pending_notice"] is None


async def test_post_restart_ack_confirms_success_when_the_version_matches(monkeypatch):
    from types import SimpleNamespace as NS

    from backend.telegram_bot import deliver_pending_update_notice
    from engine import updater
    from engine.version import get_version
    sent = []
    monkeypatch.setattr(updater, "read_state", lambda clone_dir=updater.ROOT: {
        "state": "restarting", "pending_notice": {"chat_id": 7, "to": f"v{get_version()}"}})
    monkeypatch.setattr(updater, "write_state", lambda clone_dir=updater.ROOT, **f: None)

    async def send_message(chat_id, text, **kw):
        sent.append((chat_id, text))
    await deliver_pending_update_notice(NS(bot=NS(send_message=send_message)))
    assert len(sent) == 1 and "Update complete" in sent[0][1]


async def test_a_restart_with_nobody_to_notify_still_settles_the_state(monkeypatch):
    """A dashboard-initiated restart writes state="restarting" with no chat to reply to. If this
    returns early without clearing it the state stays "restarting" for good, and the next boot that
    happens to find a pending_notice delivers a bogus "Update complete" for an update that finished
    days ago."""
    from types import SimpleNamespace as NS

    from backend.telegram_bot import deliver_pending_update_notice
    from engine import updater
    sent, written = [], []
    monkeypatch.setattr(updater, "read_state", lambda clone_dir=updater.ROOT: {
        "state": "restarting", "pending_notice": None, "from_tag": "v0.1.0"})
    monkeypatch.setattr(updater, "write_state",
                        lambda clone_dir=updater.ROOT, **f: written.append(f))

    async def send_message(chat_id, text, **kw):
        sent.append(text)
    await deliver_pending_update_notice(NS(bot=NS(send_message=send_message)))
    assert sent == [], "there is nobody to tell"
    assert written and written[-1]["state"] == "applied", "the transient state must be settled"
    assert written[-1]["pending_notice"] is None


def _settle_state(monkeypatch, state: dict) -> dict:
    """Point read_state/write_state at one dict, so the settle can be watched end to end."""
    from engine import updater
    monkeypatch.setattr(updater, "read_state", lambda clone_dir=updater.ROOT: dict(state))
    monkeypatch.setattr(updater, "write_state",
                        lambda clone_dir=updater.ROOT, **f: state.update(f) or dict(state))
    monkeypatch.setattr(updater, "_resolves", lambda ref, clone_dir=updater.ROOT: True)
    return state


async def test_the_settle_carries_a_revert_forward_instead_of_calling_it_applied(monkeypatch):
    """A REVERT restarts too, and the settle used to hardcode "applied".

    The whole flow: revert() writes state="reverted"; the dashboard posts /update/restart on the
    success path, which writes "restarting"; on boot this settle found "restarting" with no chat_id
    and wrote "applied". can_revert() then no longer saw "reverted" and said yes — so the dashboard
    offered "Revert to v0.1.0" on an install already running v0.1.0. The transient state must carry
    the real one forward, not invent one."""
    from types import SimpleNamespace as NS

    from backend.telegram_bot import deliver_pending_update_notice
    from engine import updater
    state = _settle_state(monkeypatch, {"state": "restarting", "before_restart": "reverted",
                                        "pending_notice": None, "from_ref": "v0.1.0",
                                        "from_tag": "v0.1.0"})

    async def send_message(chat_id, text, **kw):
        raise AssertionError("there is nobody to tell")
    await deliver_pending_update_notice(NS(bot=NS(send_message=send_message)))

    assert state["state"] == "reverted", "the settle invented an outcome instead of restoring one"
    assert state["before_restart"] is None, "the marker must not survive into the next update"
    ok, why = updater.can_revert()
    assert ok is False and "already been put back" in why, (
        "the dashboard is being offered a revert to the release it is already running")


async def test_the_settle_carries_the_state_forward_on_the_notified_path_too(monkeypatch):
    """The same settle runs after the ✅ is sent — /update revert confirm from Telegram reaches it
    with a chat to answer."""
    from types import SimpleNamespace as NS

    from backend.telegram_bot import deliver_pending_update_notice
    from engine.version import get_version
    sent = []
    state = _settle_state(monkeypatch, {
        "state": "restarting", "before_restart": "reverted", "from_ref": "v0.1.0",
        "pending_notice": {"chat_id": 5, "to": f"v{get_version()}"}})

    async def send_message(chat_id, text, **kw):
        sent.append(text)
    await deliver_pending_update_notice(NS(bot=NS(send_message=send_message)))
    assert len(sent) == 1 and "Update complete" in sent[0]
    assert state["state"] == "reverted" and state["pending_notice"] is None


async def test_reply_html_fallback_strips_pre_too(monkeypatch):
    """The plain-text fallback exists so a Telegram parse error still lands as clean text. /update
    wraps the changelog and the pip tail in <pre>, so a fallback that only strips code/b/i shows
    the user literal "<pre>" markup."""
    from backend.telegram_bot import reply_html
    seen = []

    class _Picky:
        async def reply_text(self, text, **kw):
            if kw.get("parse_mode"):
                raise RuntimeError("Can't parse entities")
            seen.append(text)
    await reply_html(_Picky(), "<b>Update failed</b>\n<pre>ERROR: no matching distribution</pre>")
    assert len(seen) == 1
    assert "<pre>" not in seen[0] and "</pre>" not in seen[0]
    assert "ERROR: no matching distribution" in seen[0]


async def test_no_ack_when_nothing_is_pending(monkeypatch):
    from types import SimpleNamespace as NS

    from backend.telegram_bot import deliver_pending_update_notice
    from engine import updater
    sent = []
    monkeypatch.setattr(updater, "read_state", lambda clone_dir=updater.ROOT: {})

    async def send_message(chat_id, text, **kw):
        sent.append(text)
    await deliver_pending_update_notice(NS(bot=NS(send_message=send_message)))
    assert sent == []


# --------------------------------------------------------------------------
# /profiles, /profile (argus-3gf) — READ and SWITCH from the phone.
#
# Driven against a REAL Engine (same as tests/test_profiles.py) rather than a stub, because the
# load-bearing claims are about the STORE: that `/profile <name>` binds one session and leaves both
# the other session's binding and the global default alone. A fake engine could not fail those.
# --------------------------------------------------------------------------
class _PCfg:
    telegram_bot_token = "123:abc"
    allowed_chat_ids = [1, 2]


def _profile_engine(tmp_path):
    """An engine with three profiles: the migration's `Default`, a narrow `Assistant` and a wider
    `Homelab admin` that widens exactly two tools over it."""
    from config import Config
    from engine.engine import Engine
    from engine.profiles.store import Profile

    e = Engine(Config(model_base_url="http://x/v1", model_name="m", telegram_bot_token=""),
               data_dir=str(tmp_path))
    all_tools = e.profile_detail("Default")["all_tools"]
    assert "calculator" in all_tools and "get_current_time" in all_tools
    narrow = Profile(name="Assistant", description="Personal assistant",
                     tools={n: "ask" for n in all_tools})
    narrow.tools["calculator"] = "deny"
    wide = Profile(name="Homelab admin", description="Runs the homelab",
                   tools={n: "ask" for n in all_tools})
    wide.tools["calculator"] = "allow"            # deny -> allow : WIDER
    wide.tools["get_current_time"] = "allow"      # ask  -> allow : WIDER
    e.profiles.create(narrow)
    e.profiles.create(wide)
    return e


async def _profile_cmd(engine, cmd, args, chat_id=1):
    from types import SimpleNamespace as NS
    app = build_telegram_app(engine=engine, config=_PCfg())
    handlers = {c: h.callback for h in app.handlers[0] for c in (getattr(h, "commands", None) or [])}
    msg = _Msg()
    await handlers[cmd](NS(effective_chat=NS(id=chat_id), effective_message=msg), NS(args=args))
    return "\n".join(msg.sent)


# 1. /profiles lists every profile and marks the one bound to this chat.
async def test_profiles_lists_all_and_marks_the_one_bound_to_this_chat(tmp_path):
    e = _profile_engine(tmp_path)
    e.profiles.bind("1", "Homelab admin")
    out = await _profile_cmd(e, "profiles", [])
    for name in ("Default", "Assistant", "Homelab admin"):
        assert name in out, f"{name} missing from /profiles"
    assert out.count("← active in this chat") == 1
    marked = next(ln for ln in out.splitlines() if "← active in this chat" in ln)
    assert "Homelab admin" in marked
    assert "(default for new sessions)" in out          # the GLOBAL default is marked separately


# 2. /profile <name> rebinds THIS chat only — the other chat and the global default are untouched.
async def test_profile_switch_binds_this_session_only(tmp_path):
    e = _profile_engine(tmp_path)
    e.profiles.bind("2", "Assistant")                   # a second chat, bound elsewhere
    default_before = e.profiles.active_profile
    await _profile_cmd(e, "profile", ["Homelab", "admin"], chat_id=1)
    assert e.profiles.name_for_session("1") == "Homelab admin"
    assert e.profiles.name_for_session("2") == "Assistant"
    assert e.profiles.active_profile == default_before, "the GLOBAL default must not move"


# 3. The switch reply names every tool that WIDENED, and says so explicitly when none did.
async def test_switch_reply_announces_widened_tools(tmp_path):
    e = _profile_engine(tmp_path)
    e.profiles.bind("1", "Assistant")
    out = await _profile_cmd(e, "profile", ["Homelab", "admin"])
    assert "Homelab admin" in out
    assert "calculator (deny → allow)" in out
    assert "get_current_time (ask → allow)" in out
    assert 'Wider than "Assistant"' in out


async def test_switch_reply_says_so_when_nothing_widened(tmp_path):
    # Silence would read as "not checked" — the no-widening case must be stated.
    e = _profile_engine(tmp_path)
    e.profiles.bind("1", "Homelab admin")
    out = await _profile_cmd(e, "profile", ["Assistant"])     # strictly narrower
    assert "Nothing widened" in out
    assert "→ allow" not in out


# 4. An unknown name is refused, the valid names are listed, and nothing is rebound.
async def test_unknown_profile_is_refused_without_fuzzy_matching(tmp_path):
    e = _profile_engine(tmp_path)
    e.profiles.bind("1", "Assistant")
    out = await _profile_cmd(e, "profile", ["Homelab"])       # a prefix of a real profile
    assert "No profile named: Homelab" in out
    for name in ("Default", "Assistant", "Homelab admin"):
        assert name in out
    assert e.profiles.name_for_session("1") == "Assistant", "a near-miss must not switch anything"
    assert "✓" not in out


# 5. Bare /profile reports the active profile for this chat and changes nothing.
async def test_bare_profile_reports_without_changing_anything(tmp_path):
    e = _profile_engine(tmp_path)
    before = dict(e.profiles.sessions)
    default_before = e.profiles.active_profile
    out = await _profile_cmd(e, "profile", [])
    assert f"Active in this chat: {default_before}" in out
    assert e.profiles.sessions == before, "a read must not create a binding"
    assert e.profiles.active_profile == default_before

    e.profiles.bind("1", "Homelab admin")
    out = await _profile_cmd(e, "profile", [])
    assert "Active in this chat: Homelab admin" in out
    assert e.profiles.sessions == {"1": "Homelab admin"}


# 6. With a run in flight the reply says it applies next turn, and the run is left alone.
async def test_switch_during_a_run_applies_next_turn_and_leaves_the_run_alone(tmp_path):
    import asyncio

    e = _profile_engine(tmp_path)
    e.profiles.bind("1", "Assistant")
    interrupts = []

    async def _never_finishes():
        await asyncio.sleep(30)

    async def _interrupt(session_id):
        interrupts.append(session_id)
        return False
    e.interrupt = _interrupt
    task = asyncio.create_task(_never_finishes())
    e._running["1"] = task
    try:
        await asyncio.sleep(0)
        out = await _profile_cmd(e, "profile", ["Homelab", "admin"])
        assert "next message" in out
        assert "keeps the profile it started under" in out
        assert not task.done() and not task.cancelled(), "the in-flight run must not be cancelled"
        assert interrupts == [], "a slash command must not preempt the running turn"
        assert e.profiles.name_for_session("1") == "Homelab admin"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


async def test_switch_with_no_run_in_flight_omits_the_next_turn_wording(tmp_path):
    e = _profile_engine(tmp_path)
    e.profiles.bind("1", "Assistant")
    out = await _profile_cmd(e, "profile", ["Homelab", "admin"])
    assert "next message" not in out


# 7. Both commands reach /help, which is generated from BOT_COMMANDS.
def test_profile_commands_are_advertised_in_help():
    from backend.telegram_bot import help_text
    cmds = dict(BOT_COMMANDS)
    assert "profile" in cmds and "profiles" in cmds
    text = help_text()
    assert "/profiles —" in text and "/profile —" in text


def test_profile_names_are_not_shadowable_by_a_custom_alias():
    from engine.custom_commands import RESERVED_COMMANDS
    assert {"profile", "profiles"} <= RESERVED_COMMANDS


# --- the pure renderers (a long list must not be refused by Telegram) ---
def test_profiles_text_splits_rather_than_being_refused():
    from backend.telegram_bot import (
        TELEGRAM_MAX_CHARS,
        profiles_text,
        split_for_telegram,
    )
    rows = [{"name": f"profile_{i}", "description": "d" * 120, "is_default": i == 0,
             "sessions": [], "denied": [], "hidden_skills": [], "stale_tools": [], "stale_count": 0}
            for i in range(60)]
    text = profiles_text({"profiles": rows, "session_profile": "profile_3", "session_id": "1",
                          "active_profile": "profile_0"})
    assert len(text) > TELEGRAM_MAX_CHARS                     # the case the splitter exists for
    chunks = split_for_telegram(text, limit=TELEGRAM_MAX_CHARS)
    assert len(chunks) > 1
    assert all(len(c) <= TELEGRAM_MAX_CHARS for c in chunks)


def test_widened_text_lists_every_widened_tool():
    from backend.telegram_bot import widened_text
    w = [{"tool": "exec_python", "from": "ask", "to": "allow"},
         {"tool": "delete_row", "from": "deny", "to": "ask"}]
    out = widened_text(w, "Assistant")
    assert "exec_python (ask → allow)" in out
    assert "delete_row (deny → ask)" in out
    assert "Assistant" in out


def test_profile_shape_line_reports_the_staleness_count():
    from backend.telegram_bot import profile_shape_line
    line = profile_shape_line({"denied": ["a", "b"], "hidden_skills": ["s"], "stale_count": 4},
                              total_tools=14)
    assert "12 tools" in line and "2 denied" in line
    assert "1 skill hidden" in line
    assert "4 not configured (Ask)" in line
    assert profile_shape_line({}, total_tools=0) == ""
