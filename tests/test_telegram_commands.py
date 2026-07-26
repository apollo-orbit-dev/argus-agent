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

    def list_sessions(self):
        return self._sessions

    def rename_session(self, session_id, name):
        self.renamed.append((session_id, name))

    def reset(self, session_id):
        pass


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


def _stub_updater(monkeypatch, preview, apply_result=None, applied=None):
    from engine import updater
    monkeypatch.setattr(updater, "preview", lambda clone_dir=updater.ROOT: preview)
    monkeypatch.setattr(updater, "write_state", lambda clone_dir=updater.ROOT, **f: dict(f))
    monkeypatch.setattr(updater, "read_state", lambda clone_dir=updater.ROOT: {})
    monkeypatch.setattr(updater, "perform_restart", lambda info: None)

    def fake_apply(target, clone_dir=updater.ROOT, emit=None):
        if applied is not None:
            applied.append(target)
        if emit:
            emit({"type": "log", "line": "Successfully installed argus"})
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
